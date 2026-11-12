import base64
import json
import json as json_module
import logging
import mimetypes
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any

import requests

from app.core.config import ModelEndpointConfig, Settings
from app.services.provider_control_service import ProviderControlService, ProviderError
from app.services.trace import get_current_trace

logger = logging.getLogger(__name__)
trace_logger = logging.getLogger("app.trace")


def _start_model_trace(operation: str, provider: str, model: str | None):
    context = get_current_trace()
    if context is None:
        return None
    context.increment("model_calls")
    return {
        "trace_id": context.trace_id,
        "request_id": context.request_id,
        "paper_id": context.paper_id,
        "operation": operation,
        "provider": provider,
        "model": model,
        "started_at": time.perf_counter(),
    }


def _finish_model_trace(span, status: str = "success", error_type: str | None = None) -> None:
    if span is None:
        return
    trace_logger.info(
        json.dumps(
            {
                "event": "model_call",
                "trace_id": span["trace_id"],
                "request_id": span["request_id"],
                "paper_id": span["paper_id"],
                "metadata": {
                    "operation": span["operation"],
                    "provider": span["provider"],
                    "model": span["model"],
                    "latency_ms": int((time.perf_counter() - span["started_at"]) * 1000),
                    "status": status,
                    "error_type": error_type,
                },
            },
            ensure_ascii=False,
            default=str,
        )
    )


@dataclass(frozen=True)
class RerankResult:
    index: int
    score: float


class ModelGateway:
    def configured_model(self, role: str, fallback: str) -> str:
        return fallback

    def embed_texts(
        self, texts: list[str], timeout_seconds: int | None = None
    ) -> list[list[float]]:
        raise NotImplementedError

    def resolve_embedding_endpoint(self, fallback: str) -> ModelEndpointConfig:
        return ModelEndpointConfig(role="embedding", model=self.configured_model("embedding", fallback))

    def embed_texts_for_endpoint(
        self, texts: list[str], endpoint: ModelEndpointConfig, timeout_seconds: int | None = None
    ) -> list[list[float]]:
        return self.embed_texts(texts, timeout_seconds)

    def rerank(self, query: str, documents: list[str], top_k: int) -> list[RerankResult]:
        raise NotImplementedError

    def complete_text(self, system: str, user: str) -> str:
        raise NotImplementedError

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        raise NotImplementedError

    def describe_image(self, image_path: str, context: str | None = None) -> str:
        raise NotImplementedError


class ModelGatewayError(Exception):
    pass


class ModelGatewayConfigError(ModelGatewayError):
    pass


class ModelGatewayRequestError(ModelGatewayError):
    def __init__(self, message: str, provider_error: ProviderError | None = None) -> None:
        super().__init__(message)
        if provider_error:
            for name in (
                "role",
                "provider",
                "credential_id",
                "http_status",
                "error_code",
                "retryable",
                "retry_after",
            ):
                setattr(self, name, getattr(provider_error, name))


class DashScopeModelGateway(ModelGateway):
    def __init__(self, settings: Settings, http_client=requests) -> None:
        self.settings = settings
        self.http = http_client
        self.control = ProviderControlService(settings)

    def configured_model(self, role: str, fallback: str) -> str:
        try:
            return self.control.resolve_config(role).model
        except ProviderError as exc:
            raise ModelGatewayRequestError(str(exc), exc) from exc

    def embed_texts(
        self, texts: list[str], timeout_seconds: int | None = None
    ) -> list[list[float]]:
        if not texts:
            return []
        endpoint = self.resolve_embedding_endpoint(self.settings.embedding_model)
        return self.embed_texts_for_endpoint(texts, endpoint, timeout_seconds)

    def resolve_embedding_endpoint(self, fallback: str) -> ModelEndpointConfig:
        return ModelEndpointConfig(role="embedding", model=self.configured_model("embedding", fallback))

    def embed_texts_for_endpoint(
        self, texts: list[str], endpoint: ModelEndpointConfig, timeout_seconds: int | None = None
    ) -> list[list[float]]:
        if not texts:
            return []
        request_endpoint = self._endpoint("embedding")
        span = _start_model_trace("embed_texts", request_endpoint.provider, endpoint.model)
        self._require_config_with_trace(request_endpoint, span)

        try:
            response = self._controlled_post(
                request_endpoint,
                "/embeddings",
                "embedding",
                json={
                    "model": endpoint.model,
                    "input": texts,
                    "dimensions": self.settings.embedding_dimension,
                },
                timeout=(3, timeout_seconds or endpoint.timeout_seconds),
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            _finish_model_trace(span, status="error", error_type=type(exc).__name__)
            raise self._request_error("embedding", exc) from exc

        data = body.get("data")
        if not isinstance(data, list):
            _finish_model_trace(span, status="error", error_type="ModelGatewayRequestError")
            raise ModelGatewayRequestError(
                "DashScope embedding failed while parsing response: missing data list."
            )

        items = sorted(data, key=lambda item: item.get("index", 0))
        embeddings = [item.get("embedding") for item in items]
        if len(embeddings) != len(texts) or not all(isinstance(item, list) for item in embeddings):
            _finish_model_trace(span, status="error", error_type="ModelGatewayRequestError")
            raise ModelGatewayRequestError(
                "DashScope embedding failed while parsing response: response size "
                "does not match input."
            )
        _finish_model_trace(span)
        return embeddings

    def rerank(self, query: str, documents: list[str], top_k: int) -> list[RerankResult]:
        endpoint = self._endpoint("rerank")
        if not endpoint.enabled:
            return []
        span = _start_model_trace("rerank", endpoint.provider, endpoint.model)
        self._require_config_with_trace(endpoint, span)
        if not documents:
            _finish_model_trace(span)
            return []

        try:
            response = self._controlled_post(
                endpoint,
                "/reranks",
                "rerank",
                max_attempts=1,
                json={
                    "model": endpoint.model,
                    "query": query,
                    "documents": documents,
                    "top_n": min(max(0, top_k), len(documents)),
                },
                timeout=(3, min(2, endpoint.timeout_seconds)),
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            _finish_model_trace(span, status="error", error_type=type(exc).__name__)
            raise self._request_error("rerank", exc) from exc

        results = body.get("results")
        if results is None and isinstance(body.get("output"), dict):
            results = body["output"].get("results")
        if not isinstance(results, list):
            _finish_model_trace(span, status="error", error_type="ModelGatewayRequestError")
            raise ModelGatewayRequestError(
                "DashScope rerank failed while parsing response: missing results list."
            )

        parsed = []
        for item in results:
            if not isinstance(item, dict):
                continue
            index = item.get("index")
            score = item.get("relevance_score")
            if isinstance(index, int) and isinstance(score, int | float):
                parsed.append(RerankResult(index=index, score=float(score)))
        result = parsed[: max(0, top_k)]
        _finish_model_trace(span)
        return result

    def complete_text(self, system: str, user: str) -> str:
        endpoint = self._endpoint("llm")
        span = _start_model_trace("complete_text", endpoint.provider, endpoint.model)
        self._require_config_with_trace(endpoint, span)
        try:
            content = self._chat_completion(
                endpoint,
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                stream=self.settings.llm_stream,
                operation="text completion",
            )
        except Exception as exc:
            if isinstance(exc, ModelGatewayRequestError):
                _finish_model_trace(span, status="error", error_type=type(exc).__name__)
                raise
            _finish_model_trace(span, status="error", error_type=type(exc).__name__)
            raise self._request_error("text completion", exc) from exc
        _finish_model_trace(span)
        return content.strip()

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        endpoint = self._endpoint("structured")
        span = _start_model_trace("complete_json", endpoint.provider, endpoint.model)
        self._require_config_with_trace(endpoint, span)

        try:
            content = self._chat_completion(
                endpoint,
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
                stream=self.settings.structured_stream,
                operation="JSON completion",
            )
            parsed = self._parse_json_content(content)
        except ModelGatewayRequestError:
            _finish_model_trace(span, status="error", error_type="ModelGatewayRequestError")
            raise
        except Exception as exc:
            _finish_model_trace(span, status="error", error_type=type(exc).__name__)
            raise self._request_error("JSON completion", exc) from exc

        if isinstance(parsed, list):
            logger.warning("DASHSCOPE_JSON_TOP_LEVEL_LIST_WRAPPED item_count=%s", len(parsed))
            _finish_model_trace(span)
            return {"items": parsed}
        if not isinstance(parsed, dict):
            logger.warning("DASHSCOPE_JSON_INVALID_TOP_LEVEL type=%s", type(parsed).__name__)
            _finish_model_trace(span, status="error", error_type="ModelGatewayRequestError")
            raise ModelGatewayRequestError(
                "DashScope JSON completion failed while parsing response: "
                "completion did not return an object."
            )
        _finish_model_trace(span)
        return parsed

    def _chat_completion(
        self,
        endpoint: ModelEndpointConfig,
        messages: list[dict],
        response_format: dict | None = None,
        stream: bool = False,
        operation: str = "chat completion",
    ) -> str:
        payload = {
            "model": endpoint.model,
            "messages": messages,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        if stream:
            payload["stream"] = True

        response = self._controlled_post(
            endpoint,
            "/chat/completions",
            operation,
            json=payload,
            timeout=(30, endpoint.timeout_seconds),
            stream=stream,
        )
        response.raise_for_status()
        if stream:
            return self._stream_content(response, operation)
        body = response.json()
        return body["choices"][0]["message"]["content"]

    def _require_config(self, endpoint: ModelEndpointConfig) -> None:
        if endpoint.provider.lower() != "dashscope":
            raise ModelGatewayConfigError(
                f"Provider '{endpoint.provider}' is not implemented for role {endpoint.role}."
            )
        prefix = endpoint.role.upper()
        if not endpoint.api_key:
            raise ModelGatewayConfigError(
                f"{prefix}_API_KEY is required for {prefix}_PROVIDER=dashscope."
            )
        if not endpoint.base_url:
            raise ModelGatewayConfigError(
                f"{prefix}_BASE_URL is required for {prefix}_PROVIDER=dashscope."
            )

    def _require_config_with_trace(self, endpoint: ModelEndpointConfig, span) -> None:
        try:
            self._require_config(endpoint)
        except Exception as exc:
            _finish_model_trace(span, status="error", error_type=type(exc).__name__)
            raise

    def _headers(self, endpoint: ModelEndpointConfig) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {endpoint.api_key}",
            "Content-Type": "application/json",
        }

    def _endpoint(self, role: str) -> ModelEndpointConfig:
        try:
            return self.control.resolve_endpoint(role)
        except ProviderError as exc:
            raise ModelGatewayRequestError(str(exc), exc) from exc

    def _post_with_retry(self, url: str, **kwargs):
        """Retry only transport failures and explicitly transient provider responses."""
        attempts = max(1, self.settings.model_retry_max_attempts)
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self.http.post(url, **kwargs)
                status_code = getattr(response, "status_code", 200)
                if status_code != 429 and status_code < 500:
                    return response
                last_error = requests.HTTPError(response=response)
            except requests.RequestException as exc:
                last_error = exc
            if attempt == attempts - 1:
                assert last_error is not None
                raise last_error
            time.sleep(self._retry_delay_seconds(getattr(last_error, "response", None), attempt))
        raise RuntimeError("Unreachable model retry state")

    def _controlled_post(
        self,
        endpoint: ModelEndpointConfig,
        path: str,
        operation: str,
        max_attempts: int | None = None,
        **kwargs,
    ):
        attempts = max(1, max_attempts or self.settings.model_retry_max_attempts)
        excluded: set[str] = set()
        last_error: ProviderError | None = None
        for attempt in range(1, attempts + 1):
            retry_sleep = self._retry_delay_seconds(None, attempt - 1)
            current = endpoint if attempt == 1 else self.control.resolve_endpoint(endpoint.role, excluded)
            started = time.perf_counter()
            try:
                estimated_tokens = (
                    len(json_module.dumps(kwargs.get("json") or {}, ensure_ascii=False)) // 4
                )
                with self.control.lease_endpoint(
                    current.role,
                    estimated_tokens=estimated_tokens,
                    preferred=current,
                    exclude=excluded,
                ) as current:
                    response = self.http.post(
                        f"{current.base_url.rstrip('/')}{path}",
                        headers=self._headers(current),
                        **kwargs,
                    )
                status = getattr(response, "status_code", 200)
                latency = int((time.perf_counter() - started) * 1000)
                if status < 400:
                    self.control.report_success(current)
                    self.control.record_event(current, operation, "success", latency, attempt)
                    return response
                retry_header = (
                    getattr(response, "headers", {}).get("Retry-After") if status == 429 else None
                )
                if retry_header:
                    retry_sleep = self._retry_delay_seconds(response, attempt - 1)
                retry_after = (
                    retry_sleep
                    if retry_header
                    else (60, 180, 600)[min(attempt - 1, 2)]
                    if status == 429
                    else None
                )
                response_text = str(getattr(response, "text", "")).lower()
                quota_exhausted = status == 403 and any(
                    marker in response_text for marker in ("quota", "balance", "insufficient", "credit")
                )
                error = ProviderError(
                    f"Provider returned HTTP {status}.",
                    role=current.role,
                    provider=current.provider,
                    credential_id=current.credential_id,
                    http_status=status,
                    error_code=(
                        "PROVIDER_RATE_LIMITED"
                        if status == 429
                        else "PROVIDER_AUTH_FAILED"
                        if status == 401
                        else "PROVIDER_QUOTA_EXHAUSTED"
                        if quota_exhausted
                        else "PROVIDER_FORBIDDEN"
                        if status == 403
                        else "PROVIDER_UNAVAILABLE"
                        if status >= 500
                        else "PROVIDER_BAD_REQUEST"
                    ),
                    retryable=status == 429 or status >= 500,
                    retry_after=retry_after,
                )
                self.control.record_event(current, operation, "failure", latency, attempt, error)
                if status in {401, 403, 429} or status >= 500:
                    self.control.report_failure(current, error)
                if not error.retryable:
                    return response
                last_error = error
            except ProviderError as exc:
                last_error = exc
                if (
                    current.credential_id
                    and exc.error_code == "PROVIDER_CAPACITY_WAIT"
                ):
                    excluded.add(current.credential_id)
            except requests.RequestException as exc:
                code = (
                    "PROVIDER_TIMEOUT"
                    if isinstance(exc, requests.Timeout)
                    else "PROVIDER_UNAVAILABLE"
                )
                last_error = ProviderError(
                    f"Provider request failed: {exc}",
                    role=current.role,
                    provider=current.provider,
                    credential_id=current.credential_id,
                    error_code=code,
                    retryable=True,
                )
                latency = int((time.perf_counter() - started) * 1000)
                self.control.report_failure(current, last_error)
                self.control.record_event(
                    current, operation, "failure", latency, attempt, last_error
                )
            if (
                current.credential_id
                and last_error
                and last_error.error_code in {"PROVIDER_RATE_LIMITED", "PROVIDER_AUTH_FAILED"}
            ):
                excluded.add(current.credential_id)
            if attempt == attempts:
                raise last_error
            time.sleep(retry_sleep)
        raise RuntimeError("Unreachable controlled provider retry state")

    def _retry_delay_seconds(self, response, retry_index: int) -> float:
        header = (
            getattr(response, "headers", {}).get("Retry-After") if response is not None else None
        )
        if header:
            try:
                return max(0.0, float(header))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(header)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=UTC)
                    return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
                except (TypeError, ValueError):
                    pass
        # ponytail: fixed backoff keeps provider recovery predictable without another dependency.
        return (1, 3, 10)[min(retry_index, 2)] + random.uniform(0, 0.5)

    def _request_error(self, operation: str, exc: Exception) -> ModelGatewayRequestError:
        if isinstance(exc, ProviderError):
            return ModelGatewayRequestError(f"DashScope {operation} failed: {exc}", exc)
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        if status_code:
            try:
                reason = HTTPStatus(status_code).phrase
            except ValueError:
                reason = "HTTP error"
            return ModelGatewayRequestError(
                f"DashScope {operation} failed with HTTP {status_code} {reason}."
            )
        return ModelGatewayRequestError(f"DashScope {operation} failed: {exc}")

    def _stream_content(self, response, operation: str = "JSON completion") -> str:
        parts = []
        for raw_line in response.iter_lines(decode_unicode=True):
            if not raw_line:
                continue
            line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
            if line.startswith("data:"):
                line = line.removeprefix("data:").strip()
            if line == "[DONE]":
                break
            chunk = json.loads(line)
            choice = chunk.get("choices", [{}])[0]
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if content:
                parts.append(content)
            message = choice.get("message") or {}
            message_content = message.get("content")
            if message_content:
                parts.append(message_content)
        if not parts:
            raise ModelGatewayRequestError(
                f"DashScope {operation} failed while parsing stream: no content."
            )
        return "".join(parts)

    def _parse_json_content(self, content) -> Any:
        if isinstance(content, dict):
            return content
        if not isinstance(content, str):
            raise TypeError("message content is not JSON text")
        content = content.strip()
        if content.startswith("```"):
            content = content.strip("`").removeprefix("json").strip()
        return json.loads(content)

    def describe_image(self, image_path: str, context: str | None = None) -> str:
        endpoint = self._endpoint("vision")
        span = _start_model_trace("describe_image", endpoint.provider, endpoint.model)
        self._require_config_with_trace(endpoint, span)
        try:
            path = Path(image_path)
            media_type = mimetypes.guess_type(path.name)[0] or "image/png"
            data_url = (
                f"data:{media_type};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
            )
            response = self._controlled_post(
                endpoint,
                "/chat/completions",
                "vision description",
                json={
                    "model": endpoint.model,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "Describe this academic figure as JSON with fields "
                                        "figure_type, summary_zh, summary_en, "
                                        "key_observations, axis_info, limitations, keywords, "
                                        "and embedding_text. "
                                        f"Nearby context: {context or ''}"
                                    ),
                                },
                                {"type": "image_url", "image_url": {"url": data_url}},
                            ],
                        }
                    ],
                },
                timeout=(30, endpoint.timeout_seconds),
            )
            response.raise_for_status()
            body = response.json()
            result = body["choices"][0]["message"]["content"].strip()
        except Exception as exc:
            _finish_model_trace(span, status="error", error_type=type(exc).__name__)
            raise self._request_error("vision description", exc) from exc
        _finish_model_trace(span)
        return result


def runtime_model_name(gateway: ModelGateway, role: str, fallback: str) -> str:
    getter = getattr(gateway, "configured_model", None)
    return getter(role, fallback) if callable(getter) else fallback


def get_model_gateway(settings: Settings, http_client=requests) -> ModelGateway:
    settings.validate_runtime_config()
    return DashScopeModelGateway(settings, http_client=http_client)
