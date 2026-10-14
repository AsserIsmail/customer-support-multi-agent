"""Integration tests use real SDK JSON-RPC sessions and subprocess transports."""

import asyncio
import os
import sys

import pymupdf
import pytest
from mcp.client import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from support_ai.config import PolicySettings
from support_ai.mcp_client import MCPToolError, SupportTools, connect_support_tools
from support_ai.policies import PolicyIndex, PolicyError
from support_ai.seed import seed_database


TOOLS = {"customer_lookup", "customer_profile", "order_lookup", "support_tickets", "policy_search"}


@pytest.fixture
def server_env(tmp_path):
    database = tmp_path / "support.db"
    seed_database(database)
    env = dict(os.environ)
    env.update(SUPPORT_DB_PATH=str(database), CHROMA_PATH=str(tmp_path / "chroma"),
               CHROMA_COLLECTION="test_policies", OPENAI_API_KEY="")
    return env


def test_stdio_discovery_customer_history_validation_and_no_writes(server_env, tmp_path):
    from pathlib import Path
    path = Path(server_env["SUPPORT_DB_PATH"])
    before = path.read_bytes()

    async def scenario():
        async with connect_support_tools(cwd=tmp_path, env=server_env, timeout=20) as tools:
            discovered = await tools.list_tools()
            assert {tool.name for tool in discovered} == TOOLS
            for tool in discovered:
                assert tool.annotations.read_only_hint is True
                assert tool.annotations.destructive_hint is False
            orders_schema = next(tool.input_schema for tool in discovered if tool.name == "order_lookup")
            assert orders_schema["properties"]["limit"]["maximum"] == 100
            matches = await tools.call("customer_lookup", {"query": "Emma"})
            assert matches["status"] == "ambiguous"
            assert len(matches["customers"]) == 2
            assert (await tools.call("customer_lookup", {"query": "Emma Wilson"}))["status"] == "found"
            assert (await tools.call("customer_lookup", {"query": "' OR 1=1 --"}))["status"] == "not_found"
            profile = await tools.call("customer_profile", {"customer_id": 1})
            assert profile["customer"]["name"] == "Emma Wilson"
            first = await tools.call("order_lookup", {"customer_id": 1, "limit": 2})
            assert [row["id"] for row in first["orders"]] == [103, 102]
            assert first["next_offset"] == 2
            last = await tools.call("order_lookup", {"customer_id": 1, "limit": 2, "offset": 2})
            assert [row["id"] for row in last["orders"]] == [101]
            assert last["next_offset"] is None
            tickets = await tools.call("support_tickets", {"customer_id": 1})
            assert len(tickets["tickets"]) == 3
            assert all(row["customer_id"] == 1 for row in tickets["tickets"])
            assert (await tools.call("customer_profile", {"customer_id": 999}))["status"] == "not_found"
            assert (await tools.call("order_lookup", {"customer_id": 999}))["status"] == "customer_not_found"
            assert (await tools.call("support_tickets", {"customer_id": 1, "offset": 100}))["status"] == "empty"
            for name, arguments in [
                ("order_lookup", {"customer_id": "1 OR 1=1"}),
                ("order_lookup", {"customer_id": True}),
                ("order_lookup", {"customer_id": 0}),
                ("order_lookup", {"customer_id": 1, "limit": 101}),
                ("support_tickets", {"customer_id": 1, "offset": -1}),
                ("customer_lookup", {"query": "   "}),
                ("policy_search", {"query": "refund", "limit": 21}),
                ("execute_sql", {"sql": "DELETE FROM customers"}),
                ("ingest_pdf", {"path": "anything.pdf"}),
            ]:
                result = await tools.session.call_tool(name, arguments)
                assert result.is_error, (name, arguments)
            with pytest.raises(MCPToolError, match="missing"):
                await tools.call("policy_search", {"query": "refund"})
            # A policy failure must not take down customer tools.
            assert (await tools.call("customer_profile", {"customer_id": 1}))["status"] == "found"

    asyncio.run(scenario())
    assert path.read_bytes() == before
    assert not (tmp_path / "chroma").exists()


def test_unavailable_database_is_mcp_error(server_env, tmp_path):
    server_env["SUPPORT_DB_PATH"] = str(tmp_path / "missing.db")

    async def scenario():
        async with connect_support_tools(cwd=tmp_path, env=server_env, timeout=20) as tools:
            assert {tool.name for tool in await tools.list_tools()} == TOOLS
            with pytest.raises(MCPToolError, match="database is unavailable"):
                await tools.call("customer_lookup", {"query": "Emma"})
    asyncio.run(scenario())
    assert not (tmp_path / "missing.db").exists()


class TestEmbedder:
    model = "test-vectors"

    def embed(self, texts):
        return [[1.0, 0.0] if "refund" in text.lower() else [0.0, 1.0] for text in texts]


def test_policy_search_through_stdio_with_real_chroma(server_env, tmp_path):
    from pathlib import Path
    settings = PolicySettings(Path(server_env["CHROMA_PATH"]), collection="test_policies",
                              embedding_model="test-vectors")
    index = PolicyIndex(settings, TestEmbedder())
    with pymupdf.open() as doc:
        doc.new_page().insert_text((30, 30), "Refund requests accepted within 30 days of delivery.")
        index.ingest_bytes(doc.tobytes(), "returns.pdf")
    # Test-only launcher injects deterministic embeddings; production has no fake mode.
    launcher = tmp_path / "test_server.py"
    launcher.write_text('''
from support_ai.config import Settings, PolicySettings, configure_logging
from support_ai.policies import PolicyIndex
from support_ai.mcp_server import create_server
class Embedder:
    model = "test-vectors"
    def embed(self, texts):
        return [[1.0, 0.0] if "refund" in text.lower() else [0.0, 1.0] for text in texts]
settings = Settings.from_env()
configure_logging(settings.log_level)
create_server(settings, lambda: PolicyIndex(PolicySettings.from_env(), Embedder(), create=False)).run()
''', encoding="utf-8")
    server_env["OPENAI_EMBEDDING_MODEL"] = "test-vectors"

    async def scenario():
        parameters = StdioServerParameters(command=sys.executable, args=[str(launcher)],
                                           cwd=str(tmp_path), env=server_env)
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=20) as session:
                await session.initialize()
                tools = SupportTools(session)
                result = await tools.call("policy_search", {"query": "refund"})
                assert result["status"] == "found"
                passage = result["passages"][0]
                assert passage["citation"] == "[returns.pdf, p. 1]"
                assert "30 days" in passage["text"]
                assert passage["document_id"]
                assert (await tools.call("policy_search", {"query": "unrelated"}))["status"] == "no_evidence"
                assert (await tools.call("order_lookup", {"customer_id": 1}))["status"] == "found"
    asyncio.run(scenario())
    assert index.collection.count() == 1


def test_read_existing_policy_mode_does_not_create_collections(tmp_path):
    settings = PolicySettings(tmp_path / "chroma", embedding_model="test-vectors")
    with pytest.raises(PolicyError, match="missing"):
        PolicyIndex(settings, TestEmbedder(), create=False)
    assert not settings.chroma_path.exists()
    index = PolicyIndex(settings, TestEmbedder())
    from dataclasses import replace
    with pytest.raises(PolicyError):
        PolicyIndex(replace(settings, collection="nonexistent"), TestEmbedder(), create=False)
    assert [c.name for c in index.client.list_collections()] == [settings.collection]


def test_unexpected_tool_error_does_not_leak_secrets(caplog):
    from support_ai.mcp_server import _result
    def failure():
        raise RuntimeError("private-api-key-or-document")
    result = _result(failure)
    assert result.is_error
    assert "private-api-key-or-document" not in result.model_dump_json() + caplog.text


def test_schema_tools_over_stdio_with_injected_metadata(tmp_path):
    launcher = tmp_path / "schema_server.py"
    launcher.write_text('''
from pathlib import Path
from support_ai.config import Settings, OracleSettings
from support_ai.mcp_server import create_server
from support_ai.schema_catalog import SchemaCatalog
settings = OracleSettings("reader", "unused-password", "unused-host/service", "SUPPORT_OWNER")
calls = 0
def load():
    global calls
    calls += 1
    if calls >= 3:
        raise RuntimeError("private-driver-error")
    return {"ORDERS": {"owner": "SUPPORT_OWNER", "table": "ORDERS", "comments": "Purchase history",
        "columns": [{"column_name": "ID", "data_type": "NUMBER", "comments": None}],
        "constraints": [], "indexes": [], "relationships": []}}
catalog = SchemaCatalog(settings, loader=load)
create_server(Settings(Path("unused.db"), db_backend="oracle", oracle=settings),
              schema_factory=lambda: catalog).run()
''', encoding="utf-8")

    async def run():
        parameters = StdioServerParameters(command=sys.executable, args=[str(launcher)], cwd=str(tmp_path), env=dict(os.environ))
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=20) as session:
                await session.initialize()
                tools = SupportTools(session)
                discovered = {tool.name: tool for tool in await tools.list_tools()}
                schema_tools = {"list_tables", "describe_table", "get_relationships", "search_schema", "refresh_schema_index"}
                assert set(discovered) == TOOLS | schema_tools | {"execute_readonly_query"}
                rejected = await session.call_tool("execute_readonly_query", {"sql": "DELETE FROM orders"})
                assert rejected.is_error and rejected.structured_content["code"] == "invalid_sql"
                assert discovered["refresh_schema_index"].annotations.read_only_hint is False
                assert all(discovered[name].annotations.read_only_hint is True for name in schema_tools - {"refresh_schema_index"})
                listing = await tools.call("list_tables", {})
                assert listing["tables"][0]["table"] == "ORDERS"
                assert (await tools.call("describe_table", {"table": "orders"}))["table"]["columns"][0]["column_name"] == "ID"
                assert (await tools.call("get_relationships", {}))["relationships"] == []
                assert (await tools.call("search_schema", {"query": "purchase"}))["matches"][0]["kind"] == "table"
                assert (await tools.call("refresh_schema_index", {}))["schema_version"] == listing["schema_version"]
                for name, args in [("describe_table", {"table": "SYS.USERS"}),
                                   ("get_relationships", {"table": "SECRETS"}),
                                   ("search_schema", {"query": "id", "limit": True}),
                                   ("search_schema", {"query": " "})]:
                    assert (await session.call_tool(name, args)).is_error
                for name in ("refresh_schema_index", "list_tables"):
                    result = await session.call_tool(name, {})
                    assert result.is_error
                    assert "private-driver-error" not in result.model_dump_json()
                    assert result.structured_content["status"] == "error"
    asyncio.run(run())
