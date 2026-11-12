import json

from app.models.paper import Paper
from app.services.metadata_enrichment_service import MetadataEnrichmentService


def test_mineru_native_metadata_enrichment_extracts_nature_header(tmp_path):
    blocks = [
        {"page": 1, "type": "title", "content": "A native MinerU paper title"},
        {"page": 1, "type": "paragraph", "content": "Alice Smith<sup>1</sup>, Bob Jones<sup>2</sup> and Carol Lee<sup>1</sup>*"},
        {"page": 1, "type": "paragraph", "content": "This abstract describes the study. " * 20},
        {"page": 1, "type": "page_header", "content": "Nature Nanotechnology"},
        {"page": 1, "type": "page_header", "content": "https://doi.org/10.1038/s41565-018-0183-2"},
        {"page": 1, "type": "page_footer", "content": "VOL 13 | AUGUST 2018"},
    ]
    (tmp_path / "content_map.json").write_text(json.dumps({"blocks": blocks}), encoding="utf-8")
    paper = Paper(
        paper_id="paper_1", title="upload.pdf", metadata_provenance={"title": "filename"}
    )

    result = MetadataEnrichmentService().enrich(paper, tmp_path)

    assert result["source"] == "mineru_native"
    assert paper.title == "A native MinerU paper title"
    assert paper.authors == [
        {"name": "Alice Smith"}, {"name": "Bob Jones"}, {"name": "Carol Lee"}
    ]
    assert paper.year == 2018
    assert paper.doi == "10.1038/s41565-018-0183-2"
    assert paper.venue == "Nature Nanotechnology"
    assert paper.language == "en"
    assert paper.metadata_provenance["abstract"] == "mineru_native"


def test_manifest_metadata_is_not_overwritten_by_mineru(tmp_path):
    (tmp_path / "content_map.json").write_text(
        json.dumps({"blocks": [{"page": 1, "type": "title", "content": "MinerU title"}]}),
        encoding="utf-8",
    )
    paper = Paper(
        paper_id="paper_1", title="Manifest title", metadata_provenance={"title": "manifest"}
    )

    MetadataEnrichmentService().enrich(paper, tmp_path)

    assert paper.title == "Manifest title"
