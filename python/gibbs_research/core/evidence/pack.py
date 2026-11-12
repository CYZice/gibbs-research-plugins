"""Standardized evidence payloads shared by every report adapter.

These are plain, JSON-serializable dataclasses (no SQLAlchemy), so a pack can be
logged, snapshotted into the report, and fed verbatim to the narrative LLM.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


@dataclass
class Metric:
    """A single quantitative result, with its unit and provenance source."""

    name: str                       # machine key, e.g. "ionic_conductivity"
    label: str                      # human label, e.g. "离子电导率"
    value: Any                      # float | str | None
    unit: Optional[str] = None      # e.g. "S/cm", "eV", "Å"
    source: Optional[str] = None    # e.g. "MSD/Nernst-Einstein", "CP2K summary_card"
    quality: Optional[Dict[str, Any]] = None   # e.g. {"r2": 0.98, "window_ps": 200}


@dataclass
class FigureSpec:
    """A figure the renderers (ECharts on-screen / matplotlib in PDF) can draw.

    ``chart`` carries the cross-renderer figure spec (see plan §figure-spec).
    ``figure_id`` is the stable handle the LLM references as ``[[fig:id]]``.
    """

    figure_id: str
    kind: str                       # line|scatter|bar|stacked_area|rdf|arrhenius|nyquist|pie|structure|table
    title: str
    chart: Dict[str, Any] = field(default_factory=dict)
    caption: str = ""


@dataclass
class Provenance:
    """How a result was produced — drives both the Methods section and citations."""

    method: str                     # "经典分子动力学", "DFT 单点", "第一性原理分子动力学(AIMD)", ...
    engine: Optional[str] = None    # "LAMMPS", "CP2K", "PySCF", "Quantum ESPRESSO"
    level: Optional[str] = None     # "OPLS-AA", "PBE-D3(BJ)/DZVP", "wB97X-D/def2-TZVP"
    params: Dict[str, Any] = field(default_factory=dict)   # cutoff, ensemble, k-points...
    citation_keys: List[str] = field(default_factory=list)  # provenance-derived (citations.py)


@dataclass
class EvidencePack:
    """One source job/material's full, structured evidence."""

    kind: str                       # "md"|"qc"|"electrode_aimd"|"sse"|...
    source_id: int
    system_label: str               # "1M LiPF6 in EC:DMC", molecule name, material formula
    provenance: Provenance
    metrics: List[Metric] = field(default_factory=list)
    figures: List[FigureSpec] = field(default_factory=list)
    caveats: List[str] = field(default_factory=list)
    status: str = "COMPLETED"       # job status; packs are emitted even when incomplete
    title: Optional[str] = None     # user-facing task name, if any
    raw: Dict[str, Any] = field(default_factory=dict)  # escape hatch for fallback

    @property
    def ref(self) -> str:
        """Stable reference used by the narrative LLM, e.g. 'md:12'."""
        return f"{self.kind}:{self.source_id}"

    def has_results(self) -> bool:
        return bool(self.metrics) or bool(self.figures)


def pack_to_dict(pack: EvidencePack) -> Dict[str, Any]:
    """Compact dict for serialization / feeding to the LLM (drops the heavy raw)."""
    d = asdict(pack)
    d["ref"] = pack.ref
    d.pop("raw", None)
    return d
