from fastapi.testclient import TestClient

from app.main import app
from app.schemas.v1 import ProcessingStatus


def test_openapi_exposes_only_v1_literature_contract() -> None:
    paths = app.openapi()["paths"]

    assert "/api/v1/papers/search" in paths
    assert "/api/v1/evidence/search" in paths
    assert "/api/v1/assets/{asset_id}/download" in paths
    assert "/api/v1/agent/evidence/search" not in paths
    assert "/papers/meta_search" not in paths
    assert "/evidence/context_pack" not in paths


def test_legacy_paths_return_not_found() -> None:
    client = TestClient(app)

    assert client.post("/papers/meta_search", json={}).status_code == 404
    assert client.post("/api/v1/agent/evidence/search", json={}).status_code == 404
    assert client.get("/files/pdfs/paper_1.pdf").status_code == 404


def test_paper_search_rejects_legacy_fields() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/v1/papers/search",
        json={"query": "transformer", "limit": 20, "offset": 0},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_v1_search_contract_has_no_legacy_response_fields() -> None:
    schema = app.openapi()["components"]["schemas"]
    paper = schema["PaperSummary"]["properties"]
    evidence = schema["EvidenceHit"]["properties"]

    for forbidden in ("paper_uid", "source_type", "storage_status", "pdf_path", "schema_version"):
        assert forbidden not in paper
    for forbidden in ("paper_id", "paper_title", "source", "bbox", "source_location", "debug"):
        assert forbidden not in evidence


def test_paper_search_supports_hybrid_fields_without_a_new_route() -> None:
    schema = app.openapi()["components"]["schemas"]
    request = schema["PaperSearchRequest"]["properties"]
    response = schema["PaperSearchResponse"]["properties"]

    assert "/api/v1/papers/search" in app.openapi()["paths"]
    assert set(request["search_mode"]["enum"]) == {"lexical", "semantic", "hybrid"}
    assert "facets" in request
    assert {"facets", "search_info"}.issubset(response)


def test_processing_status_allows_queued_embedding() -> None:
    status = ProcessingStatus(
        fulltext="local_pdf",
        parse="queued",
        evidence="not_started",
        embedding="queued",
        multimodal="not_started",
    )

    assert status.embedding == "queued"


def test_external_discovery_contract_is_separate_from_local_catalog() -> None:
    schema = app.openapi()
    paths = schema["paths"]

    assert "/api/v1/discovery/sources" in paths
    assert "/api/v1/discovery/search" in paths
    assert "/api/v1/discovery/works/resolve" in paths
    assert "/api/v1/discovery/downloads" in paths
    assert paths["/api/v1/discovery/search"]["post"]["responses"]["200"]["content"]

    request = schema["components"]["schemas"]["DiscoveryDownloadRequest"]
    assert "url" not in request["properties"]


def test_federated_search_and_remote_import_contracts_are_explicit() -> None:
    schema = app.openapi()
    paths = schema["paths"]

    assert "/api/v1/literature/search" in paths
    assert "/api/v1/literature/imports" in paths
    assert paths["/api/v1/literature/imports"]["post"]["responses"]["202"]
    import_item = schema["components"]["schemas"]["LiteratureImportItem"]["properties"]
    assert "url" not in import_item
    assert "storage_key" not in import_item
    batch = schema["components"]["schemas"]["ImportBatchResponse"]["properties"]
    assert "items" in batch


def test_pipeline_v2_batch_contract_supports_chunked_intake_and_pagination() -> None:
    schema = app.openapi()
    paths = schema["paths"]

    assert paths["/api/v1/import-batches"]["post"]["responses"]["202"]
    assert paths["/api/v1/import-batches/manifests"]["post"]["responses"]["202"]
    assert "/api/v1/import-batches/{batch_id}/items" in paths
    assert "/api/v1/import-batches/{batch_id}/finalize" in paths
    assert "/api/v1/tasks/{task_id}/stages" in paths
    assert "/api/v1/admin/pipeline/overview" in paths
    assert "/api/v1/admin/pipeline/intake/pause" in paths
    assert "/api/v1/admin/pipeline/intake/resume" in paths
    assert "/api/v1/admin/pipelines" in paths
    assert "/api/v1/admin/pipelines/{root_task_id}/stages" in paths
    assert "/api/v1/admin/pipeline-stages/{task_id}/retry" in paths
    append = schema["components"]["schemas"]["ImportBatchAppendRequest"]
    assert append["properties"]["items"]["maxItems"] == 5000
    response = schema["components"]["schemas"]["ImportBatchResponse"]["properties"]
    assert "next_cursor" in response
    item = schema["components"]["schemas"]["ImportBatchItemResponse"]["properties"]
    assert "current_stage" in item
