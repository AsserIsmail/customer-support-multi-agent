import asyncio
from types import SimpleNamespace

import pytest

from support_ai.agent_models import OracleQueryPlan, QueryBind, RAGPlan
from support_ai.agents import SupportAssistant
from support_ai.mcp_client import MCPToolError
from test_agents import FakeTools, ScriptedModel, route, sql, draft


class OracleTools(FakeTools):
    def __init__(self, *, errors=(), truncated=False, **kwargs):
        super().__init__(**kwargs)
        self.errors = list(errors)
        self.truncated = truncated
        self.version = "v1"

    async def list_tools(self):
        return [SimpleNamespace(name="execute_readonly_query")]

    async def call(self, name, arguments):
        if name in {"customer_lookup", "customer_profile", "policy_search"}:
            return await super().call(name, arguments)
        self.calls.append((name, arguments))
        if name == self.fail:
            raise MCPToolError("unavailable")
        base = dict(owner="SUPPORT_OWNER", schema_version=self.version, status="found")
        if name == "list_tables":
            return dict(base, tables=[{"table": "ORDERS"}, {"table": "TICKETS"}])
        if name == "search_schema":
            return dict(base, matches=[{"table": "TICKETS"}])
        if name == "describe_table":
            return dict(base, table={"table": arguments["table"], "columns": [{"column_name": "CUSTOMER_ID"}]})
        if name == "get_relationships":
            return dict(base, relationships=[{"table": "TICKETS", "referenced_table": "ORDERS",
                "column_pairs": [{"column": "ORDER_ID", "referenced_column": "ID"},
                                 {"column": "CUSTOMER_ID", "referenced_column": "CUSTOMER_ID"}]}])
        if name == "refresh_schema_index":
            self.version = "v2"
            return dict(base, status="refreshed")
        if name == "execute_readonly_query":
            if self.errors:
                raise MCPToolError("raw detail must not reach planner", code=self.errors.pop(0))
            return dict(status="found", columns=[{"name": "ID"}], rows=[[103]],
                        truncated=self.truncated, truncation_reasons=["row_limit"] if self.truncated else [],
                        query_id="test", tables=[{"table": "ORDERS"}])
        raise AssertionError(name)


def query(sql_text="SELECT id FROM orders WHERE customer_id = :customer_id"):
    return ("oracle_sql", OracleQueryPlan(sql=sql_text,
        parameters=[QueryBind(name="customer_id", value=1)], row_limit=50, clarification=""))


def test_actual_graph_discovers_schema_before_planning_and_execution():
    tools = OracleTools(truncated=True)
    model = ScriptedModel(route("sql"), sql(orders=True), query(), draft(ids=["query"]))
    result = asyncio.run(SupportAssistant(tools, model).ask("Emma orders", thread_id="a"))
    assert result["status"] == "answered"
    assert "results are partial" in result["answer"]
    names = [name for name, _ in tools.calls]
    assert names == ["customer_lookup", "list_tables", "search_schema", "describe_table",
                     "describe_table", "get_relationships", "execute_readonly_query"]
    payload = model.payloads[2][1]
    assert payload["schema"]["tables"][0]["table"] == "TICKETS"
    assert len(payload["schema"]["relationships"][0]["column_pairs"]) == 2
    assert payload["selected_customer"]["id"] == 1
    evidence = model.payloads[-1][1]["evidence"]["query"]
    assert evidence["complete"] is False
    assert evidence["data"]["schema_version"] == "v1"


@pytest.mark.parametrize("code", ["invalid_sql", "invalid_columns"])
def test_invalid_query_refreshes_and_repairs_once_with_safe_code(code):
    tools = OracleTools(errors=[code])
    model = ScriptedModel(route("sql"), sql(), query(), query(), draft(ids=["query"]))
    result = asyncio.run(SupportAssistant(tools, model).ask("Emma orders", thread_id="a"))
    assert result["status"] == "answered"
    repaired = model.payloads[3][1]
    assert repaired["error_code"] == code
    assert repaired["schema"]["schema_version"] == "v2"
    assert "raw detail" not in str(repaired)
    assert [name for name, _ in tools.calls].count("refresh_schema_index") == 1


@pytest.mark.parametrize("errors,plans", [(["invalid_sql", "invalid_sql"], 2), (["timeout"], 1), (["database_error"], 1)])
def test_failures_are_bounded_and_never_fall_back_to_fixed_lookups(errors, plans):
    tools = OracleTools(errors=errors)
    model = ScriptedModel(route("sql"), sql(), *[query() for _ in range(plans)])
    result = asyncio.run(SupportAssistant(tools, model).ask("Emma orders", thread_id="a"))
    assert result["status"] == "error"
    names = [name for name, _ in tools.calls]
    assert names.count("execute_readonly_query") == plans
    assert "order_lookup" not in names


def test_failed_discovery_never_generates_or_executes_sql():
    tools = OracleTools(fail="describe_table")
    model = ScriptedModel(route("sql"), sql())
    result = asyncio.run(SupportAssistant(tools, model).ask("Emma orders", thread_id="a"))
    assert result["status"] == "error"
    assert not any(name == "execute_readonly_query" for name, _ in tools.calls)


def test_ambiguous_customer_never_generates_sql():
    tools = OracleTools(ambiguous=True)
    model = ScriptedModel(route("sql"), sql(query="Emma"))
    result = asyncio.run(SupportAssistant(tools, model).ask("Emma orders", thread_id="a"))
    assert result["status"] == "needs_clarification"
    assert [name for name, _ in tools.calls] == ["customer_lookup"]


def test_missing_schema_information_clarifies_without_execution():
    tools = OracleTools()
    plan = OracleQueryPlan(sql=None, parameters=[], row_limit=50, clarification="What was the delivery date?")
    model = ScriptedModel(route("sql"), sql(), ("oracle_sql", plan))
    result = asyncio.run(SupportAssistant(tools, model).ask("Emma delivery date", thread_id="a"))
    assert result["status"] == "needs_clarification"
    assert not any(name == "execute_readonly_query" for name, _ in tools.calls)


def test_combined_route_passes_query_evidence_to_rag_and_synthesis():
    tools = OracleTools()
    model = ScriptedModel(route("both"), sql(orders=True), query(),
        ("rag", RAGPlan(query="refund eligibility")), draft(ids=["query", "doc:0"]))
    result = asyncio.run(SupportAssistant(tools, model).ask("Can Emma return it?", thread_id="a"))
    assert result["status"] == "answered"
    assert result["trace"] == ["supervisor", "sql", "rag", "synthesis"]
    assert model.payloads[3][1]["customer_evidence"]["query"]["data"]["rows"] == [[103]]
    assert result["sources"][0]["source"] == "returns.pdf"


def test_schema_changes_during_discovery_are_refreshed_before_generation():
    class ChangingTools(OracleTools):
        async def call(self, name, arguments):
            result = await super().call(name, arguments)
            if name == "get_relationships" and self.version == "v1":
                result["schema_version"] = "changed"
            return result
    tools = ChangingTools()
    model = ScriptedModel(route("sql"), sql(), query(), draft(ids=["query"]))
    result = asyncio.run(SupportAssistant(tools, model).ask("Emma orders", thread_id="a"))
    assert result["status"] == "answered"
    assert model.payloads[2][1]["schema"]["schema_version"] == "v2"
    assert [name for name, _ in tools.calls].count("execute_readonly_query") == 1


def test_synthesis_cannot_ignore_executed_query_evidence():
    model = ScriptedModel(route("sql"), sql(), query(), draft(ids=["customer"]))
    result = asyncio.run(SupportAssistant(OracleTools(), model).ask("Emma orders", thread_id="a"))
    assert result["status"] == "error"


def test_mcp_error_code_is_preserved_for_bounded_repair():
    from unittest.mock import AsyncMock
    from mcp.types import CallToolResult
    from support_ai.mcp_client import SupportTools
    session = AsyncMock()
    session.call_tool.return_value = CallToolResult(content=[], is_error=True,
        structured_content={"status": "error", "code": "invalid_sql", "message": "Invalid query"})
    with pytest.raises(MCPToolError) as error:
        asyncio.run(SupportTools(session).call("execute_readonly_query", {}))
    assert error.value.code == "invalid_sql"
