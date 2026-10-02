"""Keep tests independent of the user's local .env and application storage."""

import pytest


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    for key in ("SUPPORT_DB_PATH", "SUPPORT_LOG_LEVEL", "OPENAI_API_KEY",
                "CHROMA_PATH", "CHROMA_COLLECTION", "OPENAI_EMBEDDING_MODEL",
                "POLICY_CHUNK_SIZE", "POLICY_CHUNK_OVERLAP", "POLICY_MAX_DISTANCE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
