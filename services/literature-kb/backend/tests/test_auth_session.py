from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main
from app.core.config import Settings


def test_login_cookie_allows_protected_requests(monkeypatch):
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: Settings(app_env="test", api_auth_token="login-token"),
    )
    app = main.create_app()

    @app.get("/session-check")
    async def session_check():
        return {"ok": True}

    client = TestClient(app)
    assert client.get("/session-check").status_code == 401
    assert client.post("/auth/login", json={"token": "wrong"}).status_code == 401
    assert client.post("/auth/login", json={"token": "login-token"}).status_code == 200
    assert client.get("/auth/session").json() == {"authenticated": True}
    assert client.get("/session-check").json() == {"ok": True}
    assert client.post("/auth/logout").status_code == 200
    assert client.get("/session-check").status_code == 401
