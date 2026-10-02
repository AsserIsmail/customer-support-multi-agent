from pathlib import Path

from streamlit.testing.v1 import AppTest

from support_ai import web_client

APP = Path(__file__).resolve().parents[1] / "streamlit_app.py"


def test_chat_displays_answer_and_retains_credentials(monkeypatch):
    calls = []
    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/sessions":
            return {"session_id": "a" * 32, "token": "private-session-token"}
        return {"status": "answered", "answer": "Returns within 30 days. [returns.pdf, p. 1]",
                "sources": [{"citation": "[returns.pdf, p. 1]"}]}
    monkeypatch.setattr(web_client, "request", request)
    app = AppTest.from_file(str(APP)).run()
    assert not app.exception
    app.chat_input[0].set_value("What is the refund policy?").run()
    assert not app.exception
    assert len(app.chat_message) == 2
    assert "Returns within 30 days" in app.chat_message[1].markdown[0].value
    app.chat_input[0].set_value("What about damaged items?").run()
    assert len(app.chat_message) == 4
    assert sum(path == "/sessions" for _, path, _ in calls) == 1
    assert calls[-1][2]["credentials"]["token"] == "private-session-token"
    assert all("private-session-token" not in item.value for item in app.markdown)


def test_new_conversation_deletes_old_context(monkeypatch):
    calls = []
    def request(method, path, **kwargs):
        calls.append((method, path))
        if path == "/sessions":
            return {"session_id": "a" * 32, "token": "token"}
        return {"status": "answered", "answer": "Hello", "sources": []}
    monkeypatch.setattr(web_client, "request", request)
    app = AppTest.from_file(str(APP)).run()
    app.chat_input[0].set_value("hello").run()
    app.button[0].click().run()
    assert not app.exception
    assert not app.chat_message
    assert ("DELETE", "/sessions/" + "a" * 32) in calls


def test_unavailable_backend_has_useful_error(monkeypatch):
    def request(*args, **kwargs):
        raise web_client.APIError("Cannot reach the support API.")
    monkeypatch.setattr(web_client, "request", request)
    app = AppTest.from_file(str(APP)).run()
    app.chat_input[0].set_value("hello").run()
    assert not app.exception
    assert "Cannot reach" in app.error[0].value


def test_expired_session_requires_new_conversation(monkeypatch):
    def request(method, path, **kwargs):
        if path == "/sessions":
            return {"session_id": "a" * 32, "token": "token"}
        raise web_client.APIError("Conversation expired", 401)
    monkeypatch.setattr(web_client, "request", request)
    app = AppTest.from_file(str(APP)).run()
    app.chat_input[0].set_value("hello").run()
    assert not app.exception
    assert app.chat_input[0].disabled
    assert "expired" in app.warning[0].value
    app.button[0].click().run()
    assert not app.chat_input[0].disabled
