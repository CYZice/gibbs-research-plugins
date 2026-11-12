from __future__ import annotations

import ast
import json
from typing import Any

_SKIP_KEYS = {
    "bbox",
    "image_source",
    "img_path",
    "image_path",
    "path",
    "score",
    "type",
    "level",
    "layout",
    "polygon",
    "coordinate_space",
}
_TEXT_KEYS = {
    "text",
    "content",
    "caption",
    "image_caption",
    "chart_caption",
    "table_caption",
    "latex",
    "equation",
    "title",
    "label",
    "description",
}


def mineru_text(value: Any) -> str:
    """Return human-readable MinerU text without leaking parser JSON or asset metadata."""
    if value is None:
        return ""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in {"{", "["}:
            parsed = None
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                try:
                    parsed = ast.literal_eval(stripped)
                except (ValueError, SyntaxError):
                    pass
            if isinstance(parsed, (dict, list, tuple)):
                return mineru_text(parsed)
        return stripped
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            if key in _SKIP_KEYS or (key not in _TEXT_KEYS and isinstance(item, dict)):
                continue
            text = mineru_text(item)
            if text and text not in parts:
                parts.append(text)
        return " ".join(parts)
    if isinstance(value, (list, tuple)):
        parts = []
        for item in value:
            text = mineru_text(item)
            if text and text not in parts:
                parts.append(text)
        return " ".join(parts)
    return str(value).strip()


def multimodal_text(
    modality: str,
    raw_content: Any,
    *,
    caption: str = "",
    label: str = "",
    section: str = "",
    context: str = "",
) -> str:
    raw = mineru_text(raw_content)
    values = [label.strip(), mineru_text(caption), raw]
    if section:
        values.append(f"Section: {section.strip()}")
    if context:
        values.append(f"Nearby text: {mineru_text(context)}")
    parts = []
    for value in values:
        if value and value not in parts:
            parts.append(value)
    prefix = {"figure": "Figure", "table": "Table", "formula": "Formula"}.get(modality)
    return "\n".join(([prefix] if prefix and not parts else []) + parts).strip()
