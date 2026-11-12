from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import app, create_app


def test_every_v1_operation_has_documented_success_and_runtime_errors() -> None:
    schema = app.openapi()
    for path, methods in schema["paths"].items():
        if not path.startswith("/api/v1/"):
            continue
        for method, operation in methods.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            responses = operation["responses"]
            success = responses.get("200") or responses.get("201") or responses.get("202")
            assert success and success.get("content"), (
                f"{method.upper()} {path} lacks success schema"
            )
            assert "422" not in responses
            assert operation["summary"]
            assert operation["description"]
            assert operation["operationId"]
            assert operation["tags"]
            expected_security = (
                [{"adminBearerAuth": []}]
                if path.startswith("/api/v1/admin/")
                else [{"bearerAuth": []}]
            )
            assert operation["security"] == expected_security
            assert responses["401"]["content"]["application/json"]["schema"]["$ref"].endswith(
                "/ErrorResponse"
            )


def test_openapi_explains_evidence_search_contract() -> None:
    schema = app.openapi()
    request = schema["components"]["schemas"]["EvidenceSearchRequest"]
    scope = schema["components"]["schemas"]["EvidenceScope"]
    pagination = schema["components"]["schemas"]["Pagination"]
    search = schema["paths"]["/api/v1/evidence/search"]["post"]

    assert "candidate pool" in request["description"]
    assert "before retrieval" in scope["properties"]["paper_ids"]["description"]
    assert "candidate/result set" in pagination["description"]
    assert search["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/EvidenceSearchResponse"
    )
    assert "verify" in search["description"].lower()


def test_evidence_scope_owns_paper_ids_not_filters() -> None:
    schema = app.openapi()["components"]["schemas"]

    assert "paper_ids" in schema["EvidenceScope"]["properties"]
    assert "paper_ids" in schema["PaperFilters"]["properties"]
    assert schema["EvidenceScope"]["properties"]["paper_ids"]["maxItems"] == 5000


def test_paper_pdf_and_scoped_paper_search_are_public_contracts() -> None:
    schema = app.openapi()

    assert "/api/v1/papers/{paper_id}/pdf" in schema["paths"]
    paper_ids = schema["components"]["schemas"]["PaperFilters"]["properties"][
        "paper_ids"
    ]
    assert paper_ids["maxItems"] == 5000
    assert "before lexical or semantic retrieval" in paper_ids["description"]


def test_pack_and_ground_expose_distinct_workflow_inputs() -> None:
    schema = app.openapi()["components"]["schemas"]

    pack = schema["EvidencePackRequest"]
    ground = schema["CitationGroundRequest"]
    assert pack["required"] == ["claims"]
    assert "evidence_ids" not in pack["properties"]
    assert "draft" not in pack["properties"]
    assert ground["required"] == ["draft"]
    assert "claims" not in ground["properties"]
    assert "evidence_ids" not in ground["properties"]
    assert "/api/v1/workflows/{run_id}" in app.openapi()["paths"]


def test_paper_processing_starts_only_from_ingestion_not_a_public_stage_endpoint() -> None:
    paths = app.openapi()["paths"]

    assert "/api/v1/papers/{paper_id}/operations/{operation}" not in paths


def test_reference_is_public_but_business_routes_remain_protected(monkeypatch) -> None:
    monkeypatch.setenv("API_AUTH_TOKEN", "contract-token")
    get_settings.cache_clear()
    protected_app = create_app()
    client = TestClient(protected_app)

    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200
    response = client.post("/api/v1/papers/search", json={})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    get_settings.cache_clear()
