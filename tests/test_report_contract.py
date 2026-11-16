import copy

from gibbs_research.reports import validate_draft


def test_citations_numbered_in_reading_order(draft):
    draft['abstract'] = 'Second source first [[cite:ev_2]], then first [[cite:ev_1]], repeat [[cite:ev_2]].'
    clean, _, mapping = validate_draft(draft, [], {'ev_1': 1, 'ev_2': 2})
    assert mapping == {'ev_2': 1, 'ev_1': 2}
    assert clean['abstract'] == 'Second source first [1], then first [2], repeat [1].'


def test_paper_resources_and_import_status(client):
    response = client.post('/api/literature-resource', json={'paper_id': 'paper_demo', 'kind': 'content', 'params': {'offset': 100, 'limit': 200}})
    assert response.status_code == 200
    assert client.get('/api/literature/status/import-batches/batch_demo').json()['path'] == '/api/v1/import-batches/batch_demo'
    assert client.post('/api/literature-resource', json={'paper_id': 'paper_demo', 'kind': 'admin'}).status_code == 422
