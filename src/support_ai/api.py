"""Local FastAPI service for session-isolated chat and policy PDF uploads."""

import asyncio
import logging
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Literal

import anyio
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from support_ai.agent_models import ModelError
from support_ai.config import Settings, PolicySettings
from support_ai.policies import MAX_PDF_BYTES, PolicyError, extract_chunks
from support_ai.web_runtime import runtime_context

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class APISettings:
    chat_timeout: float = 300
    session_ttl: float = 3600
    max_sessions: int = 100

    @classmethod
    def from_env(cls):
        Settings.from_env()
        result = cls(float(os.getenv("API_CHAT_TIMEOUT_SECONDS", "300")),
                     float(os.getenv("API_SESSION_TTL_SECONDS", "3600")),
                     int(os.getenv("API_MAX_SESSIONS", "100")))
        if not 1 <= result.chat_timeout <= 900 or not 1 <= result.session_ttl <= 86400 or not 1 <= result.max_sessions <= 1000:
            raise ValueError("Invalid API timeout, session TTL, or session capacity")
        return result


@dataclass
class Session:
    token: str
    touched: float
    busy: bool = False


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    session_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    message: str = Field(min_length=1, max_length=4000)


class Source(BaseModel):
    chunk_id: str
    source: str
    page: int
    citation: str


class ChatResponse(BaseModel):
    status: Literal["answered", "needs_clarification", "insufficient_evidence", "error"]
    answer: str
    sources: list[Source]
    route: str
    trace: list[str]


class BodyLimitMiddleware:
    """Bound actual bytes before multipart/JSON parsing, including chunked requests."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH"):
            return await self.app(scope, receive, send)
        limit = MAX_PDF_BYTES + 65536 if scope["path"] == "/policies" else 32768
        parts, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            part = message.get("body", b"")
            size += len(part)
            if size > limit:
                result = JSONResponse(status_code=413, content={"detail": "Request body is too large."},
                                      headers={"Cache-Control": "no-store"})
                return await result(scope, receive, send)
            parts.append(part)
            if not message.get("more_body", False):
                break
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": b"".join(parts), "more_body": False}
            return await receive()
        await self.app(scope, replay, send)


def create_app(runtime_factory=runtime_context, settings: APISettings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        app.state.settings = settings or APISettings.from_env()
        app.state.sessions = {}
        app.state.session_lock = asyncio.Lock()
        app.state.upload_lock = asyncio.Lock()
        async with runtime_factory() as runtime:
            app.state.runtime = runtime
            yield

    app = FastAPI(title="Customer Support Assistant", version="0.1.0", lifespan=lifespan)
    app.add_middleware(BodyLimitMiddleware)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # FastAPI's default payload echoes invalid input, which may contain secrets.
        return JSONResponse(status_code=422, content={"detail": "Invalid request. Check required fields and input limits."})

    @app.middleware("http")
    async def response_headers(request: Request, call_next):
        # Reject oversized declared bodies before multipart parsing. The file itself
        # is also checked after reading, including when Content-Length is absent.
        limit = MAX_PDF_BYTES + 65536 if request.url.path == "/policies" else 32768
        length = request.headers.get("content-length")
        if length:
            try:
                if int(length) < 0 or int(length) > limit:
                    return JSONResponse(status_code=413, content={"detail": "Request body is too large."})
            except ValueError:
                return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length."})
        result = await call_next(request)
        result.headers["Cache-Control"] = "no-store"
        result.headers["X-Content-Type-Options"] = "nosniff"
        return result

    async def purge_expired():
        now = time.monotonic()
        stale = [key for key, session in app.state.sessions.items()
                 if not session.busy and now - session.touched > app.state.settings.session_ttl]
        for key in stale:
            await app.state.runtime.forget(key)
            del app.state.sessions[key]

    async def require_session(session_id: str, authorization: str | None) -> Session:
        async with app.state.session_lock:
            await purge_expired()
            session = app.state.sessions.get(session_id)
            token = authorization.removeprefix("Bearer ") if authorization and authorization.startswith("Bearer ") else ""
            if session is None or not secrets.compare_digest(session.token, token):
                raise HTTPException(401, "Conversation expired or credentials are invalid. Start a new conversation.")
            session.touched = time.monotonic()
            return session

    @app.get("/health")
    async def health():
        return app.state.runtime.health()

    @app.post("/sessions", status_code=201)
    async def new_session():
        async with app.state.session_lock:
            await purge_expired()
            if len(app.state.sessions) >= app.state.settings.max_sessions:
                raise HTTPException(503, "Conversation capacity reached. Close an existing conversation or retry later.")
            session_id, token = uuid.uuid4().hex, secrets.token_urlsafe(32)
            app.state.sessions[session_id] = Session(token, time.monotonic())
            return {"session_id": session_id, "token": token}

    @app.delete("/sessions/{session_id}")
    async def delete_session(session_id: str, authorization: Annotated[str | None, Header()] = None):
        session = await require_session(session_id, authorization)
        if session.busy:
            raise HTTPException(409, "This conversation has a request in progress.")
        session.busy = True
        try:
            await app.state.runtime.forget(session_id)
            app.state.sessions.pop(session_id, None)
        finally:
            session.busy = False
        return {"status": "deleted"}

    @app.post("/chat", response_model=ChatResponse)
    async def chat(body: ChatRequest, authorization: Annotated[str | None, Header()] = None):
        session = await require_session(body.session_id, authorization)
        if session.busy:
            raise HTTPException(409, "This conversation has a request in progress.")
        session.busy = True
        try:
            return await asyncio.wait_for(app.state.runtime.ask(body.message, body.session_id),
                                          timeout=app.state.settings.chat_timeout)
        except asyncio.TimeoutError:
            raise HTTPException(504, "The answer took too long. Please retry with a narrower question.") from None
        except ModelError:
            raise HTTPException(503, "Chat is not configured. Set OPENAI_API_KEY locally and restart the API.") from None
        except Exception as exc:
            logger.error("Chat request failed (%s)", type(exc).__name__)
            raise HTTPException(502, "The assistant could not complete the request. Please retry.") from None
        finally:
            session.busy = False
            session.touched = time.monotonic()

    @app.post("/policies")
    async def upload_policy(session_id: str, file: UploadFile,
                            authorization: Annotated[str | None, Header()] = None):
        session = await require_session(session_id, authorization)
        if session.busy:
            raise HTTPException(409, "This conversation has a request in progress.")
        if app.state.upload_lock.locked():
            raise HTTPException(409, "Another policy upload is in progress. Please retry shortly.")
        session.busy = True
        try:
            if not file.filename or not file.filename.lower().endswith(".pdf"):
                raise HTTPException(400, "Choose a PDF file.")
            content = await file.read(MAX_PDF_BYTES + 1)
            if len(content) > MAX_PDF_BYTES:
                raise HTTPException(413, "PDF must be no larger than 20 MiB.")
            try:
                await anyio.to_thread.run_sync(lambda: extract_chunks(content, PolicySettings.from_env()))
            except PolicyError as exc:
                raise HTTPException(400, str(exc)) from None
            async with app.state.upload_lock:
                try:
                    return await app.state.runtime.ingest(content, file.filename)
                except PolicyError as exc:
                    raise HTTPException(400, str(exc)) from None
                except Exception as exc:
                    logger.error("Upload request failed (%s)", type(exc).__name__)
                    raise HTTPException(502, "Policy upload failed. Check configuration and try again.") from None
        finally:
            await file.close()
            session.busy = False
            session.touched = time.monotonic()

    return app


app = create_app()


def main() -> None:
    Settings.from_env()
    uvicorn.run("support_ai.api:app", host=os.getenv("API_HOST", "127.0.0.1"),
                port=int(os.getenv("API_PORT", "8000")), workers=1, access_log=False)


if __name__ == "__main__":
    main()
