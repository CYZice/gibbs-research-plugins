import copy
import os

import pytest

from gibbs_research.plots import render, validate_spec
from gibbs_research.reports import validate_draft


def test_plot_redraw_and_stale_edit(client, spec):
    first = client.post('/api/plots', json={"spec": spec, "provenance": {"source": "sample.csv"}}).json()
    changed = copy.deepcopy(spec)
    changed['style'] = {"font_size": 16, "legend_location": "upper left"}
    update = {"artifact_id": first['artifact_id'], "base_revision": 1, "spec": changed, "feedback": "Larger font"}
    second = client.post('/api/plots', json=update)
    assert second.status_code == 200, second.text
    assert second.json()['revision'] == 2
    assert client.post('/api/plots', json=update).status_code == 409
    original = client.get(f"/api/artifacts/{first['artifact_id']}?revision=1").json()
    assert 'style' not in original['spec']
    assert original['provenance']['source'] == 'sample.csv'


@pytest.mark.parametrize('fmt,signature', [('png', b'\x89PNG'), ('svg', b'<?xml'), ('pdf', b'%PDF')])
def test_vector_exports(spec, fmt, signature):
    assert render(spec, fmt).startswith(signature)


@pytest.mark.parametrize('kind', ['line', 'scatter', 'arrhenius', 'rdf', 'nyquist', 'stacked_area', 'bar', 'pie'])
def test_chart_types(spec, kind):
    spec['type'] = kind
    if kind == 'bar':
        spec['categories'] = ['A', 'B']
        spec['series'] = [{"name": "test", "data": [1.5, 2.5]}]
    if kind == 'pie':
        spec['data'] = [{"name": "A", "value": 2}, {"name": "B", "value": 3}]
        spec.pop('series')
    assert render(spec).startswith(b'\x89PNG')


def test_bad_data_rejected(client, spec):
    spec['series'][0]['data'][0][1] = 'not a number'
    assert client.post('/api/plots', json={"spec": spec}).status_code == 422
    with pytest.raises(ValueError):
        validate_spec({"type": "bar", "categories": ["A"], "series": [{"data": [1, 2]}]})
    with pytest.raises(ValueError):
        validate_spec({"type": "line", "data": [[1, float('inf')]]})


def test_feedback_resolved_by_redraw(client, spec):
    first = client.post('/api/plots', json={"spec": spec}).json()
    feedback = client.post(f"/api/artifacts/{first['artifact_id']}/feedback", json={"revision": 1, "text": "Increase font size"}).json()
    assert len(client.get('/api/feedback').json()) == 1
    spec['style'] = {"font_size": 16}
    response = client.post('/api/plots', json={"spec": spec, "artifact_id": first['artifact_id'], "base_revision": 1, "feedback_id": feedback['id'], "feedback": feedback['text']})
    assert response.status_code == 200, response.text
    assert client.get('/api/feedback').json() == []


def make_report(client, spec, draft):
    plot = client.post('/api/plots', json={"spec": spec, "provenance": {"dataset": "sample.csv"}}).json()
    material = {"title": "Evidence and figures", "goal": "Compare methods", "evidence_ids": ["evidence_demo"], "plots": [{"artifact_id": plot['artifact_id'], "revision": 1}], "results": [{"label": "Sample", "metrics": [{"name": "response", "label": "Response", "value": 1.5}]}]}
    prepared = client.post('/api/reports/prepare', json=material)
    assert prepared.status_code == 200, prepared.text
    assert prepared.json()['sources'][0]['source']['citation']['location']['page'] == 3
    draft['results_by_system'][0]['prose'] += f" [[fig:{plot['artifact_id']}_r1]]"
    result = client.post('/api/reports', json={"material": material, "draft": draft})
    assert result.status_code == 200, result.text
    return plot, result.json()


def test_report_pins_plot_and_sources(client, spec, draft):
    plot, report = make_report(client, spec, draft)
    spec['title'] = 'New plot title'
    client.post('/api/plots', json={"artifact_id": plot['artifact_id'], "base_revision": 1, "spec": spec})
    snapshot = client.get(f"/api/artifacts/{report['artifact_id']}").json()
    assert snapshot['prepared']['plots'][0]['revision'] == 1
    assert snapshot['prepared']['plots'][0]['spec']['title'] == 'Method comparison'
    base = f"/api/reports/{report['artifact_id']}/1"
    page = client.get(base + '/html')
    assert 'data:image/png;base64,' in page.text
    assert 'evidence_id=evidence_demo' in page.text
    assert 'Evidence and figures' in page.text
    assert client.get(base + '/md').status_code == 200
    assert 'evidence_demo' in client.get(base + '/json').text


def test_unknown_citations_rejected(client, draft):
    draft['abstract'] = 'Unsupported [[cite:made_up]].'
    result = client.post('/api/reports', json={"material": {"title": "Draft", "evidence_ids": ["evidence_demo"]}, "draft": draft})
    assert result.status_code == 422
    assert 'Unknown cite' in result.text


def test_unpinned_figure_rejected(client, spec):
    plot = client.post('/api/plots', json={"spec": spec}).json()
    result = client.post('/api/reports/prepare', json={"title": "Draft", "evidence_ids": ["evidence_demo"], "plots": [{"artifact_id": plot['artifact_id']}]})
    assert result.status_code == 422


def test_explicit_model_configuration_required(client, monkeypatch):
    monkeypatch.delenv('GIBBS_MODEL_BASE_URL', raising=False)
    result = client.post('/api/reports', json={"material": {"title": "Draft", "evidence_ids": ["evidence_demo"]}})
    assert result.status_code == 422
    assert 'GIBBS_MODEL_BASE_URL' in result.text


def test_proxy_matches_existing_routes(client):
    assert client.post('/api/literature/import', json={"items": []}).json()['path'] == '/api/v1/literature/imports'
    assert client.post('/api/literature/bibliography', json={"paper_ids": ["p"]}).json()['path'] == '/api/v1/bibliography/format'
    assert client.post('/api/literature/admin', json={}).status_code == 422
    assert client.post('/api/literature-upload', content=b'not a PDF').status_code == 422


def test_csrf_and_auth(client, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from gibbs_research.server import create_app
    assert client.post('/api/plots', json={"spec": {}}, headers={"origin": "https://untrusted.example"}).status_code == 403
    monkeypatch.setenv('GIBBS_TOOLS_TOKEN', 'test-only-token')
    with TestClient(create_app(tmp_path / 'auth')) as secured:
        assert secured.get('/api/artifacts').status_code == 401
        assert secured.get('/health').status_code == 200
        assert secured.post('/auth/login', json={"token": "wrong"}).status_code == 401
        assert secured.post('/auth/login', json={"token": "test-only-token"}).status_code == 200
        assert secured.get('/api/artifacts').status_code == 200


@pytest.mark.skipif(not os.environ.get('GIBBS_TEST_PDF'), reason='Set GIBBS_TEST_PDF=1 with installed Chromium or Edge')
def test_real_pdf_export(client, spec, draft):
    _, report = make_report(client, spec, draft)
    pdf = client.get(f"/api/reports/{report['artifact_id']}/1/pdf")
    assert pdf.status_code == 200, pdf.text
    assert pdf.content.startswith(b'%PDF-')
