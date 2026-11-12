import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

IR_VERSION = "document-ir-v1"


@dataclass
class DocumentIRBlock:
    type: str
    content: str
    page: int | None = None
    bbox: list[float] | None = None
    img_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DocumentIR:
    blocks: list[DocumentIRBlock]
    metadata: dict[str, Any]


class DocumentIRFusionHook(Protocol):
    """Future structure enhancers may add metadata but must not move MinerU anchors."""

    def apply(self, document: DocumentIR) -> DocumentIR: ...


class DocumentStructureFusionService:
    def __init__(self, parsed_dir: Path, hook: DocumentIRFusionHook | None = None) -> None:
        self.parsed_dir = parsed_dir
        self.hook = hook

    def build(self) -> DocumentIR:
        content_path = self._content_list_path()
        if content_path is None:
            raise ValueError(f"No MinerU content list found under: {self.parsed_dir}")
        blocks = self._blocks_from_content_list(
            json.loads(content_path.read_text(encoding="utf-8")),
            content_path.name,
        )
        layout_path = self.parsed_dir / "layout.json"
        if layout_path.exists():
            self._apply_layout_metadata(
                blocks,
                json.loads(layout_path.read_text(encoding="utf-8")),
            )
            layout_source = "layout.json"
        else:
            layout_source = None

        self._propagate_figures(blocks)
        document = DocumentIR(
            blocks=blocks,
            metadata={
                "document_ir_version": IR_VERSION,
                "fusion_status": "success",
                "document_ir_source_file": str(content_path),
                "fusion_sources": {
                    "mineru": content_path.name,
                    "mineru_layout": layout_source,
                },
                "fusion_stats": {
                    "blocks": len(blocks),
                    "visual_assets": sum(1 for item in blocks if item.img_path),
                },
            },
        )
        return self.hook.apply(document) if self.hook else document

    def _blocks_from_content_list(
        self,
        data: Any,
        filename: str,
    ) -> list[DocumentIRBlock]:
        source = "content_list_v2" if filename.endswith("content_list_v2.json") else "content_list"
        blocks = []
        if filename.endswith("content_list_v2.json"):
            pages = data if isinstance(data, list) else []
            global_index = 0
            for page_index, page in enumerate(pages, start=1):
                if not isinstance(page, list):
                    continue
                for block in page:
                    if isinstance(block, dict):
                        blocks.append(self._content_list_block(block, page_index, global_index, source))
                        global_index += 1
            return blocks

        rows = data if isinstance(data, list) else []
        for index, block in enumerate(rows):
            if not isinstance(block, dict):
                continue
            page = block.get("page")
            if page is None and block.get("page_idx") is not None:
                page = int(block["page_idx"]) + 1
            blocks.append(self._content_list_block(block, page, index, source))
        return blocks

    def _content_list_block(
        self,
        block: dict[str, Any],
        page: int | None,
        index: int,
        source: str,
    ) -> DocumentIRBlock:
        raw_type = str(block.get("type") or "")
        asset_path = self._content_asset_path(block)
        return DocumentIRBlock(
            type=raw_type,
            content=self._content_list_text(block),
            page=page,
            bbox=block.get("bbox") if isinstance(block.get("bbox"), list) else None,
            img_path=asset_path,
            metadata={
                "structure_source": "mineru_content_list",
                "fusion_sources": [source],
                "mineru_content_type": raw_type,
                "mineru_block_index": index,
                "source_block_id": f"p0_b{index}",
                "bbox_source": source,
            },
        )

    def _apply_layout_metadata(
        self,
        blocks: list[DocumentIRBlock],
        data: dict[str, Any],
    ) -> None:
        supplement = self._layout_supplement(data)
        for block in blocks:
            metadata = None
            if block.img_path:
                metadata = supplement["by_asset"].get(Path(block.img_path).name)
            if metadata is None:
                rows = supplement["by_text"].get(
                    self._text_key(block.page or 0, block.type, block.content)
                )
                metadata = rows[0] if rows else None
            if not metadata:
                continue
            block.metadata.update(
                {key: value for key, value in metadata.items() if value is not None}
            )
            block.metadata.setdefault("fusion_sources", ["content_list_v2"])
            if "mineru_layout" not in block.metadata["fusion_sources"]:
                block.metadata["fusion_sources"].append("mineru_layout")

    def _layout_supplement(self, data: dict[str, Any]) -> dict[str, Any]:
        by_asset: dict[str, dict[str, Any]] = {}
        by_text: dict[tuple[int, str, str], list[dict[str, Any]]] = {}
        for page_index, page in enumerate(data.get("pdf_info", []), start=1):
            page_blocks = page.get("para_blocks") or page.get("preproc_blocks") or []
            for block in page_blocks:
                raw_type = str(block.get("type") or "")
                content = self._text(block)
                metadata = self._layout_metadata(block)
                if raw_type in {"image", "chart"}:
                    caption = self._caption_text(block, raw_type)
                    label = self._figure_label(caption)
                    panel = self._panel_label(caption or content)
                    if caption:
                        metadata["caption"] = caption
                        metadata["caption_source"] = "mineru_layout"
                    if label:
                        metadata["figure_label"] = label
                        metadata["figure_group_id"] = label.lower().replace(" ", "_")
                    if panel:
                        metadata["panel_label"] = panel
                    for path in self._image_paths(block):
                        by_asset[Path(path).name] = dict(metadata)
                if content:
                    by_text.setdefault(
                        self._text_key(page_index, raw_type, content), []
                    ).append(metadata)
                    for alias in self._type_aliases(raw_type):
                        by_text.setdefault(
                            self._text_key(page_index, alias, content), []
                        ).append(metadata)
        return {"by_asset": by_asset, "by_text": by_text}

    def _layout_metadata(self, block: dict[str, Any]) -> dict[str, Any]:
        metadata = {
            "mineru_layout_type": block.get("type"),
            "mineru_block_index": block.get("index"),
        }
        if block.get("level") is not None:
            metadata["mineru_level"] = block.get("level")
        return metadata

    def _content_list_path(self) -> Path | None:
        for pattern in (
            "*content_list_v2.json",
            "content_list_v2.json",
            "*content_list.json",
            "content_list.json",
        ):
            matches = sorted(self.parsed_dir.rglob(pattern))
            if matches:
                return matches[0]
        return None

    def _content_list_text(self, block: dict[str, Any]) -> str:
        content = block.get("content")
        if isinstance(content, dict):
            return self._flatten(content) or json.dumps(content, ensure_ascii=False)
        if isinstance(content, str):
            return content
        parts = []
        for key in ("text", "image_caption", "chart_caption", "table_caption"):
            if block.get(key):
                parts.append(self._flatten(block[key]))
        return " ".join(parts).strip()

    def _content_asset_path(self, block: dict[str, Any]) -> str | None:
        for key in ("img_path", "image_path"):
            path = block.get(key)
            if isinstance(path, str) and path:
                return path
        content = block.get("content")
        source = content.get("image_source") if isinstance(content, dict) else None
        path = source.get("path") if isinstance(source, dict) else None
        return path if isinstance(path, str) and path else None

    def _flatten(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            if isinstance(value.get("content"), str):
                return value["content"]
            return " ".join(
                self._flatten(item)
                for key, item in value.items()
                if key not in {"type", "score", "bbox", "image_source", "level"}
            ).strip()
        if isinstance(value, list):
            return " ".join(self._flatten(item) for item in value).strip()
        return str(value)

    def _type_aliases(self, raw_type: str) -> set[str]:
        aliases = {
            "text": {"paragraph"},
            "paragraph": {"text"},
            "header": {"page_header"},
            "footer": {"page_footer"},
            "interline_equation": {"equation_interline"},
            "equation_interline": {"interline_equation"},
        }
        return aliases.get(raw_type, set())

    def _text_key(self, page: int, raw_type: str, text: str) -> tuple[int, str, str]:
        return (page, raw_type, self._text_norm(text))

    def _text_norm(self, text: str | None) -> str:
        clean = re.sub(r"<[^>]+>", " ", text or "")
        clean = re.sub(r"\s+", " ", clean).strip().lower()
        return clean[:240]

    def _propagate_figures(self, blocks: list[DocumentIRBlock]) -> None:
        by_page: dict[int | None, list[DocumentIRBlock]] = {}
        for block in blocks:
            if block.type in {"image", "chart"}:
                by_page.setdefault(block.page, []).append(block)

        for figures in by_page.values():
            label_sources = [
                item
                for item in figures
                if item.metadata.get("figure_label") and item.metadata.get("caption")
            ]
            if not label_sources:
                continue
            for index, figure in enumerate(figures):
                if figure.metadata.get("figure_label") and figure.metadata.get("caption"):
                    continue
                nearest = min(
                    label_sources,
                    key=lambda item: abs(figures.index(item) - index),
                )
                for key in ("figure_label", "figure_group_id", "caption"):
                    if not figure.metadata.get(key) and nearest.metadata.get(key):
                        figure.metadata[key] = nearest.metadata[key]
                if not figure.metadata.get("caption_source") and figure.metadata.get("caption"):
                    figure.metadata["caption_source"] = "mineru_layout_page"

    def _caption_text(self, block: dict[str, Any], raw_type: str) -> str:
        wanted = {f"{raw_type}_caption", "image_caption", "chart_caption", "table_caption"}
        parts = []
        for child in block.get("blocks") or []:
            if child.get("type") in wanted:
                parts.append(self._text(child))
        return " ".join(part for part in parts if part).strip()

    def _image_paths(self, block: dict[str, Any]) -> list[str]:
        paths = []
        for item in self._walk(block):
            path = item.get("image_path")
            if isinstance(path, str) and path:
                paths.append(path)
        return paths

    def _text(self, value: Any) -> str:
        parts = []
        for item in self._walk(value):
            content = item.get("content")
            if isinstance(content, str):
                parts.append(content)
        return re.sub(r"\s+", " ", " ".join(parts)).strip()

    def _walk(self, value: Any):
        if isinstance(value, dict):
            yield value
            for key in ("blocks", "lines", "spans"):
                for item in value.get(key) or []:
                    yield from self._walk(item)
        elif isinstance(value, list):
            for item in value:
                yield from self._walk(item)

    def _figure_label(self, text: str) -> str | None:
        match = re.search(r"\b(?:fig(?:ure)?\.?|图)\s*([0-9]+[a-zA-Z]?)\b", text or "", re.I)
        return f"Fig. {match.group(1)}" if match else None

    def _panel_label(self, text: str) -> str | None:
        clean = re.sub(r"\s+", " ", text or "").strip()
        if len(clean) > 12:
            return None
        match = re.match(r"^([a-zA-Z])(?:$|[\s,.)])", clean)
        return match.group(1).lower() if match else None
