from __future__ import annotations

import json
import time
from hashlib import sha256
from pathlib import Path
from zipfile import ZipFile

import requests
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.paper import Paper
from app.models.paper_content import PaperContent
from app.models.paper_task import PaperTask
from app.services.document_structure_fusion import DocumentStructureFusionService
from app.services.mineru_structure_enhancer import (
    MinerUNativeStructureEnhancer,
    StructureEnhancementInput,
)


class MinerUError(Exception):
    pass


class MinerUService:
    """Run one real MinerU parse task and persist only v1 contract fields."""

    pending_states = {"waiting-file", "uploading", "pending", "running", "converting"}

    def __init__(self, db: Session, settings: Settings, http_client=requests) -> None:
        self.db, self.settings, self.http = db, settings, http_client
        self.root = Path(settings.storage_root).resolve()

    def run_task(self, paper: Paper, task: PaperTask, force: bool = False) -> dict:
        cached = self.cached_result(paper, task, force=force)
        if cached is not None:
            return cached
        batch_id = self.submit_parse(paper, task)
        deadline = time.monotonic() + 20 * 60
        while time.monotonic() < deadline:
            parsed = self.poll_parse_once(task, batch_id)
            if parsed.get("state") in self.pending_states:
                time.sleep(5)
                continue
            return self.finalize_parse(paper, task, parsed)
        error = MinerUError(
            "MinerU parse timed out after 20 minutes; the remote batch will be resumed."
        )
        error.error_code, error.retryable = "PROVIDER_TIMEOUT", True
        raise error

    def cached_result(
        self, paper: Paper, task: PaperTask, *, force: bool = False
    ) -> dict | None:
        if not self.settings.mineru_api_token:
            raise MinerUError("MINERU_API_TOKEN is not configured.")
        if not paper.pdf_storage_key:
            raise MinerUError("Paper has no PDF storage key.")
        pdf_path = self._path(paper.pdf_storage_key)
        if not pdf_path.is_file():
            raise MinerUError("Paper PDF file does not exist.")

        details = dict(task.details or {})
        provider = dict(details.get("provider") or {})
        result = dict(details.get("result") or {})
        existing_content = self.db.get(PaperContent, paper.paper_id)
        if (
            not force
            and existing_content
            and (
                provider.get("state") == "done"
                or paper.parse_status == "succeeded"
            )
        ):
            result.update({"skipped": True})
            details["result"] = result
            task.details = details
            paper.parse_status = "succeeded"
            return result
        return None

    def submit_parse(self, paper: Paper, task: PaperTask) -> str:
        if not self.settings.mineru_api_token:
            raise MinerUError("MINERU_API_TOKEN is not configured.")
        if not paper.pdf_storage_key:
            raise MinerUError("Paper has no PDF storage key.")
        pdf_path = self._path(paper.pdf_storage_key)
        if not pdf_path.is_file():
            raise MinerUError("Paper PDF file does not exist.")
        details = dict(task.details or {})
        provider = dict(details.get("provider") or {})
        batch_id = provider.get("batch_id")
        if batch_id:
            return str(batch_id)
        token = self.settings.mineru_api_token
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        base = self.settings.mineru_base_url.rstrip("/")
        task.progress = 0.05
        payload = {
            "files": [{"name": pdf_path.name, "data_id": paper.paper_id}],
            "model_version": self.settings.mineru_model_version,
            "language": self.settings.mineru_language,
            "enable_table": self.settings.mineru_enable_table,
            "enable_formula": self.settings.mineru_enable_formula,
            "is_ocr": self.settings.mineru_is_ocr,
        }
        self.db.commit()
        body = self._json(
            self.http.post(
                f"{base}/api/v4/file-urls/batch",
                headers=headers,
                json=payload,
                timeout=30,
            )
        )
        batch_id = body["data"]["batch_id"]
        upload_url = body["data"]["file_urls"][0]
        provider.update({"name": "mineru", "batch_id": batch_id, "state": "uploading"})
        details["provider"] = provider
        task.details = details
        self.db.commit()

        try:
            with pdf_path.open("rb") as stream:
                upload = self.http.put(upload_url, data=stream, timeout=180)
            if upload.status_code not in {200, 201, 204}:
                error = MinerUError(f"MinerU upload failed: {upload.status_code}")
                error.error_code, error.retryable = "PROVIDER_UNAVAILABLE", True
                raise error
        except (requests.RequestException, MinerUError):
            provider.pop("batch_id", None)
            provider["state"] = "upload_failed"
            task.details = {**dict(task.details or {}), "provider": provider}
            self.db.commit()
            raise
        provider["state"] = "pending"
        task.details = {**dict(task.details or {}), "provider": provider}
        self.db.commit()
        return str(batch_id)

    def poll_parse_once(self, task: PaperTask, batch_id: str | None = None) -> dict:
        details = dict(task.details or {})
        provider = dict(details.get("provider") or {})
        resolved_batch_id = batch_id or provider.get("batch_id")
        if not resolved_batch_id:
            raise MinerUError("MinerU provider batch ID is missing.")
        token = self.settings.mineru_api_token
        if not token:
            raise MinerUError("MINERU_API_TOKEN is not configured.")
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        base = self.settings.mineru_base_url.rstrip("/")
        self.db.commit()
        body = self._json(
            self.http.get(
                f"{base}/api/v4/extract-results/batch/{resolved_batch_id}",
                headers=headers,
                timeout=30,
            )
        )
        parsed = self._result(body)
        provider["state"] = parsed.get("state")
        details["provider"] = provider
        task.details = details
        if parsed.get("state") in self.pending_states:
            task.progress = self._progress(parsed)
        self.db.commit()
        return parsed

    def finalize_parse(self, paper: Paper, task: PaperTask, parsed: dict) -> dict:
        details = dict(task.details or {})
        provider = dict(details.get("provider") or {})
        state = parsed.get("state")
        if state != "done":
            provider.pop("batch_id", None)
            details["provider"] = provider
            task.details = details
            self.db.commit()
            error = MinerUError(
                parsed.get("err_msg") or f"MinerU parse ended with {state!r}."
            )
            error.error_code, error.retryable = "PROVIDER_UNAVAILABLE", True
            raise error
        zip_url = parsed.get("full_zip_url")
        if not zip_url:
            raise MinerUError("MinerU completed without full_zip_url.")
        paper_id = paper.paper_id
        self.db.commit()
        parsed_dir = self._download(paper_id, zip_url)
        content_key, content_map_key, text = self._build_content(
            paper_id, parsed_dir
        )
        self._upsert_content(paper_id, text)
        provider["state"] = "done"
        details["provider"] = provider
        details["artifacts"] = {
            "parsed_root_key": f"parsed/{paper_id}",
            "content_key": content_key,
            "content_map_key": content_map_key,
        }
        details["result"] = {"chars": len(text), "skipped": False}
        task.details, task.progress = details, 1.0
        paper.parse_status = "succeeded"
        paper.parse_version = self.settings.mineru_model_version
        self.db.commit()
        return details["result"]

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise MinerUError("Invalid storage key.")
        return path

    def _json(self, response) -> dict:
        response.raise_for_status()
        body = response.json()
        if body.get("code") != 0:
            raise MinerUError(body.get("msg") or "MinerU request failed.")
        return body

    def _result(self, body: dict) -> dict:
        rows = body.get("data", {}).get("extract_result") or []
        if not rows:
            raise MinerUError("MinerU batch result is empty.")
        return rows[0]

    def _progress(self, result: dict) -> float:
        progress = result.get("extract_progress") or {}
        total, done = progress.get("total_pages") or 0, progress.get("extracted_pages") or 0
        return min(0.9, 0.2 + 0.7 * done / total) if total else 0.2

    def _download(self, paper_id: str, url: str) -> Path:
        target = self.root / "parsed" / paper_id
        target.mkdir(parents=True, exist_ok=True)
        archive_path = target / "mineru_result.zip"
        response = self.http.get(url, timeout=180)
        response.raise_for_status()
        archive_path.write_bytes(response.content)
        root = target.resolve()
        with ZipFile(archive_path) as archive:
            for item in archive.infolist():
                if not (target / item.filename).resolve().is_relative_to(root):
                    raise MinerUError("Unsafe path found in MinerU archive.")
            archive.extractall(target)
        return target

    def _build_content(self, paper_id: str, parsed_dir: Path) -> tuple[str, str, str]:
        document = DocumentStructureFusionService(parsed_dir).build()
        text_parts, mapping, offset = [], [], 0
        for order, block in enumerate(document.blocks):
            # PostgreSQL text/JSON values cannot contain NUL even though MinerU
            # may preserve it from malformed PDF font streams.
            text = block.content.replace("\x00", "").strip()
            if not text:
                continue
            if text_parts:
                text_parts.append("\n\n")
                offset += 2
            start = offset
            text_parts.append(text)
            offset += len(text)
            raw_bbox = block.bbox if self._valid_mineru_bbox(block.bbox) else None
            normalized = [value / 1000 for value in raw_bbox] if raw_bbox else None
            mapping.append({
                "source_block_id": str(block.metadata.get("source_block_id") or f"p0_b{order}"),
                "char_start": start,
                "char_end": offset,
                "page": block.page,
                "type": block.type or "paragraph",
                "bbox": raw_bbox,
                "normalized_bbox": normalized,
                "raw_coordinate_space": "mineru_0_1000_top_left" if raw_bbox else None,
                "asset_path": block.img_path,
                "metadata": block.metadata,
                "content": text,
            })
        text = "".join(text_parts)
        if not text:
            raise MinerUError("MinerU content list produced no readable text.")
        enhancement = MinerUNativeStructureEnhancer().enhance(StructureEnhancementInput(paper_id, text, {"blocks": mapping}, {"parsed_root_key": f"parsed/{paper_id}"}, ()))
        sections = {item.anchor.source_block_id: item for item in enhancement.sections}
        callouts: dict[str, list[dict]] = {}
        for item in enhancement.callouts:
            callouts.setdefault(item.anchor.source_block_id, []).append({"kind": item.kind, "label": item.label, "char_start": item.anchor.char_start, "char_end": item.anchor.char_end, "confidence": item.anchor.confidence, "provenance": item.anchor.provenance})
        for block in mapping:
            source_id = block["source_block_id"]
            if section := sections.get(source_id):
                block["metadata"].update({"section_title": section.title, "section_type": section.section_type, "structure_source": section.anchor.provenance})
            if source_id in callouts:
                block["metadata"]["mineru_native_callouts"] = callouts[source_id]
        content_key, map_key = f"parsed/{paper_id}/canonical_content.txt", f"parsed/{paper_id}/content_map.json"
        self._path(content_key).write_text(text, encoding="utf-8")
        self._path(map_key).write_text(json.dumps({"version": 2, "source": document.metadata.get("document_ir_source_file"), "structure": {"version": MinerUNativeStructureEnhancer.version, "references": [{"raw_text": item.raw_text, "source_block_id": item.anchor.source_block_id, "char_start": item.anchor.char_start, "char_end": item.anchor.char_end, "page": item.anchor.page, "bbox": item.anchor.bbox, "doi": item.doi, "confidence": item.anchor.confidence, "provenance": item.anchor.provenance} for item in enhancement.references], "citation_contexts": [{"marker": item.marker, "quote": item.quote, "source_block_id": item.anchor.source_block_id, "char_start": item.anchor.char_start, "char_end": item.anchor.char_end, "page": item.anchor.page, "confidence": item.anchor.confidence, "provenance": item.anchor.provenance, "reference_index": item.reference_index} for item in enhancement.citation_contexts]}, "blocks": mapping}, ensure_ascii=False), encoding="utf-8")
        return content_key, map_key, text

    def rebuild_content_map(self, paper: Paper) -> tuple[str, str, str]:
        parsed_dir = self.root / "parsed" / paper.paper_id
        content_key, map_key, text = self._build_content(paper.paper_id, parsed_dir)
        self._upsert_content(paper.paper_id, text)
        self.db.commit()
        return content_key, map_key, text

    def _upsert_content(self, paper_id: str, text: str) -> None:
        record = self.db.get(PaperContent, paper_id)
        digest = sha256(text.encode("utf-8")).hexdigest()
        if record:
            record.text, record.content_sha256, record.char_length = text, digest, len(text)
        else:
            self.db.add(PaperContent(paper_id=paper_id, text=text, content_sha256=digest, char_length=len(text)))

    def _valid_mineru_bbox(self, value) -> bool:
        return bool(
            isinstance(value, list)
            and len(value) == 4
            and all(isinstance(item, int | float) for item in value)
            and 0 <= value[0] < value[2] <= 1000
            and 0 <= value[1] < value[3] <= 1000
        )
