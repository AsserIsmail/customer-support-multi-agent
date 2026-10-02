"""LangGraph supervisor and specialists. All retrieval uses the MCP client boundary."""

import logging
import re
from typing import TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver

from support_ai.agent_models import DecisionModel, RoutePlan, SQLPlan, RAGPlan, AnswerDraft
from support_ai.mcp_client import SupportTools

logger = logging.getLogger(__name__)


class EvidenceError(ValueError):
    """Application-authored validation messages, safe to report in logs."""


class SupportState(TypedDict, total=False):
    question: str
    history: list[dict]
    selected_customer: dict | None
    pending_request: str
    route: str
    request: str
    evidence: dict
    response: dict | None
    trace: list[str]


def response(status: str, answer: str, sources: list | None = None) -> dict:
    return {"status": status, "answer": answer, "sources": sources or []}


class SupportAssistant:
    def __init__(self, tools: SupportTools, model: DecisionModel):
        self.tools = tools
        self.model = model
        self.memory = InMemorySaver()
        self._active_threads: set[str] = set()
        builder = StateGraph(SupportState)
        for name, node in (("supervisor", self._supervisor), ("sql", self._sql),
                           ("rag", self._rag), ("synthesis", self._synthesis), ("finish", self._finish)):
            builder.add_node(name, node)
        builder.add_edge(START, "supervisor")
        builder.add_conditional_edges("supervisor", lambda s: "finish" if s.get("response") else
                                      "sql" if s["route"] in ("sql", "both") else "rag",
                                      ["finish", "sql", "rag"])
        builder.add_conditional_edges("sql", lambda s: "finish" if s.get("response") else
                                      "rag" if s["route"] == "both" else "synthesis",
                                      ["finish", "rag", "synthesis"])
        builder.add_conditional_edges("rag", lambda s: "finish" if s.get("response") else "synthesis",
                                      ["finish", "synthesis"])
        builder.add_edge("synthesis", "finish")
        builder.add_edge("finish", END)
        self.graph = builder.compile(checkpointer=self.memory)

    async def ask(self, question: str, *, thread_id: str) -> dict:
        if not isinstance(question, str) or not question.strip() or len(question) > 4000:
            raise ValueError("Question must contain 1 to 4000 characters")
        if not isinstance(thread_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", thread_id):
            raise ValueError("Thread ID must contain 1 to 100 letters, digits, underscores, or hyphens")
        if thread_id in self._active_threads:
            raise ValueError("A request is already running for this conversation")
        self._active_threads.add(thread_id)
        try:
            state = await self.graph.ainvoke({"question": question.strip()},
                config={"configurable": {"thread_id": thread_id}, "recursion_limit": 10})
            return dict(state["response"], route=state["route"], trace=state["trace"])
        finally:
            self._active_threads.discard(thread_id)

    async def forget(self, thread_id: str) -> None:
        if thread_id in self._active_threads:
            raise ValueError("Cannot clear a conversation while it is running")
        await self.memory.adelete_thread(thread_id)

    def _failure(self, role: str, exc: Exception, trace: list[str]) -> dict:
        logger.error("Agent node %s failed (%s)", role, type(exc).__name__)
        return {"response": response("error", f"I could not complete the {role} step. Please retry; "
                                     "if it continues, check the application configuration and logs."),
                "trace": trace + [role]}

    async def _supervisor(self, state: SupportState) -> dict:
        reset = {"evidence": {}, "response": None, "trace": [], "route": "clarify", "request": state["question"]}
        try:
            plan = await self.model.decide("supervisor", RoutePlan, {
                "question": state["question"], "history": state.get("history", []),
                "selected_customer": state.get("selected_customer"),
                "pending_request": state.get("pending_request", ""),
            })
            reset.update(route=plan.route, request=plan.request[:4000], trace=["supervisor"])
            logger.info("Supervisor selected route %s", plan.route)
            if plan.route == "clarify":
                reset["response"] = response("needs_clarification", plan.clarification or "What would you like to know?")
            elif plan.route == "out_of_scope":
                reset["response"] = response("insufficient_evidence", "I can help with customer profiles, orders, "
                                             "support tickets, and company policies. What would you like to know?")
        except Exception as exc:
            reset.update(self._failure("supervisor", exc, []))
        return reset

    async def _sql(self, state: SupportState) -> dict:
        trace = state["trace"] + ["sql"]
        selected = None
        try:
            plan = await self.model.decide("sql", SQLPlan, {
                "request": state["request"], "question": state["question"],
                "selected_customer": state.get("selected_customer"),
            })
            if plan.customer_query:
                result = await self.tools.call("customer_lookup", {"query": plan.customer_query})
                matches = result["customers"]
                if len(matches) > 1:
                    names = "; ".join(f"{item['name']} (customer {item['id']})" for item in matches)
                    return {"selected_customer": None, "trace": trace,
                            "response": response("needs_clarification", f"Which customer do you mean: {names}?")}
                selected = matches[0] if matches else None
            elif plan.customer_id is not None:
                selected = (await self.tools.call("customer_profile", {"customer_id": plan.customer_id}))["customer"]
            elif plan.use_previous_customer and state.get("selected_customer"):
                selected = (await self.tools.call("customer_profile", {
                    "customer_id": state["selected_customer"]["id"]}))["customer"]
            else:
                return {"selected_customer": None, "trace": trace,
                        "response": response("needs_clarification", "Which customer name, email, or customer ID should I look up?")}
            if not selected:
                return {"selected_customer": None, "trace": trace,
                        "response": response("insufficient_evidence", "I couldn't find that customer. Please check the name, email, or ID.")}
            evidence = {"customer": {"kind": "sql", "data": selected}}
            for kind, tool, needed, status in (
                ("orders", "order_lookup", plan.orders, plan.order_status),
                ("tickets", "support_tickets", plan.tickets, plan.ticket_status),
            ):
                if not needed:
                    continue
                rows, offset, complete = [], 0, False
                # Bound retrieval to four pages (200 records) per history, never an unbounded tool loop.
                for _ in range(4):
                    page = await self.tools.call(tool, {"customer_id": selected["id"], "limit": 50, "offset": offset})
                    if page["status"] == "customer_not_found":
                        raise ValueError("Customer disappeared during history lookup")
                    rows.extend(page[kind])
                    next_offset = page["next_offset"]
                    if next_offset is None:
                        complete = True
                        break
                    if next_offset <= offset:
                        raise ValueError("Invalid pagination response")
                    offset = next_offset
                matched = [row for row in rows if status is None or row["status"] == status]
                evidence[kind] = {"kind": "sql", "data": matched[:plan.record_limit],
                                  "complete": complete, "status_filter": status,
                                  "omitted": max(0, len(matched) - plan.record_limit)}
            return {"selected_customer": selected, "evidence": evidence, "trace": trace}
        except Exception as exc:
            return dict(self._failure("sql", exc, state["trace"]), selected_customer=None)

    async def _rag(self, state: SupportState) -> dict:
        try:
            plan = await self.model.decide("rag", RAGPlan, {"request": state["request"],
                                            "customer_evidence": state["evidence"]})
            result = await self.tools.call("policy_search", {"query": plan.query, "limit": 5})
            if not result["passages"]:
                return {"trace": state["trace"] + ["rag"], "response": response("insufficient_evidence",
                    "I couldn't retrieve a relevant policy passage. Please upload the applicable policy or rephrase the question; "
                    "I can't determine policy eligibility from customer records alone.")}
            evidence = dict(state["evidence"])
            for passage in result["passages"]:
                evidence[passage["chunk_id"]] = {"kind": "policy", "data": passage}
            return {"evidence": evidence, "trace": state["trace"] + ["rag"]}
        except Exception as exc:
            return self._failure("rag", exc, state["trace"])

    async def _synthesis(self, state: SupportState) -> dict:
        try:
            draft = await self.model.decide("synthesis", AnswerDraft, {"request": state["request"],
                                                                       "route": state["route"],
                                                                       "evidence": state["evidence"]})
            if not draft.blocks or len(draft.blocks) > 12:
                raise EvidenceError("Invalid answer blocks")
            evidence = state["evidence"]
            sources, lines, used_policy, used_sql = {}, [], False, False
            for block in draft.blocks:
                if not block.text.strip() or len(block.text) > 6000:
                    raise EvidenceError("Invalid answer text")
                if not block.evidence_ids or any(item not in evidence for item in block.evidence_ids):
                    raise EvidenceError("Answer lacks valid evidence references")
                if re.search(r"\[[^\]]*\]|https?://", block.text):
                    raise EvidenceError("Model attempted to generate its own citation")
                citations = []
                for evidence_id in dict.fromkeys(block.evidence_ids):
                    item = evidence[evidence_id]
                    if item["kind"] == "sql":
                        used_sql = True
                    if item["kind"] == "policy":
                        used_policy = True
                        passage = item["data"]
                        citation = f"[{passage['source']}, p. {passage['page']}]"
                        if citation not in citations:
                            citations.append(citation)
                        sources[evidence_id] = {"chunk_id": evidence_id, "source": passage["source"],
                            "page": passage["page"], "citation": citation}
                lines.append(block.text.strip() + (" " + " ".join(citations) if citations else ""))
            if state["route"] in ("rag", "both") and not used_policy:
                raise EvidenceError("Policy answer omitted source references")
            if state["route"] == "both" and not used_sql:
                raise EvidenceError("Combined answer omitted customer evidence")
            return {"trace": state["trace"] + ["synthesis"],
                    "response": response(draft.status, "\n\n".join(lines), list(sources.values()))}
        except Exception as exc:
            if isinstance(exc, EvidenceError):
                logger.warning("Answer validation: %s", str(exc))
            return self._failure("synthesis", exc, state["trace"])

    async def _finish(self, state: SupportState) -> dict:
        history = state.get("history", []) + [
            {"role": "user", "content": state["question"]},
            {"role": "assistant", "content": state["response"]["answer"]},
        ]
        return {"history": history[-12:], "pending_request": state["request"]
                if state["response"]["status"] == "needs_clarification" else ""}
