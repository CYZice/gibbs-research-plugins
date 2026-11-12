import json
import logging
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from typing import Any

from app.services.trace.context import TraceContext

logger = logging.getLogger("app.trace")
_current_trace: ContextVar[TraceContext | None] = ContextVar(
    "current_trace", default=None
)


@dataclass
class TraceSpan:
    span_id: str
    operation: str
    started_at: float = field(default_factory=time.perf_counter)
    metadata: dict[str, Any] = field(default_factory=dict)


def get_current_trace() -> TraceContext | None:
    return _current_trace.get()


class TraceCollector:
    def __init__(self, context: TraceContext) -> None:
        self.context = context
        self._token = None

    def __enter__(self):
        self._token = _current_trace.set(self.context)
        self.record("trace_start", self.context.metadata)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        status = "error" if exc_type else "success"
        self.record(
            "trace_finish",
            {
                "status": status,
                "elapsed_ms": self.context.elapsed_ms,
                "error_type": exc_type.__name__ if exc_type else None,
            },
        )
        if self._token is not None:
            _current_trace.reset(self._token)

    def start_span(self, operation: str, **metadata) -> TraceSpan:
        span = TraceSpan(
            span_id=f"span_{uuid.uuid4().hex}",
            operation=operation,
            metadata={key: value for key, value in metadata.items() if value is not None},
        )
        self.record("span_start", {"span_id": span.span_id, "operation": operation, **span.metadata})
        return span

    def finish_span(self, span: TraceSpan, status: str = "success", **metadata) -> None:
        payload = {
            "span_id": span.span_id,
            "operation": span.operation,
            "status": status,
            "latency_ms": int((time.perf_counter() - span.started_at) * 1000),
            **span.metadata,
            **{key: value for key, value in metadata.items() if value is not None},
        }
        self.record("span_finish", payload)

    def record(self, event: str, metadata: dict[str, Any] | None = None) -> None:
        payload = {
            "event": event,
            "trace_id": self.context.trace_id,
            "request_id": self.context.request_id,
            "operation": self.context.operation,
            "paper_id": self.context.paper_id,
            "metadata": metadata or {},
        }
        logger.info(json.dumps(payload, ensure_ascii=False, default=str))

    def increment(self, name: str, amount: int = 1) -> None:
        self.context.increment(name, amount)


@contextmanager
def trace_operation(
    operation: str,
    request_id: str | None = None,
    paper_id: str | None = None,
    **metadata,
):
    context = TraceContext(
        request_id=request_id,
        paper_id=paper_id,
        operation=operation,
        metadata=metadata,
    )
    with TraceCollector(context) as trace:
        span = trace.start_span(operation, **metadata)
        try:
            yield trace
        except Exception as exc:
            trace.finish_span(span, status="error", error_type=type(exc).__name__)
            raise
        else:
            trace.finish_span(span)


def traced_operation(operation: str):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            paper = kwargs.get("paper")
            if paper is None and len(args) > 1:
                paper = args[1]
            paper_id = getattr(paper, "paper_id", None)
            with trace_operation(operation, paper_id=paper_id) as trace:
                result = func(*args, **kwargs)
                summary = (
                    result.model_dump(exclude_none=True)
                    if hasattr(result, "model_dump")
                    else {"result_type": type(result).__name__}
                )
                trace.record("task_summary", summary)
                return result

        return wrapper

    return decorator
