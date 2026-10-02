"""Keep tests independent of the user's local .env and application storage."""

import pytest


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    for key in ("SUPPORT_DB_PATH", "SUPPORT_LOG_LEVEL", "OPENAI_API_KEY",
                "CHROMA_PATH", "CHROMA_COLLECTION", "OPENAI_EMBEDDING_MODEL",
                "POLICY_CHUNK_SIZE", "POLICY_CHUNK_OVERLAP", "POLICY_MAX_DISTANCE",
                "OPENAI_CHAT_MODEL", "AGENT_TIMEOUT_SECONDS", "LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2",
                "API_HOST", "API_PORT", "SUPPORT_API_URL", "API_CHAT_TIMEOUT_SECONDS",
                "API_SESSION_TTL_SECONDS", "API_MAX_SESSIONS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
