import httpx
import pytest
from fastapi.testclient import TestClient

from gibbs_research.literature import LiteratureClient
from gibbs_research.server import create_app


@pytest.fixture
def client(tmp_path):
    def upstream(request):
        path = request.url.path
        if path.endswith('/source'):
            key = path.split('/')[-2]
            return httpx.Response(200, json={"citation": {"evidence_id": key, "paper": {"paper_id": "paper_demo", "title": "Sample research paper", "doi": "10.0000/example"}, "quote": "The supplied samples were measured under the same conditions.", "location": {"page": 3, "bbox": [10, 20, 30, 40]}}})
        if path.endswith('/content'):
            return httpx.Response(200, json={"content": {"text": "The supplied samples were measured under the same conditions."}})
        return httpx.Response(200, json={"path": path, "method": request.method})
    kb = LiteratureClient('http://literature.test', transport=httpx.MockTransport(upstream))
    with TestClient(create_app(tmp_path, kb)) as session:
        yield session


@pytest.fixture
def spec():
    return {"type": "line", "title": "Method comparison", "xLabel": "Time", "xUnit": "s", "yLabel": "Response", "series": [{"name": "A", "data": [[0, 1.5], [1, 2.5]]}, {"name": "B", "data": [[0, 1.0], [1, 2.0]]}]}


@pytest.fixture
def draft():
    return {"abstract": "Evidence-backed comparison [[cite:ev_1]].", "background": "Study the supplied research data.", "methods": [{"title": "Evidence reading", "prose": "Read the full context [[cite:ev_1]]."}], "results_by_system": [{"title": "Sample A", "prose": "Measured response 1.5."}], "comparison": "Compare within the recorded conditions.", "discussion": "The study conditions are kept with the source.", "conclusions": ["The evidence supports the stated comparison [[cite:ev_1]]."], "recommendations": ["Measure additional samples."]}
