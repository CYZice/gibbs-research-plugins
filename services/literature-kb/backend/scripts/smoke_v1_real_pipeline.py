"""Resumable, real-provider smoke test for the Literature KB v1 pipeline.

Run from ``backend`` after configuring a real .env:
  python scripts/smoke_v1_real_pipeline.py --pdf-primary C:\\paper.pdf --start-local
  python scripts/smoke_v1_real_pipeline.py --resume paper_xxx

Exit codes: 0 all checks passed; 2 only external provider was temporarily blocked;
1 contract/data/application failure.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import create_engine, text as sql

ROOT = Path(__file__).parents[1]
WORKSPACE = ROOT.parent
DEFAULT_REPORT = WORKSPACE / "storage" / "smoke_v1_real_pipeline_report.json"
EXTERNAL_CODES = {429, 500, 502, 503, 504}


class SmokeFailure(RuntimeError):
    pass


class ExternalProviderBlocked(RuntimeError):
    pass


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


@dataclass
class Report:
    path: Path
    data: dict[str, Any] = field(default_factory=lambda: {"version": 2, "stages": {}, "external_blocks": []})

    @classmethod
    def load(cls, path: Path) -> "Report":
        if path.exists():
            return cls(path, json.loads(path.read_text(encoding="utf-8")))
        return cls(path)

    def stage(self, name: str, status: str, **details: Any) -> None:
        self.data.setdefault("stages", {})[name] = {"status": status, "at": time.time(), **details}
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")


class SmokeRunner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.report = Report.load(args.report.resolve())
        self.primary = args.pdf_primary.resolve() if args.pdf_primary else None
        self.table = args.pdf_table.resolve() if args.pdf_table else None
        self.base = args.api_base.rstrip("/")
        self.storage = Path(os.environ.get("STORAGE_ROOT", "../storage")).resolve()
        self.engine = create_engine(os.environ.get("DATABASE_URL", "postgresql+psycopg://literature:literature@localhost:5432/literature_kb"))
        self.paper_id = args.resume or self.report.data.get("primary_paper_id")
        self.external_blocked = False
        self.started: list[subprocess.Popen] = []

    def db_rows(self, statement: str, **params: Any) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(sql(statement), params).mappings()]

    async def api(self, client: httpx.AsyncClient, method: str, path: str, *, retries: bool = False, **kwargs: Any) -> Any:
        for attempt in range(4 if retries else 1):
            response = await client.request(method, f"{self.base}{path}", **kwargs)
            if response.status_code < 400:
                return response.json()
            if retries and response.status_code in EXTERNAL_CODES and attempt < 3:
                await asyncio.sleep(15 * (attempt + 1))
                continue
            if response.status_code in EXTERNAL_CODES and ("429" in response.text or "provider" in response.text.lower()):
                self.external_blocked = True
                block = {"path": path, "status": response.status_code, "body": response.text[:1200], "attempts": attempt + 1}
                self.report.data.setdefault("external_blocks", []).append(block)
                self.report.save()
                raise ExternalProviderBlocked(json.dumps(block, ensure_ascii=False))
            raise SmokeFailure(f"{method} {path} failed ({response.status_code}): {response.text[:1200]}")
        raise AssertionError("unreachable")

    async def expect_error(self, client: httpx.AsyncClient, method: str, path: str, expected: int, **kwargs: Any) -> None:
        response = await client.request(method, f"{self.base}{path}", **kwargs)
        require(response.status_code == expected, f"{method} {path} expected {expected}, got {response.status_code}: {response.text[:600]}")

    async def wait_task(self, client: httpx.AsyncClient, task_id: str, stage: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.args.timeout_seconds
        while time.monotonic() < deadline:
            task = await self.api(client, "GET", f"/api/v1/tasks/{task_id}")
            if task["status"] == "succeeded":
                self.report.stage(stage, "passed", task_id=task_id, result=task.get("details", {}).get("result", {}))
                return task
            if task["status"] in {"failed", "stale"}:
                raise SmokeFailure(f"{stage} task {task_id} {task['status']}: {task.get('error_message')}")
            await asyncio.sleep(3)
        raise SmokeFailure(f"{stage} task {task_id} timed out")

    def runtime_checks(self) -> None:
        for name in ("MINERU_API_TOKEN", "EMBEDDING_PROVIDER", "STRUCTURED_PROVIDER", "LLM_PROVIDER"):
            value = os.environ.get(name, "")
            require(value and value.lower() != "fake", f"{name} must configure a real provider for live smoke")
        require(os.environ.get("EMBEDDING_DIMENSION", "1024") == "1024", "EMBEDDING_DIMENSION must be 1024")
        with self.engine.connect() as connection:
            require(connection.execute(sql("SELECT 1")).scalar_one() == 1, "database query failed")
            require(connection.execute(sql("SELECT extname FROM pg_extension WHERE extname = 'vector'")).scalar_one() == "vector", "pgvector extension missing")

    def start_local(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            require(probe.connect_ex(("127.0.0.1", 8000)) != 0, "port 8000 is already in use; omit --start-local or stop the existing API")
        runtime = ROOT / ".runtime"
        runtime.mkdir(exist_ok=True)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        for name, command in (("api", [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"]), ("worker", [sys.executable, "-m", "app.worker"])):
            out = (runtime / f"smoke_{name}.out.log").open("w", encoding="utf-8")
            err = (runtime / f"smoke_{name}.err.log").open("w", encoding="utf-8")
            self.started.append(subprocess.Popen(command, cwd=ROOT, stdout=out, stderr=err, creationflags=flags))
        self.report.data["started_processes"] = [item.pid for item in self.started]
        self.report.save()

    async def health(self, client: httpx.AsyncClient) -> None:
        for _ in range(30):
            try:
                health = await self.api(client, "GET", "/health")
                if health.get("status") in {"ok", "healthy"}:
                    self.report.stage("runtime", "passed", health=health)
                    return
            except Exception:
                pass
            await asyncio.sleep(1)
        raise SmokeFailure("API health did not become ready")

    async def upload(self, client: httpx.AsyncClient, pdf: Path) -> dict[str, Any]:
        data = pdf.read_bytes()
        return await self.api(client, "POST", "/api/v1/papers", files={"file": (pdf.name, data, "application/pdf")}, data={"title": pdf.stem})

    async def upload_constraints(self, client: httpx.AsyncClient) -> None:
        before = self.db_rows("SELECT count(*) AS count FROM papers")[0]["count"]
        await self.expect_error(client, "POST", "/api/v1/papers", 400, files={"file": ("invalid.txt", b"not a pdf", "text/plain")})
        await self.expect_error(client, "POST", "/api/v1/papers", 400, files={"file": ("empty.pdf", b"", "application/pdf")})
        max_bytes = int(os.environ.get("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
        await self.expect_error(client, "POST", "/api/v1/papers", 400, files={"file": ("too_large.pdf", io.BytesIO(b"0" * (max_bytes + 1)), "application/pdf")})
        after = self.db_rows("SELECT count(*) AS count FROM papers")[0]["count"]
        require(before == after, "invalid uploads created Paper records")
        self.report.stage("upload_constraints", "passed")

    async def dedupe(self, client: httpx.AsyncClient) -> str:
        require(self.primary and self.primary.is_file(), "--pdf-primary must exist unless --resume is supplied")
        responses = await asyncio.gather(self.upload(client, self.primary), self.upload(client, self.primary))
        ids = {item["paper"]["paper_id"] for item in responses}
        require(len(ids) == 1, f"concurrent upload created multiple Papers: {ids}")
        sequential = await self.upload(client, self.primary)
        require(sequential["paper"]["paper_id"] in ids, "sequential duplicate returned another Paper")
        require(any((item.get("task", {}).get("details", {}).get("result", {}) or {}).get("duplicate") for item in [*responses, sequential]), "duplicate upload never reported duplicate=true")
        paper_id = next(iter(ids))
        rows = self.db_rows("SELECT count(*) AS papers, count(DISTINCT pdf_storage_key) AS keys FROM papers WHERE pdf_sha256 = (SELECT pdf_sha256 FROM papers WHERE paper_id = :paper_id)", paper_id=paper_id)[0]
        require(rows["papers"] == 1 and rows["keys"] == 1, f"dedupe database invariant failed: {rows}")
        if self.table:
            other = await self.upload(client, self.table)
            require(other["paper"]["paper_id"] != paper_id, "different PDF was incorrectly deduplicated")
        self.report.data["primary_paper_id"] = paper_id
        self.report.stage("dedupe", "passed", paper_id=paper_id)
        return paper_id

    async def operation(self, client: httpx.AsyncClient, operation: str, stage: str, **body: Any) -> dict[str, Any]:
        task = await self.api(client, "POST", f"/api/v1/papers/{self.paper_id}/operations/{operation}", json=body)
        return await self.wait_task(client, task["task_id"], stage)

    def validate_ir(self) -> None:
        parsed = self.storage / "parsed" / str(self.paper_id)
        payload = json.loads((parsed / "content_map.json").read_text(encoding="utf-8"))
        content = (parsed / "canonical_content.txt").read_text(encoding="utf-8")
        require(payload.get("version") == 2, "content-map must be v2")
        blocks = payload.get("blocks") or []
        pages: set[int] = set()
        for block in blocks:
            start, end = block.get("char_start"), block.get("char_end")
            require(isinstance(start, int) and isinstance(end, int) and content[start:end] == block.get("content"), f"invalid canonical range: {block.get('source_block_id')}")
            if isinstance(block.get("page"), int):
                pages.add(block["page"])
            bbox = block.get("normalized_bbox")
            if bbox is not None:
                require(len(bbox) == 4 and 0 <= bbox[0] < bbox[2] <= 1 and 0 <= bbox[1] < bbox[3] <= 1, f"invalid normalized bbox: {block.get('source_block_id')}")
                require(block.get("bbox") and block.get("raw_coordinate_space") == "mineru_0_1000_top_left", "normalized bbox lost MinerU provenance")
            asset_path = block.get("asset_path")
            if asset_path:
                require((parsed / asset_path).resolve().is_relative_to(parsed.resolve()), "asset path escaped parsed root")
        require(len(pages) >= 2, f"expected multi-page IR, got pages={pages}")
        structure = payload.get("structure") or {}
        refs = self.db_rows("SELECT count(*) AS count FROM paper_references WHERE paper_id = :paper_id", paper_id=self.paper_id)[0]["count"]
        cites = self.db_rows("SELECT count(*) AS count FROM citation_contexts WHERE paper_id = :paper_id", paper_id=self.paper_id)[0]["count"]
        require(refs == len(structure.get("references") or []), "PaperReference count differs from content-map")
        require(cites == len(structure.get("citation_contexts") or []), "CitationContext count differs from content-map")
        content_row = self.db_rows("SELECT text, content_sha256, char_length FROM paper_contents WHERE paper_id = :paper_id", paper_id=self.paper_id)[0]
        require(content_row["text"] == content and content_row["char_length"] == len(content) and content_row["content_sha256"] == hashlib.sha256(content.encode()).hexdigest(), "PaperContent does not match canonical artifact")
        self.report.stage("mineru_ir", "passed", blocks=len(blocks), pages=sorted(pages), references=refs, citation_contexts=cites)

    def validate_evidence_assets(self) -> None:
        rows = self.db_rows("SELECT modality, count(*) AS count FROM evidence_units WHERE paper_id = :paper_id GROUP BY modality", paper_id=self.paper_id)
        counts = {row["modality"]: row["count"] for row in rows}
        required = set(filter(None, self.args.require_modalities.split(",")))
        if self.table:
            required.add("table")
        missing = sorted(kind for kind in required if not counts.get(kind))
        if missing and self.args.strict:
            raise SmokeFailure(f"required modalities missing from MinerU output: {missing}")
        assets = self.db_rows("SELECT asset_type, count(*) AS count FROM resource_assets WHERE paper_id = :paper_id GROUP BY asset_type", paper_id=self.paper_id)
        for row in self.db_rows("SELECT raw_content, char_start, char_end, exclude_from_retrieval, block_type FROM evidence_units WHERE paper_id = :paper_id", paper_id=self.paper_id):
            if row["block_type"] in {"page_header", "page_footer", "page_number"}:
                require(row["exclude_from_retrieval"] is True, "page chrome is indexable")
        self.report.stage("evidence_assets", "passed", modalities=counts, assets={item["asset_type"]: item["count"] for item in assets}, missing_modalities=missing)

    def validate_multimodal(self, task: dict[str, Any]) -> None:
        provider = task.get("details", {}).get("provider", {})
        require(provider.get("vision_called") is False, "multimodal task reported Vision usage")
        rows = self.db_rows("""
            SELECT e.modality, r.repr_type, r.status, r.text, r.metadata
            FROM evidence_representations r JOIN evidence_units e ON e.evidence_id = r.evidence_id
            WHERE e.paper_id = :paper_id AND r.repr_type IN ('figure_caption_context','table_structured','formula_structured')
        """, paper_id=self.paper_id)
        figures = [row for row in rows if row["modality"] == "figure"]
        if figures:
            require(all((row["metadata"] or {}).get("vision_called") is False for row in figures), "figure metadata recorded Vision")
            require(any("Nearby text:" in row["text"] for row in figures), "figure context lacks nearby canonical text")
        for kind in ("table", "formula"):
            found = [row for row in rows if row["modality"] == kind]
            if found:
                require(all(row["status"] == "success" and (row["metadata"] or {}).get("method") == "structured_text" for row in found), f"{kind} structured output is incomplete")
        self.report.stage("multimodal", "passed", representations=len(rows), figures=len(figures), vision_called=False)

    async def embed_and_freshness(self, client: httpx.AsyncClient) -> None:
        initial = await self.operation(client, "embed", "embed_initial")
        result = initial.get("details", {}).get("result", {})
        require(result.get("embedded", 0) > 0 or result.get("skipped", 0) > 0, "embed processed no representation")
        rows = self.db_rows("""
            SELECT em.embedding_dim, em.embedding_model, count(*) AS count
            FROM evidence_embeddings em JOIN evidence_representations r ON r.representation_id = em.representation_id
            JOIN evidence_units e ON e.evidence_id = r.evidence_id
            WHERE e.paper_id = :paper_id AND em.status = 'success'
            GROUP BY em.embedding_dim, em.embedding_model
        """, paper_id=self.paper_id)
        require(rows and all(row["embedding_dim"] == 1024 and "fake" not in row["embedding_model"].lower() for row in rows), f"invalid embedding rows: {rows}")
        again = await self.operation(client, "embed", "embed_freshness")
        fresh = again.get("details", {}).get("result", {})
        require(fresh.get("embedded", 0) == 0 and fresh.get("skipped", 0) > 0, f"second embed was not fresh: {fresh}")

    async def incremental_probes(self, client: httpx.AsyncClient) -> None:
        parsed = self.storage / "parsed" / str(self.paper_id)
        map_path, text_path = parsed / "content_map.json", parsed / "canonical_content.txt"
        original_map, original_text = map_path.read_bytes(), text_path.read_text(encoding="utf-8")
        original_content_hash = hashlib.sha256(original_text.encode()).hexdigest()
        try:
            payload = json.loads(original_map)
            target = next(item for item in payload["blocks"] if item.get("normalized_bbox") and item.get("type") not in {"page_header", "page_footer", "page_number"})
            target["normalized_bbox"] = [0.01, 0.01, 0.99, 0.99]
            map_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            location = await self.operation(client, "build_evidence", "probe_location", force=True)
            require(location["details"]["result"]["updated"] >= 1, "location probe did not update Evidence")
            location_embed = await self.operation(client, "embed", "probe_location_embed")
            require(location_embed["details"]["result"]["embedded"] == 0, "bbox-only probe re-embedded text")

            payload = json.loads(original_map)
            target = next(item for item in payload["blocks"] if item.get("type") in {"paragraph", "text", "list"} and len(str(item.get("content"))) > 20)
            start = target["char_start"]
            replacement = "X" if original_text[start] != "X" else "Y"
            changed_text = original_text[:start] + replacement + original_text[start + 1:]
            target["content"] = replacement + target["content"][1:]
            map_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            text_path.write_text(changed_text, encoding="utf-8")
            with self.engine.begin() as connection:
                connection.execute(sql("UPDATE paper_contents SET text=:text, content_sha256=:digest, char_length=:length WHERE paper_id=:paper_id"), {"text": changed_text, "digest": hashlib.sha256(changed_text.encode()).hexdigest(), "length": len(changed_text), "paper_id": self.paper_id})
            changed = await self.operation(client, "build_evidence", "probe_text", force=True)
            require(changed["details"]["result"]["updated"] >= 1, "text probe did not update Evidence")
            changed_embed = await self.operation(client, "embed", "probe_text_embed")
            require(changed_embed["details"]["result"]["embedded"] >= 1, "text probe did not re-embed affected representation")

            payload = json.loads(original_map)
            removed = next(item for item in payload["blocks"] if item.get("type") in {"image", "chart", "table", "equation_interline"})
            payload["blocks"] = [item for item in payload["blocks"] if item is not removed]
            map_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.engine.begin() as connection:
                connection.execute(sql("UPDATE paper_contents SET text=:text, content_sha256=:digest, char_length=:length WHERE paper_id=:paper_id"), {"text": original_text, "digest": original_content_hash, "length": len(original_text), "paper_id": self.paper_id})
            text_path.write_text(original_text, encoding="utf-8")
            deleted = await self.operation(client, "build_evidence", "probe_delete", force=True)
            require(deleted["details"]["result"]["deleted"] >= 1, "delete probe did not remove derived Evidence")
            require(self.db_rows("SELECT count(*) AS count FROM resource_assets WHERE paper_id=:paper_id AND asset_type='pdf'", paper_id=self.paper_id)[0]["count"] == 1, "delete probe removed original PDF asset")
            self.report.stage("synthetic_incremental_probe", "passed")
        finally:
            map_path.write_bytes(original_map)
            text_path.write_text(original_text, encoding="utf-8")
            with self.engine.begin() as connection:
                connection.execute(sql("UPDATE paper_contents SET text=:text, content_sha256=:digest, char_length=:length WHERE paper_id=:paper_id"), {"text": original_text, "digest": original_content_hash, "length": len(original_text), "paper_id": self.paper_id})
            await self.operation(client, "build_evidence", "restore_after_probe", force=True)
            await self.operation(client, "embed", "restore_after_probe_embed")

    async def agent_flow(self, client: httpx.AsyncClient) -> None:
        search = await self.api(client, "POST", "/api/v1/evidence/search", json={"query": "non-flammable electrolyte lithium metal batteries", "scope": {"paper_ids": [self.paper_id]}, "filters": {}, "page": 1, "page_size": 8})
        hits = search.get("items") or []
        require(hits and all(item["paper"]["paper_id"] == self.paper_id for item in hits), "scoped search returned no or out-of-scope hits")
        unscoped = await self.api(client, "POST", "/api/v1/evidence/search", json={"query": "non-flammable electrolyte lithium metal batteries", "scope": {}, "filters": {}, "page": 1, "page_size": 8})
        require(unscoped.get("items"), "unscoped search returned no ready evidence")
        await self.expect_error(client, "POST", "/api/v1/evidence/search", 400, json={"query": "x", "scope": {"evidence_ids": ["unknown"]}, "filters": {}})
        ids = [item["evidence_id"] for item in hits[:3]]
        content = await self.api(client, "GET", f"/api/v1/evidence/{ids[0]}/content?before=300&after=600")
        quote = next(item for item in hits if item["evidence_id"] == ids[0])["content"]["quote"]
        require(quote in content["content"]["text"], "content window omitted selected quote")
        source = await self.api(client, "GET", f"/api/v1/evidence/{ids[0]}/source")
        require(source.get("pdf_asset_id"), "source omitted PDF asset")
        asset_rows = self.db_rows("SELECT asset_id FROM resource_assets WHERE paper_id=:paper_id AND asset_type IN ('figure','table','formula') LIMIT 3", paper_id=self.paper_id)
        for row in asset_rows:
            asset = await self.api(client, "GET", f"/api/v1/assets/{row['asset_id']}")
            response = await client.get(f"{self.base}/api/v1/assets/{row['asset_id']}/download")
            require(response.status_code == 200 and response.content, f"asset download failed: {asset['asset_id']}")
        pack = await self.api(client, "POST", "/api/v1/evidence/packs", json={"question": "What supports the claim?", "evidence_ids": ids})
        require({item["evidence_id"] for item in pack.get("items", [])} == set(ids), "pack used unrequested Evidence")
        await self.expect_error(client, "POST", "/api/v1/evidence/packs", 400, json={"question": "x", "evidence_ids": [], "top_k": 5})
        try:
            ground = await self.api(client, "POST", "/api/v1/citations/ground", retries=True, json={"claims": [{"claim_id": "claim_1", "text": "The paper reports a non-flammable electrolyte."}], "evidence_ids": ids[:2]})
            citations = ground["items"][0].get("citations", [])
            require(all(item["evidence_id"] in ids[:2] for item in citations), "ground returned an unrequested citation")
            require(not (ground["items"][0].get("status") == "supported" and not citations), "ground reported unsupported success without citation")
            self.report.stage("agent_flow", "passed", hits=len(hits), ground_status=ground["items"][0]["status"])
        except ExternalProviderBlocked:
            self.report.stage("agent_flow", "blocked_external", hits=len(hits))
            if self.args.strict:
                raise

    def cleanup(self) -> None:
        """Delete only the Paper created by this smoke run and its derived storage."""
        if not self.paper_id:
            return
        with self.engine.begin() as connection:
            connection.execute(sql("UPDATE paper_references SET target_paper_id=NULL WHERE target_paper_id=:paper_id"), {"paper_id": self.paper_id})
            connection.execute(sql("DELETE FROM citation_contexts WHERE paper_id=:paper_id OR evidence_id IN (SELECT evidence_id FROM evidence_units WHERE paper_id=:paper_id)"), {"paper_id": self.paper_id})
            connection.execute(sql("DELETE FROM evidence_embeddings WHERE representation_id IN (SELECT representation_id FROM evidence_representations WHERE evidence_id IN (SELECT evidence_id FROM evidence_units WHERE paper_id=:paper_id))"), {"paper_id": self.paper_id})
            connection.execute(sql("DELETE FROM resource_assets WHERE paper_id=:paper_id"), {"paper_id": self.paper_id})
            connection.execute(sql("DELETE FROM evidence_representations WHERE evidence_id IN (SELECT evidence_id FROM evidence_units WHERE paper_id=:paper_id)"), {"paper_id": self.paper_id})
            connection.execute(sql("DELETE FROM evidence_units WHERE paper_id=:paper_id"), {"paper_id": self.paper_id})
            connection.execute(sql("DELETE FROM paper_references WHERE paper_id=:paper_id"), {"paper_id": self.paper_id})
            connection.execute(sql("DELETE FROM paper_contents WHERE paper_id=:paper_id"), {"paper_id": self.paper_id})
            connection.execute(sql("DELETE FROM paper_tasks WHERE paper_id=:paper_id"), {"paper_id": self.paper_id})
            connection.execute(sql("DELETE FROM papers WHERE paper_id=:paper_id"), {"paper_id": self.paper_id})
        for target in (self.storage / "pdfs" / f"{self.paper_id}.pdf", self.storage / "parsed" / str(self.paper_id)):
            if target.exists() and target.resolve().is_relative_to(self.storage):
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
        self.report.stage("cleanup", "passed", paper_id=self.paper_id)

    async def run(self) -> int:
        self.runtime_checks()
        if self.args.reset:
            subprocess.run(["powershell", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts" / "reset_development_state.ps1"), "-ConfirmReset"], cwd=ROOT, check=True)
        if self.args.start_local:
            self.start_local()
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
            await self.health(client)
            if not self.paper_id:
                await self.upload_constraints(client)
                self.paper_id = await self.dedupe(client)
            await self.operation(client, "parse", "parse")
            await self.operation(client, "build_evidence", "build_evidence")
            self.validate_ir()
            self.validate_evidence_assets()
            enriched = await self.operation(client, "enrich_multimodal", "enrich_multimodal")
            self.validate_multimodal(enriched)
            await self.embed_and_freshness(client)
            await self.incremental_probes(client)
            await self.agent_flow(client)
        if self.args.cleanup_success and not self.external_blocked:
            self.cleanup()
        self.report.data["finished_at"] = time.time()
        self.report.save()
        return 2 if self.external_blocked else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf-primary", type=Path)
    parser.add_argument("--pdf-table", type=Path)
    parser.add_argument("--resume", metavar="PAPER_ID")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--start-local", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--cleanup-success", action="store_true", help="Delete only this smoke run's Paper and derived storage after a complete success.")
    parser.add_argument("--require-modalities", default="figure,formula")
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    require(bool(args.pdf_primary) ^ bool(args.resume), "provide exactly one of --pdf-primary or --resume")
    return args


def main() -> None:
    load_dotenv(ROOT / ".env")
    args = parse_args()
    runner = SmokeRunner(args)
    try:
        code = asyncio.run(runner.run())
    except ExternalProviderBlocked as exc:
        runner.report.data.setdefault("external_blocks", []).append({"unhandled": str(exc)})
        runner.report.save()
        code = 1 if args.strict else 2
    except Exception as exc:
        runner.report.data["failure"] = {"type": type(exc).__name__, "message": str(exc), "at": time.time()}
        runner.report.save()
        print(json.dumps(runner.report.data, ensure_ascii=False, indent=2), file=sys.stderr)
        code = 1
    print(json.dumps(runner.report.data, ensure_ascii=False, indent=2))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
