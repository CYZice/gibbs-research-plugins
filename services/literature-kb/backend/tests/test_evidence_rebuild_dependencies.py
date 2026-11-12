import inspect
from types import SimpleNamespace

from app.services.evidence_builder import EvidenceBuilderService


def test_evidence_delete_removes_citation_context_reference_first():
    calls = []

    class Db:
        def execute(self, statement):
            calls.append(str(statement))

        def scalars(self, _statement):
            return SimpleNamespace(all=lambda: [])

        def delete(self, _row):
            pass

    service = EvidenceBuilderService(Db(), SimpleNamespace(storage_root="."))
    service._delete_evidence(SimpleNamespace(evidence_id="ev_1"))

    assert "citation_contexts.evidence_id" in calls[0]


def test_full_rebuild_flushes_before_recreating_deterministic_evidence_ids():
    source = inspect.getsource(EvidenceBuilderService.build_for_paper)

    assert "self._delete_all(paper.paper_id)\n            self.db.flush()" in source


def test_structure_flushes_references_before_citation_contexts():
    calls = []

    class Db:
        def add_all(self, rows):
            calls.append(("references", len(rows)))

        def flush(self):
            calls.append(("flush", None))

        def add(self, _row):
            calls.append(("context", None))

    service = EvidenceBuilderService(Db(), SimpleNamespace(storage_root="."))
    service._sync_structure(
        "paper_1",
        {
            "references": [
                {
                    "raw_text": "A reference",
                    "source_block_id": "ref_1",
                    "char_start": 1,
                    "char_end": 12,
                }
            ],
            "citation_contexts": [
                {
                    "quote": "A citation [1]",
                    "marker": "[1]",
                    "source_block_id": "body_1",
                    "reference_index": 1,
                }
            ],
        },
        {},
    )

    assert calls == [("references", 1), ("flush", None), ("context", None)]


def test_blank_weak_representation_is_not_indexable():
    service = EvidenceBuilderService(SimpleNamespace(), SimpleNamespace(storage_root="."))
    unit = SimpleNamespace(
        evidence_id="ev_blank",
        exclude_from_retrieval=False,
    )

    representation = service._representation(unit, " \n ")

    assert representation.indexable is False
