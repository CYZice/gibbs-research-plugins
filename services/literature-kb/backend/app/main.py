import hashlib
import hmac
import logging
import uuid
from time import perf_counter, time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.routes import admin, discovery, harvest, health, v1
from app.core.config import get_settings
from app.schemas.v1 import ErrorResponse
from app.services.operations_service import record_api_request

logger = logging.getLogger(__name__)
SESSION_COOKIE = "literature_kb_session"
SESSION_TTL_SECONDS = 8 * 60 * 60


class LoginRequest(BaseModel):
    token: str


def _make_session(token: str) -> str:
    expires_at = str(int(time()) + SESSION_TTL_SECONDS)
    signature = hmac.new(token.encode(), expires_at.encode(), hashlib.sha256).hexdigest()
    return f"{expires_at}.{signature}"


def _has_valid_session(session: str | None, token: str | None) -> bool:
    if not session or not token:
        return False
    try:
        expires_at, signature = session.split(".", 1)
        expected = hmac.new(token.encode(), expires_at.encode(), hashlib.sha256).hexdigest()
        return int(expires_at) >= time() and hmac.compare_digest(signature, expected)
    except (TypeError, ValueError):
        return False


class ApiAuthMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        token: str | None,
        admin_token: str | None,
        molyte_token: str | None,
    ) -> None:
        super().__init__(app)
        self.token = token
        self.admin_token = admin_token
        self.molyte_token = molyte_token

    async def dispatch(self, request, call_next):
        request.state.literature_scope = None
        request.state.molyte_service = False
        if request.url.path.startswith("/api/v1/admin/"):
            supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
            if not self.admin_token or not supplied or not hmac.compare_digest(supplied, self.admin_token):
                request_id = request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:12]}"
                return JSONResponse({"error": {"code": "ADMIN_UNAUTHORIZED", "message": "Missing or invalid admin token.", "details": {}, "request_id": request_id}}, status_code=401, headers={"WWW-Authenticate": "Bearer", "X-Request-ID": request_id})
            return await call_next(request)
        if (
            (not self.token and not self.molyte_token)
            or request.url.path == "/health"
            or request.url.path == "/openapi.json"
            or request.url.path.startswith("/docs")
            or request.url.path == "/redoc"
            or request.url.path.startswith("/auth/")
        ):
            return await call_next(request)

        authorization = request.headers.get("authorization", "")
        prefix = "Bearer "
        supplied = authorization[len(prefix) :] if authorization.startswith(prefix) else ""
        if (
            self.molyte_token
            and supplied
            and hmac.compare_digest(supplied, self.molyte_token)
        ):
            scope = request.headers.get("x-literature-scope", "").strip()
            if not scope:
                request_id = request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:12]}"
                return JSONResponse(
                    {
                        "error": {
                            "code": "LITERATURE_SCOPE_REQUIRED",
                            "message": "The Molyte service request is missing its access scope.",
                            "details": {},
                            "request_id": request_id,
                        }
                    },
                    status_code=403,
                    headers={"X-Request-ID": request_id},
                )
            request.state.literature_scope = scope
            request.state.molyte_service = True
            return await call_next(request)
        if (
            (not supplied or not hmac.compare_digest(supplied, self.token))
            and not _has_valid_session(request.cookies.get(SESSION_COOKIE), self.token)
        ):
            request_id = request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:12]}"
            return JSONResponse(
                {
                    "error": {
                        "code": "UNAUTHORIZED",
                        "message": "Missing or invalid API token.",
                        "details": {},
                        "request_id": request_id,
                    }
                },
                status_code=401,
                headers={"WWW-Authenticate": "Bearer", "X-Request-ID": request_id},
            )
        return await call_next(request)


class ApiMetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        started = perf_counter()
        response = await call_next(request)
        record_api_request(request.url.path, response.status_code, round((perf_counter() - started) * 1000))
        return response


def create_app() -> FastAPI:
    settings = get_settings()
    settings.validate_runtime_config()
    if settings.admin_api_token:
        logger.info(
            "Admin token loaded from local settings (fingerprint=%s).",
            hashlib.sha256(settings.admin_api_token.encode()).hexdigest()[:10],
        )
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "A versioned local literature evidence API. Read `/openapi.json` for the "
            "machine-readable contract; business endpoints require Bearer authentication."
        ),
        openapi_tags=[
            {
                "name": "Catalog",
                "description": "Paper discovery and metadata capability discovery.",
            },
            {
                "name": "Evidence Retrieval",
                "description": "Evidence retrieval with explicit scope and filters.",
            },
            {
                "name": "Read & Verify",
                "description": "Read known content and resolve evidence back to source.",
            },
            {
                "name": "Assets & Relations",
                "description": "Assets, citations, and paper graph context.",
            },
            {
                "name": "Workflow",
                "description": "Paper processing tasks, evidence packs, and grounding.",
            },
            {"name": "Bibliography", "description": "Reference formatting."},
            {
                "name": "External Discovery",
                "description": "Discover open literature and stage licensed PDFs locally.",
            },
            {
                "name": "Corpus Harvest",
                "description": "Enumerate OpenAlex topics and stage large PDF corpora.",
            },
            {"name": "System", "description": "Service health and dependency readiness."},
        ],
    )
    app.add_middleware(
        ApiAuthMiddleware,
        token=settings.api_auth_token,
        admin_token=settings.admin_api_token,
        molyte_token=settings.molyte_api_token,
    )
    app.add_middleware(ApiMetricsMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:5174", "http://localhost:5174"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.post("/auth/login")
    async def login(payload: LoginRequest):
        if not settings.api_auth_token or not hmac.compare_digest(payload.token, settings.api_auth_token):
            return JSONResponse({"error": "Invalid login token."}, status_code=401)
        response = JSONResponse({"authenticated": True})
        response.set_cookie(
            key=SESSION_COOKIE,
            value=_make_session(settings.api_auth_token),
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            secure=settings.app_env == "production",
            samesite="lax",
            path="/",
        )
        return response

    @app.get("/auth/session")
    async def session(request: Request):
        return {"authenticated": _has_valid_session(request.cookies.get(SESSION_COOKIE), settings.api_auth_token)}

    @app.post("/auth/logout")
    async def logout():
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request, exc):
        request_id = request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:12]}"
        return JSONResponse(
            status_code=400,
            headers={"X-Request-ID": request_id},
            content={
                "error": {
                    "code": "INVALID_ARGUMENT",
                    "message": "Request validation failed.",
                    "details": {"errors": exc.errors()},
                    "request_id": request_id,
                }
            },
        )

    operation_metadata = {
        ("get", "/health"): ("System", "Get liveness", "Unauthenticated process liveness check."),
        ("get", "/api/v1/system/health"): (
            "System",
            "Get dependency readiness",
            "Check configured dependencies without exposing secrets.",
        ),
        ("get", "/api/v1/papers/meta-catalog"): (
            "Catalog",
            "Discover catalog capabilities",
            (
                "Discover filter, operator, sort, and sample-value capabilities "
                "before constructing searches."
            ),
        ),
        ("post", "/api/v1/papers/search"): (
            "Catalog",
            "Search papers",
            "Search local papers with metadata, full-text lexical, and optional semantic retrieval.",
        ),
        ("post", "/api/v1/literature/search"): (
            "Catalog",
            "Search local and external literature",
            "Federate local paper retrieval with bounded OpenAlex discovery and explicit degradation status.",
        ),
        ("post", "/api/v1/literature/imports"): (
            "Workflow",
            "Import external literature",
            "Queue confirmed OpenAlex works for staging, canonical registration, and complete processing.",
        ),
        ("get", "/api/v1/discovery/sources"): (
            "External Discovery",
            "List discovery sources",
            "List external scholarly sources and their local configuration status.",
        ),
        ("post", "/api/v1/discovery/search"): (
            "External Discovery",
            "Search external papers",
            "Search OpenAlex without creating local paper records or downloading content.",
        ),
        ("post", "/api/v1/discovery/works/resolve"): (
            "External Discovery",
            "Resolve external paper",
            "Resolve one OpenAlex work and enrich its open-access PDF candidates with Unpaywall.",
        ),
        ("post", "/api/v1/discovery/downloads"): (
            "External Discovery",
            "Stage external PDF",
            "Resolve an external work server-side and store one policy-eligible PDF in local staging.",
        ),
        ("get", "/api/v1/import-batches/{batch_id}"): (
            "Workflow", "Get import batch", "Read per-item progress and failures for a local or remote import batch."
        ),
        ("post", "/api/v1/import-batches/{batch_id}/retry-failed"): (
            "Workflow", "Retry failed import items", "Queue failed local or remote import items without repeating completed stages."
        ),
        ("post", "/api/v1/papers"): (
            "Workflow",
            "Upload paper",
            "Upload a PDF and create its initial paper/task records.",
        ),
        ("get", "/api/v1/papers/{paper_id}"): (
            "Catalog",
            "Get paper",
            "Read one paper's summary, processing status, and capabilities.",
        ),
        ("get", "/api/v1/papers/{paper_id}/content"): (
            "Read & Verify",
            "Read paper content",
            "Read an offset window for a known paper; this endpoint does not search.",
        ),
        ("get", "/api/v1/papers/{paper_id}/evidence"): (
            "Evidence Retrieval",
            "Browse paper evidence",
            "Page ready evidence already belonging to one known paper.",
        ),
        ("get", "/api/v1/papers/{paper_id}/assets"): (
            "Assets & Relations",
            "Browse paper assets",
            "List assets attached to a known paper.",
        ),
        ("post", "/api/v1/evidence/search"): (
            "Evidence Retrieval",
            "Search evidence",
            (
                "Retrieve evidence, then call its content or source endpoint to verify "
                "a returned evidence_id."
            ),
        ),
        ("get", "/api/v1/evidence/{evidence_id}"): (
            "Read & Verify",
            "Get evidence",
            "Read a known evidence record and its compact citation-ready representation.",
        ),
        ("get", "/api/v1/evidence/{evidence_id}/content"): (
            "Read & Verify",
            "Read evidence context",
            "Read a context window around a known evidence record; this endpoint does not search.",
        ),
        ("get", "/api/v1/evidence/{evidence_id}/source"): (
            "Read & Verify",
            "Resolve evidence source",
            "Resolve a known evidence record to citation data and its PDF asset when available.",
        ),
        ("get", "/api/v1/assets/{asset_id}"): (
            "Assets & Relations",
            "Get asset",
            "Read metadata for one known asset.",
        ),
        ("get", "/api/v1/assets/{asset_id}/download"): (
            "Assets & Relations",
            "Download asset",
            "Download a known stored asset as binary content.",
        ),
        ("get", "/api/v1/papers/{paper_id}/relations"): (
            "Assets & Relations",
            "List paper relations",
            "List references or citations for a known paper.",
        ),
        ("get", "/api/v1/papers/{paper_id}/citation-contexts"): (
            "Assets & Relations",
            "List citation contexts",
            "List in-text citation contexts for a known paper.",
        ),
        ("post", "/api/v1/bibliography/format"): (
            "Bibliography",
            "Format bibliography",
            "Format explicit paper or reference IDs in a supported style.",
        ),
        ("get", "/api/v1/tasks/{task_id}"): (
            "Workflow",
            "Get task",
            "Read the latest state of a known task.",
        ),
        ("post", "/api/v1/tasks/{task_id}/refresh"): (
            "Workflow",
            "Refresh task",
            "Return the current state of a known task.",
        ),
        ("post", "/api/v1/evidence/packs"): (
            "Workflow",
            "Build evidence pack",
            "Queue a claim-to-evidence workflow; poll the returned run_id for verified evidence.",
        ),
        ("post", "/api/v1/citations/ground"): (
            "Workflow",
            "Ground claims",
            "Queue a draft-to-citations workflow; poll the returned run_id for the grounded draft.",
        ),
        ("get", "/api/v1/workflows/{run_id}"): (
            "Workflow",
            "Get workflow run",
            "Read the status and completed result of an evidence or citation workflow.",
        ),
    }
    error_statuses = {
        ("get", "/api/v1/papers/meta-catalog"): [400],
        ("post", "/api/v1/papers/search"): [400],
        ("post", "/api/v1/discovery/search"): [400, 502, 503],
        ("post", "/api/v1/discovery/works/resolve"): [400, 404, 502, 503],
        ("post", "/api/v1/discovery/downloads"): [400, 404, 409, 413, 502, 503],
        ("get", "/api/v1/import-batches/{batch_id}"): [404],
        ("post", "/api/v1/import-batches/{batch_id}/retry-failed"): [404],
        ("post", "/api/v1/papers"): [400],
        ("get", "/api/v1/papers/{paper_id}"): [404],
        ("get", "/api/v1/papers/{paper_id}/content"): [400, 404, 409],
        ("get", "/api/v1/papers/{paper_id}/evidence"): [400, 404],
        ("get", "/api/v1/papers/{paper_id}/assets"): [400, 404],
        ("post", "/api/v1/evidence/search"): [400, 503],
        ("get", "/api/v1/evidence/{evidence_id}"): [404, 409],
        ("get", "/api/v1/evidence/{evidence_id}/content"): [400, 404, 409],
        ("get", "/api/v1/evidence/{evidence_id}/source"): [404],
        ("get", "/api/v1/assets/{asset_id}"): [404],
        ("get", "/api/v1/assets/{asset_id}/download"): [404],
        ("get", "/api/v1/papers/{paper_id}/relations"): [400, 404],
        ("get", "/api/v1/papers/{paper_id}/citation-contexts"): [400, 404],
        ("post", "/api/v1/bibliography/format"): [400, 404],
        ("get", "/api/v1/tasks/{task_id}"): [404],
        ("post", "/api/v1/tasks/{task_id}/refresh"): [404],
        ("post", "/api/v1/evidence/packs"): [400, 503],
        ("post", "/api/v1/citations/ground"): [400, 503],
        ("get", "/api/v1/workflows/{run_id}"): [404],
    }

    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
            tags=app.openapi_tags,
        )
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        error_schema = ErrorResponse.model_json_schema(ref_template="#/components/schemas/{model}")
        components.update(error_schema.pop("$defs", {}))
        components["ErrorResponse"] = error_schema
        schema["components"].setdefault("securitySchemes", {})["bearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "API token",
        }
        schema["components"]["securitySchemes"]["adminBearerAuth"] = {
            "type": "http", "scheme": "bearer", "bearerFormat": "Admin API token",
        }
        for path, methods in schema["paths"].items():
            for method, operation in methods.items():
                if method not in {"get", "post", "put", "patch", "delete"}:
                    continue
                key = (method, path)
                operation.get("responses", {}).pop("422", None)
                if key in operation_metadata:
                    tag, summary, description = operation_metadata[key]
                    operation["tags"] = [tag]
                    operation["summary"] = summary
                    operation["description"] = description
                    operation_slug = path.removeprefix("/api/v1/")
                    operation_slug = (
                        operation_slug.replace("/", "_").replace("{", "").replace("}", "")
                    )
                    operation["operationId"] = f"{method}_{operation_slug}"
                if path.startswith("/api/v1/"):
                    operation.setdefault("description", operation.get("summary", "Versioned API operation."))
                    operation["security"] = [{"adminBearerAuth": []}] if path.startswith("/api/v1/admin/") else [{"bearerAuth": []}]
                    responses = operation.setdefault("responses", {})
                    if path.endswith("/download"):
                        responses.get("200", {}).get("content", {}).pop("application/json", None)
                    responses.setdefault(
                        "401",
                        {
                            "description": "Missing or invalid API token.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ErrorResponse"}
                                }
                            },
                        },
                    )
                    for status_code in error_statuses.get(key, []):
                        responses.setdefault(
                            str(status_code),
                            {
                                "description": (
                                    "API error. Correct 400 requests; refresh 401 credentials; "
                                    "stop using 404 IDs; wait or run prerequisites for 409; "
                                    "retry 503 with backoff."
                                ),
                                "content": {
                                    "application/json": {
                                        "schema": {"$ref": "#/components/schemas/ErrorResponse"}
                                    }
                                },
                            },
                        )
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi

    app.include_router(health.router)
    app.include_router(v1.router)
    app.include_router(discovery.router)
    app.include_router(harvest.router)
    app.include_router(admin.router)

    return app


app = create_app()
