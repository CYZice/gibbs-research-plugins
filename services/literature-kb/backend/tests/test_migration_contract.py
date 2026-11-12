from pathlib import Path


def test_v1_baseline_is_explicit_and_does_not_follow_runtime_orm_metadata():
    source = (
        Path(__file__).parents[1] / "app/db/migrations/versions/0001_v1_baseline.py"
    ).read_text(encoding="utf-8")

    assert "Base.metadata.create_all" not in source
    assert 'op.create_table(\n        "papers"' in source
    assert "Vector(1024)" in source


def test_pdf_hash_migration_cleans_duplicates_before_adding_constraint():
    source = (
        Path(__file__).parents[1] / "app/db/migrations/versions/0002_pdf_sha256_unique.py"
    ).read_text(encoding="utf-8")

    assert "duplicates AS" in source
    assert source.index("DELETE FROM citation_contexts") < source.index(
        "op.create_unique_constraint"
    )


def test_workflow_runs_are_a_dedicated_persistent_table():
    source = (
        Path(__file__).parents[1] / "app/db/migrations/versions/0003_workflow_runs.py"
    ).read_text(encoding="utf-8")

    assert '"workflow_runs"' in source
    assert '"request_payload"' in source
    assert '"result_payload"' in source


def test_metasearch_migration_adds_search_and_batch_primitives():
    source = (
        Path(__file__).parents[1] / "app/db/migrations/versions/0004_metasearch_and_batch_import.py"
    ).read_text(encoding="utf-8")

    for value in (
        "paper_identifiers",
        "paper_search_documents",
        "import_batches",
        "import_batch_items",
        "pg_trgm",
        "hnsw",
    ):
        assert value in source


def test_evidence_fulltext_migration_maintains_the_search_vector():
    source = (
        Path(__file__).parents[1] / "app/db/migrations/versions/0005_evidence_fulltext_index.py"
    ).read_text(encoding="utf-8")

    assert "search_vector" in source
    assert "evidence_representation_search_vector_trigger" in source


def test_operational_control_migration_adds_provider_and_attempt_tables():
    source = (
        Path(__file__).parents[1] / "app/db/migrations/versions/0006_operational_control_plane.py"
    ).read_text(encoding="utf-8")

    for table in (
        "provider_configs",
        "provider_credentials",
        "provider_call_events",
        "execution_attempts",
        "evidence_judgements",
    ):
        assert table in source


def test_pipeline_v2_migration_adds_leases_claims_and_dispatch_indexes():
    source = (
        Path(__file__).parents[1] / "app/db/migrations/versions/0012_pipeline_v2.py"
    ).read_text(encoding="utf-8")

    for value in (
        "delivery_lease_until",
        "run_token",
        "run_lease_until",
        "pipeline_generation",
        "ix_paper_tasks_dispatch",
        "uq_pipeline_stage_per_root",
        "uq_evidence_embeddings_representation_model",
    ):
        assert value in source
    assert "':pdf'" not in source


def test_provider_channel_migration_is_replaced_by_role_consolidation():
    source = (
        Path(__file__).parents[1]
        / "app/db/migrations/versions/0010_consolidate_provider_roles.py"
    ).read_text(encoding="utf-8")

    assert "provider_channels" in source
    assert "provider_role_bindings" in source
    assert "provider_configs" in source
    assert 'DELETE FROM provider_credentials WHERE provider_config_id IS NULL' in source
    assert 'op.alter_column("provider_credentials", "provider_config_id", nullable=False)' in source
    assert 'op.drop_table("provider_channels")' in source


def test_openalex_harvest_migration_adds_staging_and_credential_tables():
    source = (
        Path(__file__).parents[1]
        / "app/db/migrations/versions/0013_openalex_harvest.py"
    ).read_text(encoding="utf-8")

    for table in (
        "harvest_jobs",
        "corpus_documents",
        "harvest_job_items",
        "harvest_credentials",
    ):
        assert f'"{table}"' in source
    assert "uq_harvest_job_document" in source
    assert "key_fingerprint" in source


def test_staged_import_migration_hard_renames_and_backfills_pipeline():
    source = (
        Path(__file__).parents[1]
        / "app/db/migrations/versions/0014_staged_import_key_concurrency.py"
    ).read_text(encoding="utf-8")

    for value in (
        "registered_items",
        "registered_paper_id",
        "import_batch_item_id",
        "max_concurrency",
        "staged_import",
        "literature-ingest",
        "uq_pipeline_stage_per_root",
    ):
        assert value in source


def test_import_pipeline_v3_migration_adds_generic_staged_artifacts():
    source = (
        Path(__file__).parents[1]
        / "app/db/migrations/versions/0015_import_pipeline_v3.py"
    ).read_text(encoding="utf-8")

    for value in (
        "staged_artifacts",
        "staged_artifact_id",
        "remote_fetch",
        "register",
        "Pipeline V2 import tasks must be drained",
        "uq_pipeline_stage_per_root",
    ):
        assert value in source


def test_workflow_scope_migration_binds_runs_to_access_scope():
    source = (
        Path(__file__).parents[1]
        / "app/db/migrations/versions/0017_workflow_scope_binding.py"
    ).read_text(encoding="utf-8")

    assert '"access_scope"' in source
    assert '"scope_digest"' in source
    assert '"ix_workflow_runs_access_scope"' in source
