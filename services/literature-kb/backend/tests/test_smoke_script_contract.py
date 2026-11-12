from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_reset_script_requires_explicit_confirmation() -> None:
    source = (ROOT / "scripts/reset_development_state.ps1").read_text(encoding="utf-8")

    assert "[switch]$ConfirmReset" in source
    assert "without -ConfirmReset" in source
    assert "storage/backups" in source


def test_live_smoke_script_has_resumable_real_provider_contract() -> None:
    source = (ROOT / "scripts/smoke_v1_real_pipeline.py").read_text(encoding="utf-8")

    for flag in ("--pdf-primary", "--pdf-table", "--resume", "--reset", "--start-local", "--strict", "--cleanup-success"):
        assert flag in source
    for stage in ("upload_constraints", "mineru_ir", "multimodal", "synthetic_incremental_probe", "agent_flow"):
        assert stage in source
    assert "vision_called" in source
    assert "scope\": {\"paper_ids\"" in source
    assert "blocked_external" in source
