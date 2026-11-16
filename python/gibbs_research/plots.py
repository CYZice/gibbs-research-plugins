"""Validated plot specs and immutable redraws."""

from __future__ import annotations

import math
import threading

from matplotlib.colors import is_color_like

from .core import figures

RENDER_LOCK = threading.RLock()


def validate_spec(spec):
    if not isinstance(spec, dict) or spec.get("type", "line") not in figures._DISPATCH:
        raise ValueError("Unsupported chart type")
    points = 0

    def visit(value):
        nonlocal points
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Chart data must contain finite numbers")
        if isinstance(value, list):
            points += len(value)
            if points > 200_000:
                raise ValueError("Chart exceeds 200000 values")
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            for item in value.values():
                visit(item)
    visit(spec)
    series = spec.get("series") or [{"data": spec.get("data", [])}]
    if not any(s.get("data") for s in series):
        raise ValueError("Chart has no data")
    chart_type = spec.get("type", "line")
    if chart_type == "bar":
        categories = spec.get("categories", [])
        if not categories:
            raise ValueError("Bar charts require categories")
        for item in series:
            if len(item.get("data", [])) != len(categories):
                raise ValueError("Bar data length must match categories")
            if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in item["data"]):
                raise ValueError("Bar data must be finite numeric values")
    elif chart_type == "pie":
        for item in series:
            data = item.get("data", [])
            if not all(isinstance(v, dict) and isinstance(v.get("value"), (int, float)) and math.isfinite(v["value"]) and v["value"] >= 0 for v in data):
                raise ValueError("Pie data requires name/value entries with nonnegative finite values")
            if not any(v["value"] > 0 for v in data):
                raise ValueError("Pie chart total must be positive")
    else:
        for series_item in series:
            for pair in series_item.get("data", []):
                if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                    raise ValueError("Expected numeric [x,y] pairs")
                try:
                    if not all(math.isfinite(float(v)) for v in pair[:2]):
                        raise ValueError()
                except (ValueError, TypeError):
                    raise ValueError("Expected finite numeric [x,y] pairs") from None
    style = spec.get("style", {})
    if not isinstance(style, dict):
        raise ValueError("Chart style must be an object")
    if not isinstance(style.get("font_size", 12), (int, float)) or not 6 <= style.get("font_size", 12) <= 40:
        raise ValueError("Font size must be between 6 and 40")
    if style.get("legend_location", "best") not in {"best", "upper right", "upper left", "lower left", "lower right", "right", "center left", "center right", "lower center", "upper center", "center"}:
        raise ValueError("Unsupported legend location")
    if "colors" in style and (not isinstance(style["colors"], list) or not style["colors"] or not all(is_color_like(v) for v in style["colors"])):
        raise ValueError("Colors must be a nonempty list of Matplotlib colors")
    if chart_type == "stacked_area" and series:
        x = [p[0] for p in series[0]["data"]]
        if any([p[0] for p in item["data"]] != x for item in series):
            raise ValueError("Stacked area series must share the same x coordinates")
    size = style.get("figsize", [6.6, 4.1])
    if not isinstance(size, list) or len(size) != 2 or not all(isinstance(v, (int, float)) and 1 <= v <= 20 for v in size):
        raise ValueError("Figure dimensions must be between 1 and 20 inches")
    return spec


def render(spec, fmt="png"):
    validate_spec(spec)
    if fmt not in {"png", "svg", "pdf"}:
        raise ValueError("Format must be png, svg or pdf")
    with RENDER_LOCK:
        result = figures.render_figure(spec, output_format=fmt)
    if not result:
        raise ValueError("Chart rendering failed; check the chart data contract")
    return result
