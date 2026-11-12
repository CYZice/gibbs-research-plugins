from fastapi.testclient import TestClient

from app.main import app


def test_workflow_requests_reject_removed_or_cross_workflow_fields() -> None:
    client = TestClient(app)

    cases = [
        ("/api/v1/evidence/packs", {"question": "legacy question"}),
        ("/api/v1/evidence/packs", {"claims": [{"text": "A claim."}], "evidence_ids": ["ev_1"]}),
        ("/api/v1/citations/ground", {"draft": "A draft.", "claims": [{"text": "Caller claim."}]}),
        ("/api/v1/citations/ground", {"draft": "A draft.", "evidence_ids": ["ev_1"]}),
    ]

    for path, body in cases:
        response = client.post(path, json=body)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_openapi_exposes_async_workflow_contract() -> None:
    paths = app.openapi()["paths"]

    assert paths["/api/v1/evidence/packs"]["post"]["responses"]["202"]
    assert paths["/api/v1/citations/ground"]["post"]["responses"]["202"]
    workflow_response = app.openapi()["components"]["schemas"]["WorkflowRunResponse"]["properties"]
    assert "details" in workflow_response
    assert "retrying" in workflow_response["status"]["enum"]
    assert paths["/api/v1/workflows/{run_id}"]["get"]
