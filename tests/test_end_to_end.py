"""Real MCP, database, PDF extraction and Chroma; deterministic inference only."""

import asyncio
import os
import sys

import pymupdf
import pytest
from mcp.client import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from support_ai.agent_models import RAGPlan, OracleQueryPlan, QueryBind
from support_ai.agents import SupportAssistant
from support_ai.config import PolicySettings
from support_ai.mcp_client import SupportTools
from support_ai.policies import PolicyIndex
from support_ai.seed import seed_database
from test_agents import ScriptedModel, route, sql, draft
from test_oracle_integration import LIVE, migrated  # fixture used only by Oracle parameter


class Embedder:
    model = "test-vectors"

    def embed(self, texts):
        return [[1.0, 0.0] if "refund" in text.lower() else [0.0, 1.0] for text in texts]


@pytest.mark.parametrize("backend", ["sqlite", pytest.param("oracle", marks=pytest.mark.skipif(
    LIVE["SUPPORT_ORACLE_TESTS"] != "1", reason="Dedicated Oracle environment not enabled"))])
def test_combined_conversation_through_real_storage_and_stdio(backend, tmp_path, request):
    database = tmp_path / "support.db"
    seed_database(database)
    env = dict(os.environ, SUPPORT_DB_BACKEND=backend, SUPPORT_DB_PATH=str(database),
               CHROMA_PATH=str(tmp_path / "chroma"), CHROMA_COLLECTION="test_policies",
               OPENAI_EMBEDDING_MODEL="test-vectors")
    if backend == "oracle":
        oracle = request.getfixturevalue("migrated")
        env.update(SUPPORT_ORACLE_USER=oracle.user, SUPPORT_ORACLE_PASSWORD=oracle.password,
                   SUPPORT_ORACLE_DSN=oracle.dsn, SUPPORT_ORACLE_SCHEMA=oracle.schema)
    settings = PolicySettings(tmp_path / "chroma", collection="test_policies", embedding_model="test-vectors")
    index = PolicyIndex(settings, Embedder())
    with pymupdf.open() as doc:
        doc.new_page().insert_text((30, 30), "Refund requests accepted within 30 days of delivery.")
        index.ingest_bytes(doc.tobytes(), "returns.pdf")
    passage = index.search("refund", limit=1)[0]
    policy_id = passage["chunk_id"]
    launcher = tmp_path / "verification_server.py"
    launcher.write_text("""
from support_ai.config import Settings, PolicySettings, configure_logging
from support_ai.policies import PolicyIndex
from support_ai.mcp_server import create_server
class Embedder:
    model = 'test-vectors'
    def embed(self, texts):
        return [[1.0, 0.0] if 'refund' in text.lower() else [0.0, 1.0] for text in texts]
settings = Settings.from_env()
configure_logging(settings.log_level)
create_server(settings, lambda: PolicyIndex(PolicySettings.from_env(), Embedder(), create=False)).run()
""", encoding="utf-8")

    def oracle_query(column="id", customer_id=1):
        return ("oracle_sql", OracleQueryPlan(
            sql=f"SELECT {column}, status FROM orders WHERE customer_id = :customer_id ORDER BY id DESC",
            parameters=[QueryBind(name="customer_id", value=customer_id)], row_limit=1, clarification=""))

    evidence_id = "query" if backend == "oracle" else "orders"
    steps = [route("both"), sql("Emma", orders=True),
             route("both", "Can Emma Wilson return her latest order?"), sql(orders=True, record_limit=1)]
    if backend == "oracle":
        # Exercise real SQL validation failure and refresh/replan over MCP.
        steps += [oracle_query("missing_column"), oracle_query()]
    steps += [("rag", RAGPlan(query="refund eligibility")),
              draft("The latest order is shipped. Refund requests are accepted within 30 days of delivery. "
                    "When was it delivered?", [evidence_id, policy_id], "needs_clarification"),
              route("sql", "Show Emma Wilson's latest order"),
              sql(None, use_previous_customer=True, orders=True, record_limit=1)]
    if backend == "oracle":
        steps += [oracle_query()]
    steps += [draft("The latest order is shipped.", [evidence_id]),
              route("rag", "What is the refund policy?"), ("rag", RAGPlan(query="refund policy")),
              draft("Refund requests are accepted within 30 days of delivery.", [policy_id])]
    model = ScriptedModel(*steps)

    async def run():
        params = StdioServerParameters(command=sys.executable, args=[str(launcher)], cwd=str(tmp_path), env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=60) as session:
                await session.initialize()
                tools = SupportTools(session)
                assistant = SupportAssistant(tools, model)
                ambiguous = await assistant.ask("Can Emma return her latest order?", thread_id="demo")
                assert ambiguous["status"] == "needs_clarification"
                assert "Emma Wilson" in ambiguous["answer"] and "Emma Chen" in ambiguous["answer"]
                assert ambiguous["trace"] == ["supervisor", "sql"]
                combined = await assistant.ask("I mean Emma Wilson", thread_id="demo")
                assert combined["status"] == "needs_clarification"
                assert combined["trace"] == ["supervisor", "sql", "rag", "synthesis"]
                assert combined["sources"][0]["citation"] == "[returns.pdf, p. 1]"
                assert "[returns.pdf, p. 1]" in combined["answer"]
                synthesis = model.payloads[-1][1]["evidence"]
                assert "30 days" in synthesis[policy_id]["data"]["text"]
                if backend == "oracle":
                    assert synthesis["query"]["data"]["rows"] == [[103, "shipped"]]
                    assert "partial" in combined["answer"]
                    plans = [p for role, p in model.payloads if role == "oracle_sql"]
                    assert len(plans) == 2 and plans[1]["error_code"] == "invalid_sql"
                    assert plans[1]["schema"]["relationships"]
                else:
                    assert synthesis["orders"]["data"][0]["id"] == 103
                followup = await assistant.ask("Show her latest order", thread_id="demo")
                assert followup["status"] == "answered"
                assert not followup["sources"]
                general = await assistant.ask("What is the refund policy?", thread_id="demo")
                assert general["trace"] == ["supervisor", "rag", "synthesis"]
                assert all(item["kind"] == "policy" for item in model.payloads[-1][1]["evidence"].values())
                assert not model.steps
                # A rejected mutation must leave the real database usable.
                if backend == "oracle":
                    rejected = await session.call_tool("execute_readonly_query", {"sql": "DELETE FROM orders"})
                    assert rejected.is_error
                assert (await tools.call("order_lookup", {"customer_id": 1}))["orders"][0]["id"] == 103
    asyncio.run(run())
