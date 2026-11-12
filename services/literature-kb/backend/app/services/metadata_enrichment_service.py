from __future__ import annotations

import html
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from app.models.paper import Paper


DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
YEAR_PATTERN = re.compile(r"\b(?:19|20)\d{2}\b")


class MetadataEnrichmentService:
    """Derive bibliographic metadata from MinerU's normalized native structure."""

    def enrich(self, paper: Paper, parsed_dir: Path) -> dict:
        content_map_path = parsed_dir / "content_map.json"
        if not content_map_path.is_file():
            return {"status": "unavailable", "reason": "content_map_missing"}
        document = json.loads(content_map_path.read_text(encoding="utf-8"))
        blocks = document.get("blocks") if isinstance(document, dict) else None
        if not isinstance(blocks, list) or not blocks:
            return {"status": "unavailable", "reason": "native_blocks_missing"}

        first_page = [block for block in blocks if block.get("page") in {0, 1, None}]
        title_index = next((index for index, block in enumerate(first_page) if block.get("type") == "title"), None)
        titles = [self._plain(block.get("content")) for block in first_page if block.get("type") == "title"]
        following = first_page[title_index + 1:] if title_index is not None else first_page
        paragraphs = [self._plain(block.get("content")) for block in following if block.get("type") == "paragraph"]
        header_text = " ".join(
            self._plain(block.get("content"))
            for block in first_page
            if block.get("type") in {"page_header", "page_footer", "page_footnote"}
        )
        all_first_page_text = " ".join(
            self._plain(block.get("content")) for block in first_page
        )

        author_index, authors = next(
            ((index, names) for index, value in enumerate(paragraphs[:6]) if len(names := self._authors(value)) >= 2),
            (None, []),
        )
        abstract_candidates = paragraphs[author_index + 1:] if author_index is not None else paragraphs
        abstract = next((value for value in abstract_candidates if 250 <= len(value) <= 5000), None)
        doi_match = DOI_PATTERN.search(all_first_page_text)
        year_match = YEAR_PATTERN.search(header_text)
        venue = self._venue(first_page)
        metadata = {
            "title": titles[0] if titles else None,
            "authors": authors or None,
            "abstract": abstract,
            "doi": doi_match.group(0).rstrip(".,;)") if doi_match else None,
            "year": int(year_match.group(0)) if year_match else None,
            "venue": venue,
            "language": "en" if sum(character.isascii() and character.isalpha() for character in all_first_page_text) >= 50 else None,
        }

        provenance = dict(paper.metadata_provenance or {})
        changed = []
        for field, value in metadata.items():
            if value is None or value == "" or value == []:
                continue
            current_source = provenance.get(field)
            if current_source == "manifest":
                continue
            if not getattr(paper, field) or current_source in {None, "filename", "mineru_native"}:
                setattr(paper, field, value)
                provenance[field] = "mineru_native"
                changed.append(field)
        paper.metadata_provenance = provenance
        paper.updated_at = datetime.now(UTC)
        return {"status": "enriched", "fields": sorted(changed), "source": "mineru_native"}

    @staticmethod
    def _plain(value) -> str:
        if not isinstance(value, str):
            return ""
        value = re.sub(r"<sup\b[^>]*>.*?</sup>", " ", value, flags=re.IGNORECASE | re.DOTALL)
        return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())

    def _authors(self, value: str) -> list[dict[str, str]]:
        if not value or len(value) > 2000:
            return []
        names = re.split(r",\s*|\s+(?:and|&)\s+", value)
        cleaned = [name.strip(" *†‡,;") for name in names]
        cleaned = [name for name in cleaned if 2 <= len(name) <= 100 and len(name.split()) >= 2]
        return [{"name": name} for name in cleaned] if len(cleaned) >= 2 else []

    def _venue(self, blocks: list[dict]) -> str | None:
        candidates = [
            self._plain(block.get("content"))
            for block in blocks
            if block.get("type") in {"page_header", "page_footer"}
        ]
        known = {
            "scientific reports": "Scientific Reports",
            "nature communications": "Nature Communications",
            "nature nanotechnology": "Nature Nanotechnology",
            "nature energy": "Nature Energy",
            "nature materials": "Nature Materials",
            "nature chemistry": "Nature Chemistry",
            "communications chemistry": "Communications Chemistry",
        }
        lowered_candidates = [candidate.lower() for candidate in candidates]
        for key, label in known.items():
            if any(key in candidate for candidate in lowered_candidates):
                return label
        for candidate in candidates:
            lowered = candidate.lower()
            if lowered == "nature" or lowered.startswith("nature ") and "portfolio" not in lowered:
                match = re.search(r"nature\s+[a-z]+", candidate, re.IGNORECASE)
                return match.group(0).title() if match else "Nature"
        return None
