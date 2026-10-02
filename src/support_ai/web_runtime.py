"""Application resources owned by FastAPI's lifespan task."""

import os
from contextlib import asynccontextmanager

import anyio

from support_ai.agent_models import OpenAIDecisions
from support_ai.agents import SupportAssistant
from support_ai.config import Settings, PolicySettings, configure_logging
from support_ai.mcp_client import connect_support_tools
from support_ai.policies import PolicyIndex


class Runtime:
    def __init__(self, tools):
        self.tools = tools
        self.assistant = None

    async def ask(self, question: str, session_id: str) -> dict:
        if self.assistant is None:
            self.assistant = SupportAssistant(self.tools, OpenAIDecisions())
        return await self.assistant.ask(question, thread_id=session_id)

    async def forget(self, session_id: str) -> None:
        if self.assistant:
            await self.assistant.forget(session_id)

    async def ingest(self, content: bytes, filename: str) -> dict:
        def ingest():
            return PolicyIndex(PolicySettings.from_env()).ingest_bytes(content, filename)
        # The call is shielded by AnyIO by default: do not release the writer lock
        # while an embedding/upsert thread is still running after disconnect.
        return await anyio.to_thread.run_sync(ingest)

    def health(self) -> dict:
        return {"status": "ok", "customer_database_present": Settings.from_env().db_path.is_file(),
                "policy_index_present": (PolicySettings.from_env().chroma_path / "chroma.sqlite3").is_file(),
                "openai_key_configured": bool(os.getenv("OPENAI_API_KEY", "").strip())}


@asynccontextmanager
async def runtime_context():
    configure_logging(Settings.from_env().log_level)
    # Enter and exit the SDK context in the same lifespan task. Request tasks
    # may make concurrent calls through the initialized client session.
    async with connect_support_tools() as tools:
        yield Runtime(tools)
