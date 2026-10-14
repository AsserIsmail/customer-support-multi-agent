"""Schema-grounded Oracle planning through MCP, with one bounded repair attempt."""

import json

from support_ai.agent_models import OracleQueryPlan
from support_ai.mcp_client import MCPToolError


async def discover(tools, request: str) -> dict:
    listing = await tools.call("list_tables", {})
    names = [item["table"] for item in listing["tables"]]
    if not names or len(names) > 8:
        raise MCPToolError("Schema discovery requires 1..8 approved tables")
    # Search prioritizes descriptions; include the other approved tables so joins
    # do not lose bridge tables when the question omits their names.
    search = await tools.call("search_schema", {"query": request[:400], "limit": 20})
    ranked = [item["table"] for item in search["matches"] if item["table"] in names]
    ordered = list(dict.fromkeys(ranked + names))
    descriptions = [await tools.call("describe_table", {"table": name}) for name in ordered]
    relationships = await tools.call("get_relationships", {})
    snapshots = [search, *descriptions, relationships]
    if any(item.get("schema_version") != listing["schema_version"] for item in snapshots):
        raise MCPToolError("Schema changed during discovery", code="schema_changed")
    if any(item.get("status") != "found" or not item.get("table") for item in descriptions):
        raise MCPToolError("A discovered table is unavailable", code="schema_changed")
    schema = {"owner": listing["owner"], "schema_version": listing["schema_version"],
              "tables": [item["table"] for item in descriptions],
              "relationships": relationships["relationships"]}
    if len(json.dumps(schema)) > 60000:
        raise MCPToolError("Schema metadata exceeds the planning budget")
    return schema


async def retrieve_oracle_evidence(tools, model, request: str, customer: dict) -> dict:
    previous_sql, error_code = None, None
    for attempt in range(2):
        try:
            schema = await discover(tools, request)
            plan = await model.decide("oracle_sql", OracleQueryPlan, {
                "request": request, "selected_customer": customer, "schema": schema,
                "previous_sql": previous_sql, "error_code": error_code,
            })
            if plan.sql is None:
                return {"clarification": plan.clarification or "Which available customer records should I retrieve?"}
            previous_sql = plan.sql
            parameters = {bind.name: bind.value for bind in plan.parameters}
            if len(parameters) != len(plan.parameters):
                raise MCPToolError("Duplicate parameter names", code="invalid_parameters")
            result = await tools.call("execute_readonly_query", {
                "sql": plan.sql, "parameters": parameters, "row_limit": plan.row_limit,
            })
            return dict(result, schema_version=schema["schema_version"])
        except MCPToolError as exc:
            if attempt or exc.code not in {"invalid_sql", "invalid_columns", "invalid_parameters", "schema_changed", "unauthorized_object"}:
                raise
            error_code = exc.code
            await tools.call("refresh_schema_index", {})
    raise MCPToolError("Oracle query planning failed")
