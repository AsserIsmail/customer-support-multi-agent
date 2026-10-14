"""Opt-in live-model smoke evaluation. Reports contain demo records; keep them local."""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from time import monotonic

from support_ai.agent_models import OpenAIDecisions
from support_ai.agents import SupportAssistant
from support_ai.config import Settings, configure_logging
from support_ai.mcp_client import connect_support_tools

CASES = [
    ("ambiguity", "conversation", "Show Emma's latest order", "sql", {"needs_clarification"}),
    ("clarification", "conversation", "I mean Emma Wilson", "sql", {"answered"}),
    ("followup", "conversation", "Show her open support tickets", "sql", {"answered"}),
    ("aggregate", "aggregate", "How many orders does Emma Wilson have in each status?", "sql", {"answered"}),
    ("join", "join", "Show Emma Wilson's orders alongside their support tickets, including both IDs", "sql", {"answered"}),
    ("combined", "combined", "Why hasn't Emma Wilson's latest order arrived, and is she eligible for a refund?", "both", {"needs_clarification", "insufficient_evidence", "answered"}),
    ("topic_change", "combined", "What is the standard refund policy?", "rag", {"answered"}),
]


class RecordingModel:
    def __init__(self, delegate):
        self.delegate = delegate
        self.decisions = []

    async def decide(self, role, schema, payload):
        decision = await self.delegate.decide(role, schema, payload)
        self.decisions.append({"role": role, "input": payload, "output": decision.model_dump()})
        return decision


class RecordingTools:
    def __init__(self, delegate):
        self.delegate = delegate
        self.calls = []

    async def list_tools(self):
        return await self.delegate.list_tools()

    async def call(self, name, arguments):
        call = {"name": name, "arguments": arguments}
        self.calls.append(call)
        try:
            result = await self.delegate.call(name, arguments)
            call["result"] = result
            return result
        except Exception as exc:
            call["error_type"] = type(exc).__name__
            call["error_code"] = getattr(exc, "code", None)
            raise


def checks_for(result, calls, route, statuses, backend, case_name=""):
    """Structural checks do not grade query semantics or factual answer quality."""
    checks = {"expected_route": result["route"] == route,
              "expected_status": result["status"] in statuses}
    if route in {"rag", "both"}:
        checks["policy_citations"] = bool(result.get("sources"))
    executed = [i for i, call in enumerate(calls) if call["name"] == "execute_readonly_query"]
    if backend == "oracle" and route in {"sql", "both"} and case_name != "ambiguity":
        checks["oracle_executed"] = bool(executed)
    if executed:
        required = {"list_tables", "search_schema", "describe_table", "get_relationships"}
        checks["discovery_before_execution"] = all(
            required <= {c["name"] for c in calls[:index]} for index in executed)
    return checks


async def evaluate(output: Path, *, backend: str):
    settings = Settings.from_env()
    if settings.db_backend != backend:
        raise ValueError("Configured backend does not match --backend")
    configure_logging(settings.log_level)
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "backend": backend,
              "model": os.getenv("OPENAI_CHAT_MODEL", "gpt-4.1-mini"),
              "completed": False, "grading": "Structural checks only; SQL semantics and answers require review", "cases": []}
    model = RecordingModel(OpenAIDecisions())
    output.parent.mkdir(parents=True, exist_ok=True)
    def save():
        output.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
    async with connect_support_tools() as connected:
        # Capture independently retrieved records as the manual review baseline.
        report["baseline"] = {
            "customer": await connected.call("customer_profile", {"customer_id": 1}),
            "orders": await connected.call("order_lookup", {"customer_id": 1}),
            "tickets": await connected.call("support_tickets", {"customer_id": 1}),
        }
        if report["baseline"]["customer"].get("customer", {}).get("name") != "Emma Wilson":
            raise ValueError("Evaluation requires the synthetic Emma Wilson dataset")
        tools = RecordingTools(connected)
        assistant = SupportAssistant(tools, model)
        for name, thread, question, route, statuses in CASES:
            tools.calls, model.decisions = [], []
            started = monotonic()
            result = await assistant.ask(question, thread_id=thread)
            checks = checks_for(result, tools.calls, route, statuses, backend, name)
            report["cases"].append({"name": name, "question": question, "result": result,
                "elapsed_seconds": round(monotonic() - started, 2), "checks": checks,
                "tool_calls": tools.calls, "decisions": model.decisions})
            save()
            print(f"{name}: {result['status']} (structural checks {'passed' if all(checks.values()) else 'FAILED'})", flush=True)
            if result["status"] == "error" and result["trace"] == ["supervisor"]:
                report["stopped_reason"] = "Supervisor failed; check model access before spending on further cases"
                save()
                break
        report["completed"] = len(report["cases"]) == len(CASES)
        save()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=["oracle", "sqlite"], default="oracle")
    parser.add_argument("--output", type=Path, default=Path("output/evaluation/live.json"))
    args = parser.parse_args()
    try:
        report = asyncio.run(evaluate(args.output, backend=args.backend))
    except Exception as exc:
        parser.exit(1, f"Evaluation could not complete ({type(exc).__name__}); check local configuration.\n")
    if not report["completed"] or not all(all(case["checks"].values()) for case in report["cases"]):
        parser.exit(1, "Evaluation completed with failed structural checks; review the local report.\n")


if __name__ == "__main__":
    main()
