import json
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.services.mineru_service import MinerUError, MinerUService
from app.services.mineru_structure_enhancer import (
    MinerUNativeStructureEnhancer,
    NoopMinerUStructureEnhancer,
    StructureEnhancementInput,
)


def test_nested_mineru_content_map_keeps_page_and_normalized_bbox(tmp_path):
    parsed = tmp_path / "parsed" / "paper_test"
    parsed.mkdir(parents=True)
    (parsed / "sample_content_list_v2.json").write_text(json.dumps([
        [{"type": "paragraph", "content": {"paragraph_content": [{"type": "text", "content": "page one"}]}, "bbox": [0, 100, 1000, 200]}],
        [{"type": "paragraph", "content": {"paragraph_content": [{"type": "text", "content": "page two"}]}, "bbox": [100, 200, 900, 300]}],
    ]), encoding="utf-8")
    service = MinerUService(None, Settings(storage_root=str(tmp_path)))
    _, key, text = service._build_content("paper_test", parsed)
    payload = json.loads((tmp_path / key).read_text(encoding="utf-8"))

    assert text == "page one\n\npage two"
    assert payload["version"] == 2
    assert [item["page"] for item in payload["blocks"]] == [1, 2]
    assert payload["blocks"][1]["normalized_bbox"] == [0.1, 0.2, 0.9, 0.3]


def test_mineru_content_removes_postgres_incompatible_nul_bytes(tmp_path):
    parsed = tmp_path / "parsed" / "paper_nul"
    parsed.mkdir(parents=True)
    (parsed / "sample_content_list.json").write_text(
        json.dumps([{"type": "text", "text": "hello\u0000 world"}]),
        encoding="utf-8",
    )

    service = MinerUService(None, Settings(storage_root=str(tmp_path)))
    _, map_key, text = service._build_content("paper_nul", parsed)
    payload = json.loads((tmp_path / map_key).read_text(encoding="utf-8"))

    assert text == "hello world"
    assert "\x00" not in payload["blocks"][0]["content"]


def test_noop_structure_enhancer_preserves_canonical_anchors():
    source = StructureEnhancementInput("paper", "text", {"blocks": [{"char_start": 0, "char_end": 4, "content": "text"}]}, {}, ())
    assert NoopMinerUStructureEnhancer().enhance(source).sections == []


def test_mineru_native_structure_enhancer_adds_only_anchored_metadata():
    text = "Introduction\n\nFig. 1 shows the result [1].\n\nReferences\n\n[1] Example work. doi:10.1000/example"
    blocks = [
        {"source_block_id": "p0_b0", "char_start": 0, "char_end": 12, "content": "Introduction", "type": "title", "page": 1, "normalized_bbox": [0.1, 0.1, 0.4, 0.2]},
        {"source_block_id": "p0_b1", "char_start": 14, "char_end": 42, "content": "Fig. 1 shows the result [1].", "type": "paragraph", "page": 1, "normalized_bbox": [0.1, 0.3, 0.8, 0.4]},
        {"source_block_id": "p0_b2", "char_start": 44, "char_end": 54, "content": "References", "type": "title", "page": 2, "normalized_bbox": [0.1, 0.1, 0.4, 0.2]},
        {"source_block_id": "p0_b3", "char_start": 56, "char_end": len(text), "content": "[1] Example work. doi:10.1000/example", "type": "paragraph", "page": 2, "normalized_bbox": [0.1, 0.3, 0.8, 0.4]},
    ]
    source = StructureEnhancementInput("paper", text, {"blocks": blocks}, {}, ())

    result = MinerUNativeStructureEnhancer().enhance(source)

    assert [section.section_type for section in result.sections] == ["introduction", "references"]
    assert result.callouts[0].kind == "fig"
    assert result.citation_contexts[0].marker == "[1]"
    assert result.references[0].doi == "10.1000/example"
    for item in [*result.sections, *result.callouts, *result.citation_contexts, *result.references]:
        anchor = item.anchor
        assert text[anchor.char_start:anchor.char_end]


def test_cached_mineru_result_restores_parse_status_without_external_call(tmp_path):
    (tmp_path / "pdfs").mkdir()
    (tmp_path / "pdfs" / "paper_1.pdf").write_bytes(b"cached pdf")
    paper = SimpleNamespace(paper_id="paper_1", parse_status="running", pdf_storage_key="pdfs/paper_1.pdf")
    task = SimpleNamespace(details={"provider": {"state": "done"}, "result": {}}, progress=0)

    class Db:
        def get(self, _model, _paper_id):
            return object()

    service = MinerUService(Db(), SimpleNamespace(storage_root=str(tmp_path), mineru_api_token="token"))

    result = service.run_task(paper, task)

    assert result["skipped"] is True
    assert paper.parse_status == "succeeded"


def test_poll_parse_once_checks_provider_only_once_without_sleep():
    task = SimpleNamespace(
        details={"provider": {"batch_id": "batch_1", "state": "pending"}},
        progress=0,
    )

    class Db:
        commits = 0

        def commit(self):
            self.commits += 1

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "code": 0,
                "data": {
                    "extract_result": [
                        {
                            "state": "running",
                            "extract_progress": {
                                "total_pages": 10,
                                "extracted_pages": 3,
                            },
                        }
                    ]
                },
            }

    class Http:
        calls = 0

        def get(self, *_args, **_kwargs):
            self.calls += 1
            return Response()

    db = Db()
    http = Http()
    settings = SimpleNamespace(
        mineru_api_token="token",
        mineru_base_url="https://mineru.example",
        storage_root=".",
    )

    result = MinerUService(db, settings, http).poll_parse_once(task)

    assert result["state"] == "running"
    assert http.calls == 1
    assert db.commits == 2
    assert task.details["provider"]["state"] == "running"
    assert 0.2 < task.progress < 0.9


def test_upload_failure_clears_batch_id_so_parse_retry_reuploads(tmp_path):
    (tmp_path / "pdfs").mkdir()
    (tmp_path / "pdfs" / "paper_1.pdf").write_bytes(b"pdf")
    task = SimpleNamespace(details={}, progress=0)

    class Db:
        def commit(self):
            pass

    class Response:
        def __init__(self, body=None, status_code=200):
            self.body = body
            self.status_code = status_code

        def raise_for_status(self):
            pass

        def json(self):
            return self.body

    class Http:
        posts = 0
        uploads = 0

        def post(self, *_args, **_kwargs):
            self.posts += 1
            return Response(
                {
                    "code": 0,
                    "data": {
                        "batch_id": f"batch_{self.posts}",
                        "file_urls": ["https://upload.example"],
                    },
                }
            )

        def put(self, *_args, **_kwargs):
            self.uploads += 1
            return Response(status_code=500 if self.uploads == 1 else 200)

    paper = SimpleNamespace(paper_id="paper_1", pdf_storage_key="pdfs/paper_1.pdf")
    settings = SimpleNamespace(
        mineru_api_token="token",
        mineru_base_url="https://mineru.example",
        storage_root=str(tmp_path),
        mineru_model_version="vlm",
        mineru_language="ch",
        mineru_enable_table=True,
        mineru_enable_formula=True,
        mineru_is_ocr=False,
    )
    http = Http()
    service = MinerUService(Db(), settings, http)

    with pytest.raises(MinerUError, match="upload failed"):
        service.submit_parse(paper, task)

    assert task.details["provider"] == {"name": "mineru", "state": "upload_failed"}
    assert service.submit_parse(paper, task) == "batch_2"
    assert http.posts == 2
    assert task.details["provider"]["state"] == "pending"
