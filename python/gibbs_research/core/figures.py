"""Server-side figure rendering for the typeset PDF.

Mirrors the on-screen Nature/ECharts style (Okabe–Ito palette, IUPAC axis
labels, thick spines, inward ticks, dashed grid, no smoothing) using matplotlib
(already installed on the backend — see ``app/workers/rdf_plotter.py``). One
function per chart ``type``; each consumes the SAME cross-renderer figure spec
the frontend uses and returns PNG bytes.

Uses the object-oriented API (``Figure`` + ``FigureCanvasAgg``) — threadsafe and
free of global pyplot state, so it is safe to call from a threadpool while many
figures render concurrently.
"""

from __future__ import annotations

import base64
import io
import os
from typing import Any, Dict, List, Optional, Tuple

import matplotlib

matplotlib.use("Agg")
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.backends.backend_agg import FigureCanvasAgg  # noqa: E402
from matplotlib import font_manager  # noqa: E402


# Exact cross-renderer color contract — must match NATURE_COLORS in
# frontend/src/components/sse/natureChart.tsx.
OKABE_ITO = [
    "#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00",
    "#56B4E9", "#762A83", "#1B7837", "#B2182B",
]

_DPI = 200
_FIGSIZE = (6.6, 4.1)

# Bundled CJK font (added under assets/fonts/ on the server). If absent we look
# for a common system CJK font, else fall back (Latin-only) — captions live in
# the HTML, so most figure text is Latin/math anyway.
_FONTS_DIR = os.path.join(os.path.dirname(__file__), "assets", "fonts")
_CJK_CANDIDATES = [
    os.path.join(_FONTS_DIR, "NotoSansCJKsc-Regular.otf"),
    os.path.join(_FONTS_DIR, "SourceHanSansSC-Regular.otf"),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
]


def _register_cjk() -> Optional[str]:
    for path in _CJK_CANDIDATES:
        if os.path.exists(path):
            try:
                font_manager.fontManager.addfont(path)
                return font_manager.FontProperties(fname=path).get_name()
            except Exception:
                continue
    return None


_CJK_NAME = _register_cjk()


def _new_axes():
    fig = Figure(figsize=_FIGSIZE, dpi=_DPI)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    family = [_CJK_NAME] if _CJK_NAME else []
    family += ["DejaVu Sans", "Arial"]
    for item in ([ax.title] + ax.get_xticklabels() + ax.get_yticklabels()):
        item.set_fontfamily(family)
    ax._gibbs_family = family  # type: ignore[attr-defined]
    # Nature-ish spines/ticks/grid
    for spine in ax.spines.values():
        spine.set_linewidth(1.6)
        spine.set_color("#333333")
    ax.tick_params(direction="in", length=4, width=1.4, colors="#333333", labelsize=10)
    ax.grid(True, linestyle="--", linewidth=0.7, color="#ececec")
    ax.set_axisbelow(True)
    fig.subplots_adjust(left=0.15, right=0.95, top=0.92, bottom=0.15)
    return fig, ax


def _color(i: int, explicit: Optional[int] = None) -> str:
    idx = explicit if explicit is not None else i
    return OKABE_ITO[idx % len(OKABE_ITO)]


def _axis_label(symbol: Any, unit: Any) -> str:
    sym = (str(symbol).strip() if symbol else "")
    u = (str(unit).strip() if unit else "")
    # IUPAC: italic math for ASCII quantity symbols (t, r, g(r), 1000/T);
    # CJK / word labels render upright in the CJK font (no mathtext).
    if sym:
        sym = f"${sym}$" if (sym.isascii() and len(sym) <= 8) else sym
    if sym and u:
        return f"{sym} / {u}"
    return sym or u


def _set_labels(ax, chart: Dict[str, Any]) -> None:
    fam = getattr(ax, "_gibbs_family", None)
    ax.set_xlabel(_axis_label(chart.get("xLabel"), chart.get("xUnit")), fontsize=12, fontfamily=fam)
    ax.set_ylabel(_axis_label(chart.get("yLabel"), chart.get("yUnit") or chart.get("unit")), fontsize=12, fontfamily=fam)


def _pairs(data: Any) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    for p in (data or []):
        try:
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                out.append((float(p[0]), float(p[1])))
        except Exception:
            continue
    return out


def _finish(fig, ax) -> bytes:
    labels = ax.get_legend_handles_labels()[1]
    if labels:
        ax.legend(frameon=False, fontsize=9, prop={"family": getattr(ax, "_gibbs_family", None)})
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=_DPI, bbox_inches="tight")
    buf.seek(0)
    data = buf.getvalue()
    fig.clf()
    return data


def render_line(chart: Dict[str, Any]) -> bytes:
    fig, ax = _new_axes()
    series = chart.get("series") or []
    is_area = chart.get("type") == "stacked_area"
    if series:
        for i, s in enumerate(series):
            pts = _pairs(s.get("data"))
            if not pts:
                continue
            xs, ys = zip(*pts)
            c = _color(i, s.get("colorIndex"))
            ax.plot(xs, ys, color=c, linewidth=2.0, label=s.get("name") or f"系列{i + 1}")
            if is_area:
                ax.fill_between(xs, ys, alpha=0.45, color=c)
    elif _pairs(chart.get("data")):
        xs, ys = zip(*_pairs(chart.get("data")))
        ax.plot(xs, ys, color=_color(0), linewidth=2.0)
    _set_labels(ax, chart)
    return _finish(fig, ax)


def render_rdf(chart: Dict[str, Any]) -> bytes:
    fig, ax = _new_axes()
    for i, s in enumerate(chart.get("series") or []):
        pts = _pairs(s.get("data"))
        if not pts:
            continue
        xs, ys = zip(*pts)
        ax.plot(xs, ys, color=_color(i, s.get("colorIndex")), linewidth=2.0, label=s.get("name") or "g(r)")
    coord = _pairs(chart.get("coordination"))
    if coord:
        ax2 = ax.twinx()
        cx, cy = zip(*coord)
        ax2.plot(cx, cy, color=_color(2), linewidth=1.5, linestyle="--", label="N(r)")
        ax2.set_ylabel("$N(r)$", fontsize=11)
        ax2.tick_params(direction="in")
    _set_labels(ax, chart)
    return _finish(fig, ax)


def render_bar(chart: Dict[str, Any]) -> bytes:
    fig, ax = _new_axes()
    cats = [str(c) for c in (chart.get("categories") or [])]
    series = chart.get("series") or []
    import numpy as np
    x = np.arange(len(cats))
    n = max(1, len(series))
    width = 0.8 / n
    for i, s in enumerate(series):
        vals = [(_safe_float(v)) for v in (s.get("data") or [])]
        ax.bar(x + (i - (n - 1) / 2) * width, vals, width=width,
               color=_color(i, s.get("colorIndex")), label=s.get("name") or f"系列{i + 1}")
    ax.set_xticks(list(x))
    ax.set_xticklabels(cats, fontsize=9, rotation=20, ha="right",
                       fontfamily=getattr(ax, "_gibbs_family", None))
    ax.grid(True, axis="y", linestyle="--", linewidth=0.7, color="#ececec")
    ax.grid(False, axis="x")
    _set_labels(ax, chart)
    return _finish(fig, ax)


def render_scatter(chart: Dict[str, Any]) -> bytes:
    fig, ax = _new_axes()
    series = chart.get("series") or []
    pts = _pairs(series[0].get("data")) if series else _pairs(chart.get("data"))
    if pts:
        xs, ys = zip(*pts)
        ax.scatter(xs, ys, color=_color(0), s=36, zorder=3)
    fit = chart.get("fit") or {}
    if isinstance(fit.get("slope"), (int, float)) and isinstance(fit.get("intercept"), (int, float)) and pts:
        xs = [p[0] for p in pts]
        x0 = fit.get("x0", min(xs))
        x1 = fit.get("x1", max(xs))
        try:
            ax.plot([x0, x1], [fit["slope"] * x0 + fit["intercept"], fit["slope"] * x1 + fit["intercept"]],
                    color=_color(1), linewidth=1.8, linestyle="--", zorder=2)
        except Exception:
            pass
    _set_labels(ax, chart)
    return _finish(fig, ax)


def render_nyquist(chart: Dict[str, Any]) -> bytes:
    fig, ax = _new_axes()
    series = chart.get("series") or []
    pts = _pairs(series[0].get("data")) if series else _pairs(chart.get("data"))
    if pts:
        xs, ys = zip(*pts)
        ax.plot(xs, ys, color=_color(0), marker="o", markersize=4, linewidth=1.4)
        ax.set_aspect("equal", adjustable="datalim")
    _set_labels(ax, chart)
    return _finish(fig, ax)


def render_pie(chart: Dict[str, Any]) -> bytes:
    fig, ax = _new_axes()
    ax.grid(False)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(left=False, bottom=False, labelleft=False, labelbottom=False)
    data = (chart.get("series") or [{}])[0].get("data") if chart.get("series") else chart.get("data")
    items = [(str(d.get("name", "")), _safe_float(d.get("value"))) for d in (data or []) if isinstance(d, dict)]
    items = [(n, v) for n, v in items if v is not None and v > 0]
    if items:
        ax.pie([v for _, v in items], labels=[n for n, _ in items],
               colors=[_color(i) for i in range(len(items))], autopct="%1.0f%%",
               textprops={"fontsize": 9, "fontfamily": getattr(ax, "_gibbs_family", None)})
    return _finish(fig, ax)


def _safe_float(v: Any) -> Optional[float]:
    try:
        f = float(v)
        return f if f == f else None
    except Exception:
        return None


_DISPATCH = {
    "line": render_line,
    "stacked_area": render_line,
    "rdf": render_rdf,
    "bar": render_bar,
    "scatter": render_scatter,
    "arrhenius": render_scatter,
    "nyquist": render_nyquist,
    "pie": render_pie,
}


def render_figure(chart: Dict[str, Any]) -> Optional[bytes]:
    """Render a figure spec to PNG bytes, or None if not renderable here.

    ``table`` blocks are laid out as HTML; ``structure`` figures embed a
    pre-captured PNG (``content.image``) when present, else None.
    """
    if not isinstance(chart, dict):
        return None
    ctype = str(chart.get("type") or "line")
    fn = _DISPATCH.get(ctype)
    if fn is None:
        return None
    try:
        return fn(chart)
    except Exception:
        return None


def render_figure_data_uri(chart: Dict[str, Any]) -> Optional[str]:
    png = render_figure(chart)
    if not png:
        return None
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")
