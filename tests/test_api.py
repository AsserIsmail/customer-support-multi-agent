import asyncio
import time
from contextlib import asynccontextmanager

import pymupdf
import pytest
from fastapi.testclient import TestClient

from support_ai.api import APISettings, create_app
from support_ai.agent_models import ModelError
from support_ai.policies import MAX_PDF_BYTES, PolicyError


class FakeRuntime:
    def __init__(self):
        self.messages = {}
        self.forgotten = []
        self.uploads = []
        self.failure = None

    async def ask(self, question, session_id):
        if self.failure:
            raise self.failure
        self.messages.setdefault(session_id, []).append(question)
        return {"status": "answered", "answer": " | ".join(self.messages[session_id]),
                "sources": [], "route": "sql", "trace": ["supervisor", "sql", "synthesis"]}

    async def forget(self, session_id):
        self.forgotten.append(session_id)
        self.messages.pop(session_id, None)

    async def ingest(self, content, filename):
        self.uploads.append((content, filename))
        return {"status": "indexed", "source": filename, "chunks": 1, "document_id": "test"}

    def health(self):
        return {"status": "ok", "openai_key_configured": False}


@pytest.fixture
def runtime():
    return FakeRuntime()


def app_for(runtime, settings=None):
    @asynccontextmanager
    async def factory():
        yield runtime
    return create_app(factory, settings or APISettings())


@pytest.fixture
def client(runtime):
    with TestClient(app_for(runtime)) as client:
        yield client


def new_session(client):
    result = client.post("/sessions")
    assert result.status_code == 201
    body = result.json()
    return body["session_id"], {"Authorization": "Bearer " + body["token"]}


def pdf():
    with pymupdf.open() as document:
        document.new_page().insert_text((30, 30), "Refunds within 30 days of delivery.")
        return document.tobytes()


def test_sessions_are_isolated_and_deleted(client, runtime):
    first, first_headers = new_session(client)
    second, second_headers = new_session(client)
    for session, headers, text in [(first, first_headers, "Emma"), (second, second_headers, "Liam"),
                                    (first, first_headers, "Her orders")]:
        result = client.post("/chat", json={"session_id": session, "message": text}, headers=headers)
        assert result.status_code == 200
    assert result.json()["answer"] == "Emma | Her orders"
    assert runtime.messages[second] == ["Liam"]
    assert result.headers["cache-control"] == "no-store"
    assert client.delete(f"/sessions/{first}", headers=first_headers).status_code == 200
    assert first in runtime.forgotten
    assert client.post("/chat", json={"session_id": first, "message": "again"}, headers=first_headers).status_code == 401


def test_wrong_session_token_cannot_access_or_delete_other_session(client):
    first, first_headers = new_session(client)
    second, _ = new_session(client)
    assert client.post("/chat", json={"session_id": second, "message": "hello"}, headers=first_headers).status_code == 401
    assert client.delete(f"/sessions/{second}", headers=first_headers).status_code == 401
    assert client.post("/chat", json={"session_id": first, "message": "hello"}).status_code == 401


@pytest.mark.parametrize("message", ["", "   ", "x" * 4001, None])
def test_invalid_questions_are_not_echoed(client, message):
    session, headers = new_session(client)
    result = client.post("/chat", json={"session_id": session, "message": message}, headers=headers)
    assert result.status_code == 422
    assert "input" not in result.json()


def test_capacity_and_expiration(runtime):
    app = app_for(runtime, APISettings(max_sessions=1, session_ttl=1))
    with TestClient(app) as client:
        session, _ = new_session(client)
        assert client.post("/sessions").status_code == 503
        app.state.sessions[session].touched = time.monotonic() - 2
        assert client.post("/sessions").status_code == 201
        assert session in runtime.forgotten


def test_busy_session_rejects_chat_and_delete(client):
    session, headers = new_session(client)
    client.app.state.sessions[session].busy = True
    assert client.post("/chat", json={"session_id": session, "message": "hello"}, headers=headers).status_code == 409
    assert client.delete(f"/sessions/{session}", headers=headers).status_code == 409


def test_chat_timeout_releases_session(runtime):
    async def slow(*args):
        await asyncio.sleep(1)
    runtime.ask = slow
    with TestClient(app_for(runtime, APISettings(chat_timeout=0.01))) as client:
        session, headers = new_session(client)
        result = client.post("/chat", json={"session_id": session, "message": "hello"}, headers=headers)
        assert result.status_code == 504
        assert not client.app.state.sessions[session].busy


@pytest.mark.parametrize("error,status", [(ModelError("private-key"), 503), (RuntimeError("private-key"), 502)])
def test_errors_do_not_expose_secrets(client, runtime, error, status):
    runtime.failure = error
    session, headers = new_session(client)
    result = client.post("/chat", json={"session_id": session, "message": "hello"}, headers=headers)
    assert result.status_code == status
    assert "private-key" not in result.text


def test_pdf_upload(client, runtime):
    session, headers = new_session(client)
    content = pdf()
    result = client.post("/policies", params={"session_id": session}, headers=headers,
                         files={"file": ("policy.pdf", content, "application/pdf")})
    assert result.status_code == 200
    assert result.json()["status"] == "indexed"
    assert runtime.uploads == [(content, "policy.pdf")]


@pytest.mark.parametrize("filename,content", [("file.txt", b"text"), ("file.pdf", b"broken")])
def test_invalid_upload_never_reaches_embedding(client, runtime, filename, content):
    session, headers = new_session(client)
    result = client.post("/policies", params={"session_id": session}, headers=headers,
                         files={"file": (filename, content)})
    assert result.status_code == 400
    assert not runtime.uploads


def test_large_upload_and_body_rejected(client, runtime):
    session, headers = new_session(client)
    result = client.post("/policies", params={"session_id": session}, headers=headers,
                         files={"file": ("big.pdf", b"a" * (MAX_PDF_BYTES + 1))})
    assert result.status_code == 413
    assert not runtime.uploads
    assert client.post("/chat", content=b"x" * 32769).status_code == 413


def test_health_and_openapi(client):
    assert client.get("/health").json()["status"] == "ok"
    assert set(client.get("/openapi.json").json()["paths"]) == {
        "/health", "/sessions", "/sessions/{session_id}", "/chat", "/policies"}


def test_chunked_oversized_body_rejected(client):
    result = client.post("/chat", content=iter([b"x" * 20000, b"y" * 20000]))
    assert result.status_code == 413


def test_busy_upload_does_not_embed(client, runtime):
    session, headers = new_session(client)
    client.app.state.sessions[session].busy = True
    result = client.post("/policies", params={"session_id": session}, headers=headers,
                         files={"file": ("policy.pdf", pdf())})
    assert result.status_code == 409
    assert not runtime.uploads


def test_real_lifespan_mcp_and_graph(tmp_path, monkeypatch):
    from support_ai.seed import seed_database
    from support_ai.agents import SupportAssistant
    from support_ai.mcp_client import connect_support_tools
    from support_ai.web_runtime import Runtime
    from support_ai.agent_models import RoutePlan, SQLPlan, AnswerDraft, AnswerBlock
    database = tmp_path / "support.db"
    seed_database(database)
    monkeypatch.setenv("SUPPORT_DB_PATH", str(database))

    class Model:
        async def decide(self, role, schema, payload):
            if role == "supervisor":
                return RoutePlan(route="sql", request=payload["question"], clarification="")
            if role == "sql":
                return SQLPlan(customer_query="Emma Wilson", customer_id=None, use_previous_customer=False,
                    orders=True, tickets=False, order_status=None, ticket_status=None, record_limit=1)
            assert payload["evidence"]["orders"]["data"][0]["id"] == 103
            return AnswerDraft(status="answered", blocks=[AnswerBlock(text="Monitor, $299.00.", evidence_ids=["orders"])])

    @asynccontextmanager
    async def factory():
        async with connect_support_tools() as tools:
            runtime = Runtime(tools)
            runtime.assistant = SupportAssistant(tools, Model())
            yield runtime

    with TestClient(create_app(factory, APISettings())) as client:
        session, headers = new_session(client)
        result = client.post("/chat", headers=headers, json={"session_id": session, "message": "Emma's latest order"})
        assert result.status_code == 200
        assert result.json()["trace"] == ["supervisor", "sql", "synthesis"]
        assert result.json()["answer"] == "Monitor, $299.00."


def test_upload_persists_and_deduplicates_with_real_chroma(tmp_path, monkeypatch):
    from support_ai import web_runtime
    from support_ai.config import PolicySettings
    from support_ai.policies import PolicyIndex
    class Embedder:
        model = "test-vectors"
        def embed(self, texts):
            return [[1.0, 0.5] for _ in texts]
    settings = PolicySettings(tmp_path / "chroma", embedding_model="test-vectors")
    monkeypatch.setattr(web_runtime, "PolicyIndex", lambda ignored: PolicyIndex(settings, Embedder()))
    runtime = web_runtime.Runtime(None)
    with TestClient(app_for(runtime)) as client:
        session, headers = new_session(client)
        content = pdf()
        for expected in ("indexed", "duplicate"):
            result = client.post("/policies", params={"session_id": session}, headers=headers,
                                 files={"file": ("test.pdf", content, "application/pdf")})
            assert result.status_code == 200
            assert result.json()["status"] == expected
        index = PolicyIndex(settings, Embedder(), create=False)
        assert index.collection.count() == 1
        assert index.search("refund")[0]["citation"] == "[test.pdf, p. 1]"
