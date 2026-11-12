"""Server-side typeset PDF export (WeasyPrint + matplotlib figures + Jinja2).

The on-screen report (frontend ``BlockEditor`` + ``REPORT_EDITORIAL_CSS``) is the
single design reference; this module reproduces that editorial layout for the
"定版 PDF": a full "检测报告" cover, ink-navy accent, serif body with tabular
numerals, sans captions/labels/references, and numbered figures/tables. Figures
are pre-rendered to PNG via :mod:`figures` and base64-embedded (no temp files).

WeasyPrint is imported lazily so importing this module never requires it; if it
is not installed the endpoint returns 501 and the UI falls back to print-to-PDF.
"""

from __future__ import annotations

import html
import os
import re
from typing import Any, Dict, List

from jinja2 import Template

from app.services.report_engine import figures


class WeasyPrintUnavailable(RuntimeError):
    """Raised when WeasyPrint (or its native deps) is not installed."""


_ASSET_FONT_DIR = os.path.join(os.path.dirname(__file__), "assets", "fonts")

# Bundled CJK fonts (optional but recommended). Drop these .otf files into
# assets/fonts/ so the PDF is self-contained and matches the on-screen typography:
# a serif for the body (ReportSerif) and a sans for captions/labels/references
# (ReportSans). Each covers both Latin and CJK, so the whole document is serif or
# sans consistently. Only .otf is @font-face'd — WeasyPrint can render tofu from a
# .ttc bound via @font-face. Missing files fall back to the named CJK stack (which
# fontconfig resolves), so text is never lost.
_SERIF_CJK_CANDIDATES = [
    os.path.join(_ASSET_FONT_DIR, "NotoSerifCJKsc-Regular.otf"),
    os.path.join(_ASSET_FONT_DIR, "SourceHanSerifSC-Regular.otf"),
]
_SANS_CJK_CANDIDATES = [
    os.path.join(_ASSET_FONT_DIR, "NotoSansCJKsc-Regular.otf"),
    os.path.join(_ASSET_FONT_DIR, "SourceHanSansSC-Regular.otf"),
]


def _font_face(family: str, candidates: List[str]) -> str:
    for path in candidates:
        if os.path.exists(path):
            url = "file://" + path.replace(" ", "%20")
            return f"@font-face {{ font-family: '{family}'; src: url('{url}'); }}\n"
    return ""


def _font_face_css() -> str:
    """@font-face for bundled serif/sans CJK .otf files (each first in its stack)."""
    return _font_face("ReportSerif", _SERIF_CJK_CANDIDATES) + _font_face("ReportSans", _SANS_CJK_CANDIDATES)


# ---------------------------------------------------------------------------
# Editorial stylesheet — a print-tuned port of frontend REPORT_EDITORIAL_CSS.
# Same accent (#1a3a5c ink-navy), serif body, sans captions/labels. Sizes are
# tuned for A4 paper (≈ the browser print-clone), not for screen px.
# ---------------------------------------------------------------------------
_DOC_TEMPLATE = Template("""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
{{ font_face }}
:root {
  --ink: #16181d; --ink-soft: #3a414b; --muted: #8b9099; --accent: #1a3a5c; --rule: #e7e6e2;
  /* Bundled ReportSerif/ReportSans first (Latin + CJK), then named CJK fonts fontconfig can resolve. */
  --serif: "ReportSerif", Georgia, "Times New Roman", "Source Serif 4", "Noto Serif CJK SC",
           "Source Han Serif SC", "Songti SC", "Noto Sans CJK SC", "PingFang SC", "Microsoft YaHei", serif;
  --sans: "ReportSans", "Helvetica Neue", Arial, "Noto Sans CJK SC", "Source Han Sans SC",
          "PingFang SC", "Microsoft YaHei", "WenQuanYi Micro Hei", sans-serif;
}
@page {
  size: A4; margin: 20mm 18mm;
  @top-center { content: "{{ running_head }}"; font: 8pt Georgia, serif; color: #b7bcc4; }
  @bottom-center { content: counter(page) " / " counter(pages); font: 8pt Arial, sans-serif; color: #b7bcc4; }
}
@page cover { margin: 0; @top-center { content: none; } @bottom-center { content: none; } }

html, body { margin: 0; padding: 0; }
body { font-family: var(--serif); color: var(--ink-soft); font-size: 10.8pt; line-height: 1.85;
       font-variant-numeric: tabular-nums; }
p { margin: 0 0 8pt; text-align: justify; }

/* ---- section headers (HEADER blocks) ---- */
h2 { font-family: var(--serif); font-size: 15pt; font-weight: 600; color: var(--ink);
     letter-spacing: -0.004em; margin: 22pt 0 9pt; padding-bottom: 7pt; border-bottom: 1px solid var(--rule); }
h2.first { margin-top: 2pt; }
h2.pbreak { break-before: page; page-break-before: always; }

/* ---- body text ---- */
.text { margin: 10pt 0 12pt; }
.text p { color: var(--ink-soft); }
.method p { line-height: 1.95; text-indent: 2em; }
.section-note { font-family: var(--sans); font-size: 10pt; font-weight: 600; letter-spacing: .03em;
     color: var(--accent); border-left: 2pt solid var(--accent); padding-left: 8pt; margin: 0 0 8pt; }

/* ---- abstract (lede) ---- */
.abstract { margin: 12pt 0 16pt; padding: 2pt 0 2pt 14pt; border-left: 2pt solid var(--accent); }
.abstract p { color: var(--ink); font-size: 11.4pt; line-height: 1.8; text-align: left; }

/* ---- figures & tables ---- */
figure { margin: 14pt 0; break-inside: avoid; text-align: center; }
figure img { max-width: 100%; max-height: 180mm; border: 1px solid var(--rule); border-radius: 5px; }
.figcap, .tabcap { font-family: var(--sans); font-size: 9pt; line-height: 1.6; color: var(--muted);
     text-align: left; margin-top: 8pt; }
.figcap .num, .tabcap .num, .tabttl .num { color: var(--accent); font-weight: 700; }
.tabttl { font-family: var(--serif); font-size: 12pt; font-weight: 600; color: var(--ink); margin: 0 0 8pt; }
.card { border: 1px solid var(--rule); border-radius: 5px; padding: 12pt 14pt; margin: 16pt 0; break-inside: avoid; }
table.data { width: 100%; border-collapse: collapse; font-size: 9.6pt; }
table.data th { text-align: left; font-weight: 700; color: var(--ink); background: #f8fafc;
     padding: 6pt 8pt; border-bottom: 1.2px solid #e3e6ea; }
table.data td { padding: 5pt 8pt; border-bottom: 1px solid #f0f0f0; vertical-align: top;
     word-break: break-word; }
table.data tr:last-child td { border-bottom: none; }

/* ---- references ---- */
ol.refs { margin: 12pt 0 6pt; padding-left: 0; }
ol.refs li { list-style: none; font-family: var(--sans); font-size: 10pt; line-height: 1.7;
     color: var(--ink-soft); text-indent: -1.7em; padding-left: 1.7em; margin-bottom: 6pt; }
.appendix { font-family: var(--sans); font-size: 9pt; color: var(--muted); }

/* ---- cover (full A4 page) ---- */
.cover { page: cover; box-sizing: border-box; height: 295mm; padding: 30mm 24mm 22mm;
     display: flex; flex-direction: column; }
.cover .cv-cn { text-align: center; font-family: var(--sans); font-size: 30pt; font-weight: 800;
     letter-spacing: 0.26em; color: #111827; margin-bottom: 8pt; }
.cover .cv-en { text-align: center; font-family: var(--sans); font-size: 11pt; letter-spacing: 0.34em;
     color: #64748b; margin-bottom: 22pt; }
.cover .cv-title { text-align: center; font-family: var(--serif); font-size: 21pt; font-weight: 600;
     line-height: 1.35; color: var(--ink); margin: 0 auto; max-width: 30em; }
.cover .cv-meta { margin-top: 30mm; border-top: 1px solid #e5e7eb; border-bottom: 1px solid #e5e7eb;
     padding: 12pt 0; }
.cover table.cv-grid { width: 100%; border-collapse: collapse; }
.cover table.cv-grid td { padding: 9pt 4pt; vertical-align: baseline; width: 50%; }
.cover .cv-lbl { font-family: var(--sans); font-size: 9.5pt; color: #8c8c8c; }
.cover .cv-val { font-family: var(--serif); font-size: 12.5pt; font-weight: 600; color: #111827; margin-top: 2pt; }
.cover .cv-foot { margin-top: auto; display: flex; justify-content: space-between; align-items: flex-end; }
.cover .cv-amt-lbl, .cover .cv-prov-lbl { font-family: var(--sans); font-size: 9.5pt; color: #64748b; margin-bottom: 5pt; }
.cover .cv-amt-val { font-family: var(--serif); font-size: 17pt; font-weight: 800; color: #7c2d12; }
.cover .cv-prov { text-align: right; }
.cover .cv-prov-val { font-family: var(--serif); font-size: 14pt; font-weight: 700; color: var(--ink); }
.cover .cv-prov-code { font-family: var(--sans); font-size: 9pt; color: #64748b; margin-top: 4pt; }
</style></head><body>
{{ cover }}
{{ body }}
</body></html>""")


def _esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""))


def _bold(escaped: str) -> str:
    """把已转义文本里的轻量 Markdown 粗体 **关键参数** 渲染为 <strong>。"""
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)


def _paragraphs(text: Any, method: bool = False) -> str:
    raw = str(text or "")
    if method:  # mirror frontend normalizeMethodText: strip bullets, collapse blanks
        raw = re.sub(r"^Analysis protocols:\s*", "", raw, flags=re.I)
        raw = re.sub(r"\n\s*[-•]\s+", "\n\n", raw)
        raw = re.sub(r"(?m)^\s*[-•]\s+", "", raw)
    parts = [p.strip() for p in re.split(r"\n{2,}", raw) if p.strip()] or \
            [p.strip() for p in raw.split("\n") if p.strip()]
    return "".join(f"<p>{_bold(_esc(p))}</p>" for p in parts)


def _cover_html(blocks: List[Any]) -> str:
    cover = next((b for b in blocks if b.block_type == "COVER"), None)
    if cover is None:
        return ""
    c = cover.content or {}

    def amt(v: Any) -> str:
        try:
            return "¥" + format(float(v), ",.2f")
        except (TypeError, ValueError):
            return "-"

    fields = [
        ("样品名称", c.get("sample_name") or "计算模拟样品"),
        ("样品数量", f"{c.get('sample_count')} 个" if c.get("sample_count") is not None else "-"),
        ("委托单位", c.get("counterparty_name") or "-"),
        ("统一社会信用代码", c.get("counterparty_credit_code") or "-"),
        ("测试/服务单位", c.get("service_provider_name") or "-"),
        ("委托类别", "委托测试"),
        ("报告编号", c.get("report_code") or "-"),
        ("生成日期", c.get("created_date") or "-"),
    ]
    cells = "".join(
        f'<td><div class="cv-lbl">{_esc(k)}</div><div class="cv-val">{_esc(v)}</div></td>'
        + ("</tr><tr>" if i % 2 == 1 and i != len(fields) - 1 else "")
        for i, (k, v) in enumerate(fields)
    )
    total = c.get("total_amount_cny")
    return (
        '<div class="cover">'
        f'<div class="cv-cn">{_esc(c.get("title_cn") or "检 测 报 告")}</div>'
        f'<div class="cv-en">{_esc(c.get("title_en") or "TESTING REPORT")}</div>'
        f'<div class="cv-title">{_esc(c.get("report_title") or "研究报告")}</div>'
        f'<div class="cv-meta"><table class="cv-grid"><tr>{cells}</tr></table></div>'
        '<div class="cv-foot">'
        f'<div><div class="cv-amt-lbl">服务金额</div><div class="cv-amt-val">{amt(total) if total is not None else "-"}</div></div>'
        f'<div class="cv-prov"><div class="cv-prov-lbl">服务方</div>'
        f'<div class="cv-prov-val">{_esc(c.get("service_provider_name") or "-")}</div>'
        f'<div class="cv-prov-code">统一社会信用代码：{_esc(c.get("service_provider_credit_code") or "-")}</div></div>'
        '</div></div>'
    )


def build_report_html(report: Any, blocks: List[Any]) -> str:
    ordered = sorted(blocks, key=lambda b: (b.order_index if b.order_index is not None else 0))
    fig_n = 0
    tab_n = 0
    body_parts: List[str] = []
    first_header = True

    for b in ordered:
        c: Dict[str, Any] = b.content or {}
        btype = b.block_type
        if btype == "COVER":
            continue
        if btype == "PAGE_BREAK":
            body_parts.append("<div style='break-before:page;page-break-before:always'></div>")
            continue
        if btype == "HEADER":
            text = str(c.get("text") or "")
            classes = []
            if first_header:
                classes.append("first")
                first_header = False
            if "计算方法" in text or "结果详情" in text:
                classes.append("pbreak")
            cls = (' class="%s"' % " ".join(classes)) if classes else ""
            body_parts.append(f"<h2{cls}>{_esc(text)}</h2>")
        elif btype == "TEXT":
            tags = c.get("tags") or []
            section = c.get("section_title")
            if "REFERENCES" in tags:
                lines = [ln.strip() for ln in str(c.get("text") or "").split("\n") if ln.strip()]
                items = "".join(f"<li>{_esc(ln)}</li>" for ln in lines)
                body_parts.append(f"<ol class='refs'>{items}</ol>")
            else:
                is_method = any("METHOD" in str(t) for t in tags)
                is_abstract = "ABSTRACT" in tags
                is_appendix = "APPENDIX" in tags
                note = f"<div class='section-note'>{_esc(section)}</div>" if section else ""
                cls = "text"
                if is_abstract:
                    cls += " abstract"
                if is_method:
                    cls += " method"
                if is_appendix:
                    cls += " appendix"
                body_parts.append(f"<div class='{cls}'>{note}{_paragraphs(c.get('text'), is_method)}</div>")
        elif btype == "FIGURE":
            chart = c.get("chart")
            if isinstance(chart, dict) and chart.get("type") == "structure":
                fig_n += 1
                body_parts.append(
                    f"<figure><figcaption class='figcap'><span class='num'>图 {fig_n}.</span> "
                    f"{_esc(c.get('caption') or c.get('title'))}（交互式结构请见在线版本）</figcaption></figure>")
            elif c.get("image"):
                fig_n += 1
                body_parts.append(_figure_html(fig_n, _image_src(c.get("image")), c.get("caption") or c.get("title")))
            elif isinstance(chart, dict):
                uri = figures.render_figure_data_uri(chart)
                if uri:
                    fig_n += 1
                    body_parts.append(_figure_html(fig_n, uri, c.get("caption") or c.get("title")))
        elif btype == "TABLE":
            tab_n += 1
            body_parts.append(_table_html(tab_n, c))

    running_head = str(getattr(report, "title", "") or "研究报告")[:80]
    return _DOC_TEMPLATE.render(
        running_head=_esc(running_head),
        font_face=_font_face_css(),
        cover=_cover_html(ordered),
        body="".join(body_parts),
    )


def _figure_html(n: int, src: str, caption: Any) -> str:
    return (f"<figure><img src='{src}'/>"
            f"<figcaption class='figcap'><span class='num'>图 {n}.</span> {_esc(caption)}</figcaption></figure>")


def _image_src(image: Any) -> str:
    if isinstance(image, str):
        return image if image.startswith("data:") else "data:image/png;base64," + image
    if isinstance(image, dict):
        data = image.get("data") or image.get("base64") or ""
        mime = image.get("mime_type") or "image/png"
        if isinstance(data, str) and data.startswith("data:"):
            return data
        return f"data:{mime};base64,{data}"
    return ""


def _table_html(n: int, content: Dict[str, Any]) -> str:
    columns = content.get("columns") or []
    rows = content.get("rows") or []
    # reports.py 表用 {title, dataIndex, key}；统一管线表用 {label, key}。两者都支持。
    keys = [(col.get("dataIndex") or col.get("key")) if isinstance(col, dict) else str(col) for col in columns]
    heads = [(col.get("title") or col.get("label") or "") if isinstance(col, dict) else str(col) for col in columns]
    thead = "".join(f"<th>{_esc(h)}</th>" for h in heads)
    body_rows = []
    for r in rows:
        if isinstance(r, dict):
            cells = "".join(f"<td>{_esc(r.get(k, ''))}</td>" for k in keys)
        elif isinstance(r, (list, tuple)):
            cells = "".join(f"<td>{_esc(v)}</td>" for v in r)
        else:
            cells = f"<td>{_esc(r)}</td>"
        body_rows.append(f"<tr>{cells}</tr>")
    title = content.get("title") or "表"
    caption = content.get("caption")
    # 编号只出现一次：标题行 "表 N. 标题"；图注行只放说明文字（不再重复编号）。
    cap_html = f"<div class='tabcap'>{_esc(caption)}</div>" if caption else ""
    return (f"<div class='card'>"
            f"<div class='tabttl'><span class='num'>表 {n}.</span> {_esc(title)}</div>"
            f"<table class='data'><thead><tr>{thead}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"
            f"{cap_html}</div>")


def render_report_pdf(report: Any, blocks: List[Any]) -> bytes:
    """Render the report to a typeset PDF (bytes). Raises WeasyPrintUnavailable."""
    try:
        from weasyprint import HTML  # type: ignore
        from weasyprint.text.fonts import FontConfiguration  # type: ignore
    except Exception as exc:  # ImportError or native-lib load failure
        raise WeasyPrintUnavailable(str(exc)) from exc

    html_str = build_report_html(report, blocks)
    font_config = FontConfiguration()
    return HTML(string=html_str, base_url=os.path.dirname(__file__)).write_pdf(font_config=font_config)
