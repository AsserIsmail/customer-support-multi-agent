import asyncio
from copy import deepcopy

import pytest

from support_ai.agent_models import RoutePlan, SQLPlan, RAGPlan, AnswerDraft, AnswerBlock, ModelError
from support_ai.agents import SupportAssistant
from support_ai.mcp_client import MCPToolError, connect_support_tools
from support_ai.seed import seed_database


class ScriptedModel:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.payloads = []

    async def decide(self, role, schema, payload):
        self.payloads.append((role, deepcopy(payload)))
        expected_role, value = self.steps.pop(0)
        assert role == expected_role
        if isinstance(value, Exception):
            raise value
        assert isinstance(value, schema)
        return value


CUSTOMER = {"id": 1, "name": "Emma Wilson", "plan": "basic"}
PASSAGE = {"chunk_id": "doc:0", "source": "returns.pdf", "page": 1,
           "text": "Returns within 30 days of delivery.", "document_id": "doc"}


class FakeTools:
    def __init__(self, *, ambiguous=False, passages=None, fail=None):
        self.calls = []
        self.ambiguous = ambiguous
        self.passages = [PASSAGE] if passages is None else passages
        self.fail = fail

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == self.fail:
            raise MCPToolError("Unavailable")
        if name == "customer_lookup":
            if arguments["query"] == "Nobody":
                return {"customers": []}
            if self.ambiguous and arguments["query"] == "Emma":
                return {"customers": [CUSTOMER, {"id": 2, "name": "Emma Chen"}]}
            if arguments["query"] == "Liam":
                return {"customers": [{"id": 3, "name": "Liam Martin"}]}
            return {"customers": [CUSTOMER]}
        if name == "customer_profile":
            return {"customer": CUSTOMER}
        if name == "order_lookup":
            return {"status": "found", "orders": [{"id": 103, "customer_id": arguments["customer_id"],
                "product": "Monitor", "amount_cents": 29900, "currency": "USD", "status": "shipped"}],
                "next_offset": None}
        if name == "support_tickets":
            return {"status": "found", "tickets": [
                {"id": 103, "status": "open", "subject": "Delivery tracking"},
                {"id": 102, "status": "closed", "subject": "Return"}], "next_offset": None}
        if name == "policy_search":
            return {"passages": self.passages}
        raise AssertionError(name)


def route(kind, question="question"):
    return ("supervisor", RoutePlan(route=kind, request=question, clarification="Which request?"))


def sql(query="Emma Wilson", **kwargs):
    fields = dict(customer_query=query, customer_id=None, use_previous_customer=False,
                  orders=False, tickets=False, order_status=None, ticket_status=None, record_limit=50)
    fields.update(kwargs)
    return ("sql", SQLPlan(**fields))


def draft(text="Answer", ids=None, status="answered"):
    return ("synthesis", AnswerDraft(status=status, blocks=[AnswerBlock(text=text, evidence_ids=ids or ["customer"])]))


def test_sql_filter_and_mcp_only_calls():
    tools = FakeTools()
    model = ScriptedModel(route("sql"), sql(tickets=True, ticket_status="open"), draft(ids=["tickets"]))
    assistant = SupportAssistant(tools, model)
    result = asyncio.run(assistant.ask("Show Emma Wilson's open tickets", thread_id="a"))
    assert result["trace"] == ["supervisor", "sql", "synthesis"]
    assert [name for name, _ in tools.calls] == ["customer_lookup", "support_tickets"]
    evidence = model.payloads[-1][1]["evidence"]["tickets"]
    assert [item["id"] for item in evidence["data"]] == [103]


def test_rag_citations_are_rendered_from_retrieved_metadata():
    tools = FakeTools()
    model = ScriptedModel(route("rag"), ("rag", RAGPlan(query="refund window")),
                          draft("Returns are accepted within 30 days of delivery.", ["doc:0"]))
    result = asyncio.run(SupportAssistant(tools, model).ask("Refund policy?", thread_id="a"))
    assert result["trace"] == ["supervisor", "rag", "synthesis"]
    assert result["answer"].endswith("[returns.pdf, p. 1]")
    assert result["sources"][0]["chunk_id"] == "doc:0"
    assert [name for name, _ in tools.calls] == ["policy_search"]


def test_combined_evidence_and_missing_delivery_date():
    model = ScriptedModel(route("both"), sql(orders=True), ("rag", RAGPlan(query="refund policy")),
        draft("The order is shipped. The policy requires delivery within 30 days; when was it delivered?",
              ["orders", "doc:0"], "needs_clarification"))
    result = asyncio.run(SupportAssistant(FakeTools(), model).ask("Can Emma return it?", thread_id="a"))
    assert result["status"] == "needs_clarification"
    assert result["trace"] == ["supervisor", "sql", "rag", "synthesis"]
    assert set(model.payloads[-1][1]["evidence"]) == {"customer", "orders", "doc:0"}


def test_ambiguity_followup_and_thread_isolation():
    tools = FakeTools(ambiguous=True)
    model = ScriptedModel(route("sql", "Show Emma's tickets"), sql("Emma", tickets=True),
        route("sql", "Show Emma Wilson's tickets"), sql(tickets=True), draft(ids=["tickets"]),
        route("sql"), sql(None, use_previous_customer=True, orders=True), draft(ids=["orders"]),
        route("sql"), sql(None, use_previous_customer=True))
    assistant = SupportAssistant(tools, model)

    async def run():
        first = await assistant.ask("Show Emma's tickets", thread_id="a")
        assert first["status"] == "needs_clarification"
        assert "Emma Chen" in first["answer"]
        assert [name for name, _ in tools.calls] == ["customer_lookup"]
        await assistant.ask("I mean Emma Wilson", thread_id="a")
        assert model.payloads[2][1]["pending_request"] == "Show Emma's tickets"
        await assistant.ask("What was her latest order?", thread_id="a")
        assert model.payloads[5][1]["selected_customer"]["id"] == 1
        other = await assistant.ask("What was her order?", thread_id="b")
        assert other["status"] == "needs_clarification"
        assert model.payloads[8][1]["history"] == []
    asyncio.run(run())


def test_new_customer_replaces_selected_customer_and_clears_evidence():
    tools = FakeTools()
    model = ScriptedModel(route("sql"), sql(orders=True), draft(ids=["orders"]),
                          route("sql"), sql("Liam"), draft())
    assistant = SupportAssistant(tools, model)
    async def run():
        await assistant.ask("Emma's orders", thread_id="a")
        await assistant.ask("Liam's profile", thread_id="a")
    asyncio.run(run())
    final_evidence = model.payloads[-1][1]["evidence"]
    assert list(final_evidence) == ["customer"]
    assert final_evidence["customer"]["data"]["id"] == 3


@pytest.mark.parametrize("bad_ids,text", [(["invented:0"], "A fact"), (["customer"], "A fact"),
                                        (["doc:0"], "A fact"),
                                        (["doc:0"], "A fact [fake.pdf, p. 99]")])
def test_unverified_citations_fail_closed(bad_ids, text):
    model = ScriptedModel(route("both"), sql(orders=True), ("rag", RAGPlan(query="refund")), draft(text, bad_ids))
    result = asyncio.run(SupportAssistant(FakeTools(), model).ask("Can she return it?", thread_id="a"))
    assert result["status"] == "error"
    assert result["sources"] == []
    assert "A fact" not in result["answer"]


def test_no_policy_evidence_skips_synthesis():
    model = ScriptedModel(route("both"), sql(orders=True), ("rag", RAGPlan(query="refund")))
    result = asyncio.run(SupportAssistant(FakeTools(passages=[]), model).ask("Can she return it?", thread_id="a"))
    assert result["status"] == "insufficient_evidence"
    assert result["trace"] == ["supervisor", "sql", "rag"]


@pytest.mark.parametrize("failed_tool", ["customer_lookup", "order_lookup", "policy_search"])
def test_tool_errors_become_safe_answers(failed_tool):
    steps = [route("both"), sql(orders=True)]
    if failed_tool == "policy_search":
        steps.append(("rag", RAGPlan(query="refund")))
    result = asyncio.run(SupportAssistant(FakeTools(fail=failed_tool), ScriptedModel(*steps)).ask("question", thread_id="a"))
    assert result["status"] == "error"


def test_model_failure_does_not_return_previous_answer():
    model = ScriptedModel(route("sql"), sql(), draft("Previous answer"),
                          ("supervisor", ModelError("secret-provider-details")))
    assistant = SupportAssistant(FakeTools(), model)
    async def run():
        await assistant.ask("question", thread_id="a")
        result = await assistant.ask("followup", thread_id="a")
        assert result["status"] == "error"
        assert "Previous answer" not in result["answer"]
        assert "secret-provider-details" not in result["answer"]
    asyncio.run(run())


def test_missing_customer_and_out_of_scope():
    model = ScriptedModel(route("sql"), sql("Nobody"), route("out_of_scope"))
    assistant = SupportAssistant(FakeTools(), model)
    async def run():
        assert (await assistant.ask("Nobody's orders", thread_id="a"))["status"] == "insufficient_evidence"
        assert (await assistant.ask("Write a poem", thread_id="a"))["trace"] == ["supervisor"]
    asyncio.run(run())


def test_history_is_bounded_and_can_be_forgotten():
    model = ScriptedModel(*[route("out_of_scope") for _ in range(9)])
    assistant = SupportAssistant(FakeTools(), model)
    async def run():
        for _ in range(8):
            await assistant.ask("Hello", thread_id="a")
        assert len(model.payloads[-1][1]["history"]) == 12
        await assistant.forget("a")
        await assistant.ask("Hello", thread_id="a")
        assert model.payloads[-1][1]["history"] == []
    asyncio.run(run())


def test_real_mcp_customer_path_with_scripted_model(tmp_path):
    import os
    path = tmp_path / "customers.db"
    seed_database(path)
    env = dict(os.environ, SUPPORT_DB_PATH=str(path))
    async def run():
        async with connect_support_tools(cwd=tmp_path, env=env) as tools:
            model = ScriptedModel(route("sql"), sql(orders=True, record_limit=1),
                                  draft("The latest order is a Monitor for $299.00.", ["orders"]))
            result = await SupportAssistant(tools, model).ask("Emma Wilson's latest order?", thread_id="a")
            assert result["status"] == "answered"
            assert model.payloads[-1][1]["evidence"]["orders"]["data"][0]["id"] == 103
    asyncio.run(run())


def test_history_pagination_is_bounded_and_marked_incomplete():
    class ManyOrders(FakeTools):
        async def call(self, name, arguments):
            if name != "order_lookup":
                return await super().call(name, arguments)
            self.calls.append((name, arguments))
            offset = arguments["offset"]
            return {"status": "found", "orders": [{"id": offset + i, "status": "shipped"}
                for i in range(50)], "next_offset": offset + 50}
    tools = ManyOrders()
    model = ScriptedModel(route("sql"), sql(orders=True), draft(ids=["orders"]))
    asyncio.run(SupportAssistant(tools, model).ask("All orders", thread_id="a"))
    assert sum(name == "order_lookup" for name, _ in tools.calls) == 4
    evidence = model.payloads[-1][1]["evidence"]["orders"]
    assert evidence["complete"] is False
    assert evidence["omitted"] == 150


def test_concurrent_requests_for_same_thread_are_rejected():
    class WaitingModel:
        async def decide(self, role, schema, payload):
            entered.set()
            await release.wait()
            return route("out_of_scope")[1]
    async def run():
        nonlocal entered, release
        entered, release = asyncio.Event(), asyncio.Event()
        assistant = SupportAssistant(FakeTools(), WaitingModel())
        first = asyncio.create_task(assistant.ask("Hi", thread_id="same"))
        await entered.wait()
        with pytest.raises(ValueError, match="already running"):
            await assistant.ask("Hi again", thread_id="same")
        release.set()
        await first
    entered = release = None
    asyncio.run(run())


@pytest.mark.parametrize("question,thread", [("", "a"), ("x" * 4001, "a"), ("hello", "../other")])
def test_invalid_chat_input(question, thread):
    assistant = SupportAssistant(FakeTools(), ScriptedModel())
    with pytest.raises(ValueError):
        asyncio.run(assistant.ask(question, thread_id=thread))
