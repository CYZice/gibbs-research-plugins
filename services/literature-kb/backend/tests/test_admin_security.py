from fastapi.testclient import TestClient

from app import main
from app.core.config import Settings


def test_agent_token_cannot_access_admin_routes(monkeypatch):
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: Settings(app_env="test", api_auth_token="agent-token", admin_api_token="admin-token"),
    )
    client = TestClient(main.create_app())

    response = client.get(
        "/api/v1/admin/overview",
        headers={"Authorization": "Bearer agent-token"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "ADMIN_UNAUTHORIZED"


def test_dotenv_admin_token_overrides_inherited_process_value(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("ADMIN_API_TOKEN=dotenv-token\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADMIN_API_TOKEN", "stale-process-token")

    assert Settings(app_env="test").admin_api_token == "dotenv-token"
