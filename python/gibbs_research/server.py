"""Loopback research tools API and artifact preview UI."""

from __future__ import annotations

import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from . import reports
from .literature import LiteratureClient
from .plots import render
from .store import ConflictError, Store


class PlotSave(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec: dict
    caption: str = ""
    provenance: dict = Field(default_factory=dict)
    artifact_id: str | None = None
    base_revision: int | None = Field(default=None, ge=1)
    feedback: str = ""
    feedback_id: str | None = None


class ReportSave(BaseModel):
    model_config = ConfigDict(extra="forbid")
    material: dict
    draft: dict | None = None
    artifact_id: str | None = None
    base_revision: int | None = Field(default=None, ge=1)
    feedback: str = ""
    feedback_id: str | None = None


class Feedback(BaseModel):
    text: str = Field(min_length=1, max_length=10000)
    revision: int = Field(ge=1)


def create_app(root=None, literature=None):
    store = Store(Path(root or os.environ.get("GIBBS_WORKSPACE", ".runtime")))
    kb = literature or LiteratureClient(os.environ.get("GIBBS_LITERATURE_URL", "http://127.0.0.1:8000"), os.environ.get("GIBBS_LITERATURE_TOKEN", ""))
    token = os.environ.get("GIBBS_TOOLS_TOKEN", "")

    @asynccontextmanager
    async def lifespan(app):
        yield
        await kb.close()

    app = FastAPI(title="Gibbs Research Tools", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    app.state.literature = kb

    @app.middleware("http")
    async def access(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "Cross-origin changes are not allowed"}, status_code=403)
        if token and request.url.path.startswith("/api/"):
            supplied = request.headers.get("authorization", "").removeprefix("Bearer ") or request.cookies.get("gibbs_token", "")
            if not hmac.compare_digest(supplied.encode(), token.encode()):
                return JSONResponse({"detail": "Authentication required"}, status_code=401)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        if request.url.path.startswith("/api/reports/"):
            response.headers["Content-Security-Policy"] = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; frame-ancestors 'self'"
        return response

    @app.exception_handler(ConflictError)
    async def conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "Artifact or required field not found"}, status_code=404)

    @app.exception_handler(httpx.HTTPError)
    async def upstream(request, exc):
        return JSONResponse({"detail": "Cannot reach configured literature or writing service"}, status_code=502)

    @app.get("/health")
    def health():
        return {"status": "ok", "version": "0.1.0"}

    @app.post("/auth/login")
    async def login(request: Request):
        supplied = (await request.json()).get("token", "")
        if not isinstance(supplied, str) or (token and not hmac.compare_digest(token.encode(), supplied.encode())):
            raise HTTPException(401, "Invalid access token")
        result = JSONResponse({"authenticated": True})
        result.set_cookie("gibbs_token", supplied, httponly=True, samesite="strict", secure=request.url.scheme == "https")
        return result

    @app.get("/api/artifacts")
    def artifacts():
        return [{k: a[k] for k in ("artifact_id", "revision", "kind", "created_at")} | {"title": a["spec"].get("title", "研究图表") if a["kind"] == "plot" else a["prepared"]["title"]} for a in store.list()]

    @app.get("/api/artifacts/{artifact_id}")
    def artifact(artifact_id: str, revision: int | None = None):
        return store.get(artifact_id, revision)

    @app.get("/api/artifacts/{artifact_id}/history")
    def history(artifact_id: str):
        return [{k: a[k] for k in ("revision", "created_at", "feedback")} for a in store.history(artifact_id)]

    @app.post("/api/artifacts/{artifact_id}/feedback")
    def feedback(artifact_id: str, body: Feedback):
        return store.feedback(artifact_id, body.revision, body.text)

    @app.get("/api/feedback")
    def pending():
        return store.pending()

    def check_feedback(body):
        if body.feedback_id and not any(f["id"] == body.feedback_id and f["artifact_id"] == body.artifact_id for f in store.pending()):
            raise ValueError("Feedback is not pending for this artifact")

    def resolved(body, saved):
        if body.feedback_id:
            store.resolve_feedback(body.feedback_id, saved["artifact_id"])
        return saved | {"preview_path": f"/?artifact={saved['artifact_id']}&revision={saved['revision']}"}

    @app.post("/api/plots")
    async def plot_save(body: PlotSave):
        check_feedback(body)
        await asyncio.to_thread(render, body.spec)
        payload = {k: body.model_dump()[k] for k in ("spec", "caption", "provenance", "feedback")}
        return resolved(body, store.save("plot", payload, body.artifact_id, body.base_revision))

    @app.get("/api/plots/{artifact_id}/{revision}/{fmt}")
    async def plot_export(artifact_id: str, revision: int, fmt: str):
        item = store.get(artifact_id, revision)
        if item["kind"] != "plot":
            raise ValueError("Not a plot")
        content = await asyncio.to_thread(render, item["spec"], fmt)
        return Response(content, media_type={"png": "image/png", "svg": "image/svg+xml", "pdf": "application/pdf"}[fmt])

    @app.post("/api/reports/prepare")
    async def report_prepare(material: dict):
        prepared, _, _ = await reports.prepare(material, kb, store)
        return prepared

    @app.post("/api/reports")
    async def report_save(body: ReportSave):
        check_feedback(body)
        material = body.material | {"feedback": body.feedback}
        prepared, packs, ref_map = await reports.prepare(material, kb, store)
        draft = body.draft if body.draft is not None else await reports.model_write(prepared)
        clean, warnings, ref_map = reports.validate_draft(draft, packs, ref_map)
        payload = {"prepared": prepared, "draft": clean, "source_draft": draft, "reference_map": ref_map, "warnings": warnings, "feedback": body.feedback}
        await asyncio.to_thread(reports.render_html, payload)
        return resolved(body, store.save("report", payload, body.artifact_id, body.base_revision))

    @app.get("/api/reports/{artifact_id}/{revision}/{fmt}")
    async def report_export(artifact_id: str, revision: int, fmt: str):
        payload = store.get(artifact_id, revision)
        if payload["kind"] != "report":
            raise ValueError("Not a report")
        if fmt == "html":
            return HTMLResponse(await asyncio.to_thread(reports.render_html, payload))
        if fmt == "md":
            return Response(reports.render_markdown(payload), media_type="text/markdown", headers={"Content-Disposition": f'attachment; filename="{artifact_id}-r{revision}.md"'})
        if fmt == "json":
            return JSONResponse(payload)
        if fmt == "pdf":
            from playwright.sync_api import Error as BrowserError
            try:
                return Response(await reports.render_pdf(payload), media_type="application/pdf")
            except BrowserError as exc:
                raise HTTPException(503, "PDF browser unavailable. Run python -m playwright install chromium, or configure GIBBS_BROWSER_CHANNEL for an installed browser") from exc
        raise ValueError("Format must be html, md, json or pdf")

    # Explicit endpoint map: the Agent cannot proxy arbitrary admin URLs.
    actions = {"search": ("POST", "evidence/search"), "papers": ("POST", "papers/search"), "discover": ("POST", "literature/search"), "import": ("POST", "literature/imports"), "ground": ("POST", "citations/ground"), "verify": ("POST", "evidence/packs"), "bibliography": ("POST", "bibliography/format")}

    @app.post("/api/literature/{action}")
    async def literature_action(action: str, payload: dict):
        if action not in actions:
            raise ValueError("Unsupported literature action")
        method, path = actions[action]
        return await kb.request(method, path, json=payload)

    @app.get("/api/literature/evidence/{evidence_id}")
    async def literature_read(evidence_id: str):
        return await kb.evidence(evidence_id)

    @app.get("/api/literature/status/{kind}/{item_id}")
    async def literature_status(kind: str, item_id: str):
        from urllib.parse import quote
        if kind not in {"tasks", "workflows", "import-batches"}:
            raise ValueError("Status kind must be tasks, workflows or import-batches")
        return await kb.request("GET", f"{kind}/{quote(item_id, safe='')}")

    @app.post("/api/literature-resource")
    async def literature_resource(payload: dict):
        from urllib.parse import quote
        kind = payload.get("kind")
        allowed = {"paper": "", "content": "/content", "evidence": "/evidence", "assets": "/assets", "relations": "/relations", "citation-contexts": "/citation-contexts"}
        if kind not in allowed or not isinstance(payload.get("paper_id"), str):
            raise ValueError("Specify paper_id and a supported paper resource kind")
        params = payload.get("params", {})
        if not isinstance(params, dict) or set(params) - {"offset", "limit", "page", "page_size", "relation"}:
            raise ValueError("Unsupported paper resource parameters")
        return await kb.request("GET", f"papers/{quote(payload['paper_id'], safe='')}{allowed[kind]}", params=params)

    @app.post("/api/literature-upload")
    async def literature_upload(request: Request):
        # Plugin sends the PDF body, never an arbitrary server-side file path.
        limit = 50 * 1024 * 1024
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > limit:
                raise HTTPException(413, "PDF exceeds 50MB")
        if not data.startswith(b"%PDF-"):
            raise ValueError("Expected a PDF file")
        return await kb.upload("upload.pdf", bytes(data))

    static = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/")
    def index():
        return FileResponse(static / "index.html")

    return app
