from types import SimpleNamespace

from app.core.config import Settings
from app.services.enrichment_service import EnrichmentService
from app.services.mineru_representation import mineru_text, multimodal_text


class Gateway:
    def __init__(self) -> None:
        self.json_calls = 0
        self.vision_calls = 0

    def complete_json(self, system: str, user: str) -> dict:
        self.json_calls += 1
        return {"embedding_text": "Structured MinerU summary"}

    def describe_image(self, *_args, **_kwargs):
        self.vision_calls += 1
        raise AssertionError("Vision must not be called by text-only enrichment")


def evidence(modality: str, order: int, text: str, page: int = 1):
    return SimpleNamespace(
        evidence_id=f"ev_{modality}_{order}",
        modality=modality,
        block_order=order,
        raw_content=text,
        page=page,
        section_title="Results",
        asset_ids=[],
        evidence_metadata={"caption": "Fig. 1 caption", "figure_label": "Fig. 1"},
    )


def test_figure_representation_uses_same_page_text_without_vision():
    gateway = Gateway()
    service = EnrichmentService(None, Settings(app_env="test"), model_gateway=gateway)
    figure = evidence("figure", 10, "panel a")
    context_pool = [
        evidence("text", 9, "nearest body evidence"),
        evidence("text", 3, "far body evidence", page=2),
    ]

    text, metadata, repr_type = service._representation_input(figure, context_pool)

    assert repr_type == "figure_caption_context"
    assert "nearest body evidence" in text
    assert "far body evidence" not in text
    assert metadata["vision_called"] is False
    assert gateway.json_calls == 0
    assert gateway.vision_calls == 0


def test_table_uses_caption_context_without_model_call():
    gateway = Gateway()
    service = EnrichmentService(None, Settings(app_env="test"), model_gateway=gateway)
    table = evidence("table", 10, "MinerU table values")
    context_pool = [evidence("text", 9, "nearby method details")]

    text, metadata, repr_type = service._representation_input(table, context_pool)

    assert "MinerU table values" in text
    assert "Fig. 1 caption" in text
    assert repr_type == "table_caption_context"
    assert metadata["input"]["context"] == "nearby method details"
    assert metadata["model_called"] is False
    assert gateway.json_calls == 0
    assert gateway.vision_calls == 0


def test_formula_uses_same_page_context_without_model_call():
    gateway = Gateway()
    service = EnrichmentService(None, Settings(app_env="test"), model_gateway=gateway)
    formula = evidence("formula", 10, "E = mc^2")
    context_pool = [evidence("text", 9, "Energy is conserved.")]

    text, metadata, repr_type = service._representation_input(formula, context_pool)

    assert "E = mc^2" in text
    assert "Energy is conserved." in text
    assert repr_type == "formula_caption_context"
    assert metadata["model_called"] is False
    assert gateway.json_calls == 0
    assert gateway.vision_calls == 0


def test_mineru_json_is_removed_from_multimodal_representation():
    raw = "{'image_source': {'path': 'images/a.png'}, 'chart_caption': ['Capacity retention']}"

    assert mineru_text(raw) == "Capacity retention"
    text = multimodal_text(
        "figure", raw, caption="Fig. 2", section="Results", context="Cycle stability improved."
    )

    assert "Fig. 2" in text
    assert "Capacity retention" in text
    assert "Cycle stability improved" in text
    assert "image_source" not in text
    assert "images/a.png" not in text
