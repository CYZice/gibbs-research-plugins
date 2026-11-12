from app.core.config import Settings
from app.models.paper import Paper
from app.services.model_gateway import ModelGatewayError, RerankResult
from app.services.paper_search_service import (
    PaperSearchResult,
    PaperSearchService,
    normalized_identifier,
    search_terms,
)


def test_search_terms_preserve_latin_and_cjk_phrase_recall():
    terms = search_terms("固态电解质 Solid Electrolyte")

    assert "固态" in terms
    assert "电解" in terms
    assert "electrolyte" in terms


def test_identifier_normalization_handles_doi_urls_and_arxiv_prefixes():
    assert normalized_identifier("https://doi.org/10.1000/ABC") == "10.1000/abc"
    assert normalized_identifier("arXiv:2401.00001") == "2401.00001"


def test_paper_search_service_initializes_request_metrics():
    assert PaperSearchService(None, Settings(app_env="test")).metrics == {}


def test_paper_rerank_reorders_candidates_and_falls_back():
    class Gateway:
        def rerank(self, _query, _documents, _top_k):
            return [RerankResult(index=1, score=0.9), RerankResult(index=0, score=0.8)]

    first = Paper(paper_id="paper_1", title="First", authors=[{"name": "Ada Lovelace"}])
    second = Paper(paper_id="paper_2", title="Second")
    service = PaperSearchService(None, Settings(app_env="test"), model_gateway=Gateway())
    result = service._rerank(
        "query", [PaperSearchResult(first, 0.2, []), PaperSearchResult(second, 0.1, [])]
    )

    assert [item.paper.paper_id for item in result] == ["paper_2", "paper_1"]
    assert service.metrics["rerank_applied"] is True

    class FailingGateway:
        def rerank(self, *_args):
            raise ModelGatewayError("timeout")

    service = PaperSearchService(None, Settings(app_env="test"), model_gateway=FailingGateway())
    original = [PaperSearchResult(first, 0.2, []), PaperSearchResult(second, 0.1, [])]
    assert service._rerank("query", original) == original
    assert service.metrics["rerank_applied"] is False
