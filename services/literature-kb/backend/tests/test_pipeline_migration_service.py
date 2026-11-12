from app.models.paper import Paper
from app.models.paper_task import PaperTask
from app.services.pipeline_migration_service import (
    first_incomplete_stage,
    migrate_v1_process_tasks,
)


def paper_with(**statuses) -> Paper:
    values = {
        "paper_id": "paper_1",
        "title": "Test paper",
        "authors": [],
        "year": 2026,
        "source_provenance": "upload",
        "pdf_storage_key": "papers/paper_1/original.pdf",
        "parse_status": "queued",
        "evidence_status": "not_started",
        "multimodal_status": "not_started",
        "embedding_status": "not_started",
        "index_status": "not_started",
    }
    values.update(statuses)
    return Paper(**values)


def test_first_incomplete_stage_resumes_after_the_last_durable_output():
    assert first_incomplete_stage(paper_with()) == "parse"
    assert first_incomplete_stage(
        paper_with(parse_status="succeeded")
    ) == "evidence"
    assert first_incomplete_stage(
        paper_with(parse_status="succeeded", evidence_status="ready")
    ) == "resource_context"
    assert first_incomplete_stage(
        paper_with(
            parse_status="succeeded",
            evidence_status="ready",
            multimodal_status="ready",
        )
    ) == "vectorize"
    assert first_incomplete_stage(
        paper_with(
            parse_status="succeeded",
            evidence_status="ready",
            multimodal_status="ready",
            embedding_status="ready",
        )
    ) == "index"
    assert first_incomplete_stage(
        paper_with(index_status="ready")
    ) is None


def test_v1_migration_is_read_only_without_apply():
    paper = paper_with(parse_status="succeeded")
    legacy = PaperTask(
        task_id="task_v1",
        paper_id=paper.paper_id,
        operation="process",
        provider="local",
        status="queued",
        pipeline_version=1,
        details={},
    )

    class Rows:
        @staticmethod
        def all():
            return [legacy]

    class Db:
        @staticmethod
        def scalars(_statement):
            return Rows()

        @staticmethod
        def scalar(_statement):
            return None

        @staticmethod
        def get(model, key):
            if model is Paper and key == paper.paper_id:
                return paper
            return None

    result = migrate_v1_process_tasks(Db())

    assert result["dry_run"] is True
    assert result["items"][0]["initial_stage"] == "evidence"
    assert result["migrated_count"] == 0
    assert legacy.status == "queued"
