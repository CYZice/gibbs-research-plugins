import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TraceContext:
    operation: str
    request_id: str | None = None
    paper_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    trace_id: str = field(default_factory=lambda: f"trace_{uuid.uuid4().hex}")
    started_at: float = field(default_factory=time.perf_counter)
    counters: dict[str, int] = field(default_factory=dict)

    def increment(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount

    @property
    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self.started_at) * 1000)
