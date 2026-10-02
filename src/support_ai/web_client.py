"""Synchronous HTTP boundary used by Streamlit; contains no model or data access."""

import os
from pathlib import Path

import httpx
from dotenv import load_dotenv


class APIError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def request(method: str, path: str, *, credentials: dict | None = None, **kwargs) -> dict:
    load_dotenv(Path.cwd() / ".env", override=False)
    url = os.getenv("SUPPORT_API_URL", "http://127.0.0.1:8000").rstrip("/")
    headers = {"Authorization": f"Bearer {credentials['token']}"} if credentials else {}
    try:
        timeout = max(310, float(os.getenv("API_CHAT_TIMEOUT_SECONDS", "300")) + 10)
        result = httpx.request(method, url + path, headers=headers, timeout=timeout, **kwargs)
    except httpx.TimeoutException:
        raise APIError("The request timed out. The server may still be working; wait before retrying.") from None
    except httpx.HTTPError:
        raise APIError("Cannot reach the support API. Start it and check SUPPORT_API_URL.") from None
    try:
        data = result.json()
    except ValueError:
        raise APIError("The API returned an unreadable response.") from None
    if result.is_error:
        detail = data.get("detail") if isinstance(data, dict) else None
        raise APIError(detail if isinstance(detail, str) else "The API rejected the request.", result.status_code)
    if not isinstance(data, dict):
        raise APIError("The API returned an unexpected response.")
    return data
