"""LLM narrative synthesis for research reports — the caged author.

The LLM is fed ONLY the deterministic evidence packs (real numbers + figures +
provenance-derived citation numbers). It synthesizes IMRaD prose but may never
invent a number or a citation. ``validate_narrative`` enforces this after the
fact (hard whitelist for citations/figures; soft guard for stray numbers), and
``deterministic_narrative`` is a no-LLM fallback so a report is always produced.

Heavy imports (langchain / LLM client) are deferred into the functions that
need them, so ``validate_narrative`` and ``deterministic_narrative`` import and
run with no external dependencies — they are unit-testable on their own.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from app.services.report_engine.citations import inline_marker, reference_entries
from app.services.report_engine.evidence.pack import EvidencePack, pack_to_dict


CITE_RE = re.compile(r"\[\[cite:([A-Za-z0-9_]+)\]\]")
FIG_RE = re.compile(r"\[\[fig:([A-Za-z0-9_:.-]+)\]\]")
# numbers with a decimal point or scientific notation worth guarding
NUM_RE = re.compile(r"-?\d+\.\d+(?:[eE][-+]?\d+)?|-?\d+[eE][-+]?\d+")


NARRATIVE_SYSTEM_PROMPT = """你是 Gibbs 计算平台的资深研究报告撰稿人。你将拿到一份「证据包」(JSON)——里面是各计算任务的真实数值、图、方法出处与编号引文。你的任务：把它写成一份**专业、可直接交付的中文研究报告正文**（IMRaD 体例）。

【铁律 · 关进笼子】
1. 只能使用证据包里出现的数值；**严禁**自行计算、外推、改写精度或编造任何数字。引用数值时带上单位。
2. 引文只能用证据包中各任务 provenance 里给出的编号标记，写成 `[[cite:KEY]]`（KEY 用证据里给的引文键）。**严禁**编造作者、年份、期刊或 DOI，**严禁**引用未给出的文献。
3. 图只能用证据里给出的 `figure_id`，在正文相应位置写成 `[[fig:ID]]`，让排版系统在那里插图。不要描述不存在的图。
4. 必须在「讨论」里如实复述每个任务的 caveats（局限/前提），不得夸大或过度断言；对每条关键结论标注置信度（高/中/低）。
5. 若某任务暂无结果(status 非 COMPLETED 或 metrics 为空)，如实说明"该任务结果待生成/不完整"，不要编造结果。

【结构 · 各节职责互斥，杜绝重复】
- abstract（摘要）：3-5 句，只给最关键的量化发现与总体结论/推荐。
- background（研究背景与目标）：交代体系与研究问题，不重复后文数字。
- methods（研究方法）：按方法分组，讲"怎么做"(软件/力场或理论级别/系综/采样/分析口径)，**不出结果数字**；同方法的多个任务合并成一段。
- results_by_system（结果）：逐体系讲"测到了什么"，引用图与真实数值，**不复述方法**。
- comparison（横向对比）：**唯一**做跨体系比较与排序的地方，给出优劣排序与权衡。
- discussion（讨论）：机理解读、caveats、置信度、与已知规律的关系。
- conclusions（结论）：凝练判断（数组，每条一句）。
- recommendations（建议）：可执行的下一步（数组）。

【输出】严格输出**一个 JSON 对象**，不要 markdown 代码块、不要额外解释。字段：
{
 "title": str,
 "abstract": str,
 "background": str,
 "methods": [{"section": str(kind), "title": str, "prose": str}],
 "results_by_system": [{"source_ref": str, "title": str, "prose": str}],
 "comparison": str,
 "discussion": str,
 "conclusions": [str],
 "recommendations": [str]
}
"""


# ---------------------------------------------------------------------------
# Evidence digest (what the LLM actually reads)
# ---------------------------------------------------------------------------

def build_evidence_digest(packs: List[EvidencePack]) -> str:
    """Compact JSON the LLM consumes — numbers + figure ids + citation keys."""
    items = []
    for p in packs:
        d = pack_to_dict(p)
        # keep it terse: figures as id+title+caption only (not full data arrays)
        d["figures"] = [{"figure_id": f["figure_id"], "title": f["title"], "caption": f.get("caption", "")}
                        for f in d.get("figures", [])]
        items.append(d)
    return json.dumps(items, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# LLM synthesis
# ---------------------------------------------------------------------------

async def synthesize_narrative(
    packs: List[EvidencePack],
    *,
    title: str,
    template_type: str,
    ref_map: Dict[str, int],
) -> Dict[str, Any]:
    """Single caged LLM call → parsed narrative dict. Raises on failure."""
    from langchain_core.messages import HumanMessage, SystemMessage  # deferred

    from app.agent.agents.llm_config import get_brain_llm  # deferred
    from app.config import settings

    temperature = float(getattr(settings, "AGENT_BRAIN_TEMPERATURE", 0.3) or 0.3)
    llm = get_brain_llm(streaming=False, temperature=temperature, timeout=90.0)

    focus = {
        "mechanism": "侧重机理与结构-性质关系。",
        "performance": "侧重性能指标、权衡空间与候选配方排序。",
        "failure": "侧重失效模式诊断与风险点。",
    }.get((template_type or "").lower(), "综合呈现方法、结果与结论。")

    digest = build_evidence_digest(packs)
    human = (
        f"报告标题：{title}\n"
        f"报告侧重：{focus}\n"
        f"参考文献编号映射（KEY→编号，仅供你理解，正文请用 [[cite:KEY]]）：{json.dumps(ref_map, ensure_ascii=False)}\n\n"
        f"证据包（JSON 数组，每个元素是一个计算任务/材料）：\n{digest}\n\n"
        "请据此输出研究报告正文 JSON。"
    )
    ai = await llm.ainvoke([SystemMessage(content=NARRATIVE_SYSTEM_PROMPT), HumanMessage(content=human)])
    text = str(getattr(ai, "content", "") or "")
    return _parse_json_object(text)


def _parse_json_object(text: str) -> Dict[str, Any]:
    """Extract the first top-level JSON object from an LLM reply."""
    text = text.strip()
    # strip ```json fences if present
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError("narrative LLM did not return a JSON object")


# ---------------------------------------------------------------------------
# Validation (hard whitelist for cites/figures; soft guard for numbers)
# ---------------------------------------------------------------------------

def validate_narrative(
    doc: Dict[str, Any],
    packs: List[EvidencePack],
    ref_map: Dict[str, int],
) -> Tuple[Dict[str, Any], List[str]]:
    """Return (clean_doc, warnings).

    - `[[cite:KEY]]` → `[n]` when KEY is in ref_map, else stripped (+warn).
    - `[[fig:ID]]` kept only when ID is a real figure id, else stripped (+warn).
    - stray decimal numbers not present in the evidence are flagged (soft).
    """
    warnings: List[str] = []
    valid_fig_ids = {f.figure_id for p in packs for f in p.figures}
    allowed_numbers = _collect_allowed_numbers(packs)

    bad_cites: set = set()
    bad_figs: set = set()
    stray_numbers: set = set()

    def fix_text(s: Any) -> Any:
        if not isinstance(s, str):
            return s
        def _cite(m):
            key = m.group(1)
            marker = inline_marker(key, ref_map)
            if not marker:
                bad_cites.add(key)
                return ""
            return marker
        s = CITE_RE.sub(_cite, s)
        def _fig(m):
            fid = m.group(1)
            if fid in valid_fig_ids:
                return m.group(0)
            bad_figs.add(fid)
            return ""
        s = FIG_RE.sub(_fig, s)
        # soft number guard
        for tok in NUM_RE.findall(s):
            if not _number_allowed(tok, allowed_numbers):
                stray_numbers.add(tok)
        return s

    clean = _map_strings(doc, fix_text)

    if bad_cites:
        warnings.append(f"剔除了 {len(bad_cites)} 个无效引文标记：{', '.join(sorted(bad_cites))}")
    if bad_figs:
        warnings.append(f"剔除了 {len(bad_figs)} 个无效图引用：{', '.join(sorted(bad_figs))}")
    if stray_numbers:
        sample = ", ".join(sorted(stray_numbers)[:8])
        warnings.append(f"检测到 {len(stray_numbers)} 个未在证据中出现的数值(请人工复核)：{sample}")
    return clean, warnings


def _map_strings(obj: Any, fn) -> Any:
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, list):
        return [_map_strings(x, fn) for x in obj]
    if isinstance(obj, dict):
        return {k: _map_strings(v, fn) for k, v in obj.items()}
    return obj


def _collect_allowed_numbers(packs: List[EvidencePack]) -> set:
    out: set = set()
    for p in packs:
        for m in p.metrics:
            _add_number(out, m.value)
            if isinstance(m.quality, dict):
                for v in m.quality.values():
                    _add_number(out, v)
        for f in p.figures:
            chart = f.chart or {}
            for v in (chart.get("fit") or {}).values() if isinstance(chart.get("fit"), dict) else []:
                _add_number(out, v)
    return out


def _add_number(out: set, value: Any) -> None:
    try:
        f = float(value)
    except Exception:
        return
    # Keep full precision — rounding to 6 decimals destroys small values
    # (1.07e-6 -> 0.000001) and made the soft guard flag real numbers.
    out.add(f)


def _number_allowed(token: str, allowed: set, rel_tol: float = 0.02) -> bool:
    try:
        v = float(token)
    except Exception:
        return True
    for a in allowed:
        if a == 0:
            if abs(v) < 1e-9:
                return True
            continue
        if abs(v - a) <= abs(a) * rel_tol:
            return True
        # allow rescaled units (e.g. S/cm vs mS/cm) within a few orders
        for scale in (1e-3, 1e3, 1e-2, 1e2, 1e-6, 1e6):
            if abs(v - a * scale) <= abs(a * scale) * rel_tol:
                return True
    return False


# ---------------------------------------------------------------------------
# Deterministic fallback (no LLM) — still produces a coherent IMRaD doc
# ---------------------------------------------------------------------------

def deterministic_narrative(
    packs: List[EvidencePack],
    *,
    title: str,
    template_type: str,
    ref_map: Dict[str, int],
) -> Dict[str, Any]:
    by_kind: Dict[str, List[EvidencePack]] = {}
    for p in packs:
        by_kind.setdefault(p.kind, []).append(p)

    method_names = {
        "md": "经典分子动力学 (MD)", "qc": "量子化学单分子计算",
        "electrode_aimd": "界面第一性原理分子动力学 (AIMD)", "sse": "无机固态电解质多保真筛选",
    }

    n = len(packs)
    abstract = (
        f"本报告汇总了 {n} 项计算任务的结果，涵盖 {('、'.join(method_names.get(k, k) for k in by_kind))}。"
        "下文按研究方法、结果、对比与结论分节呈现，所有数值均来自平台确定性计算。"
    )
    background = "本研究围绕电解质/电极材料的关键物性展开，目标是基于一致的计算口径对候选体系进行定量评估与横向比较。"

    methods = []
    for kind, group in by_kind.items():
        prov = group[0].provenance
        cites = "".join(inline_marker(k, ref_map) for k in prov.citation_keys if k in ref_map)
        prose = f"采用{prov.method}（{prov.engine or '-'}{('，' + prov.level) if prov.level else ''}）。{cites}"
        methods.append({"section": kind, "title": method_names.get(kind, kind), "prose": prose})

    results = []
    for p in packs:
        if p.has_results():
            bits = "；".join(f"{m.label} {m.value}{(' ' + m.unit) if m.unit else ''}" for m in p.metrics[:6])
            figs = "".join(f"[[fig:{f.figure_id}]]" for f in p.figures)
            prose = f"{bits}。{figs}" if bits else f"已生成图表。{figs}"
        else:
            prose = "该任务结果待生成或不完整，下文以方法与参数作为复核入口。"
        results.append({"source_ref": p.ref, "title": p.system_label, "prose": prose})

    comparison = _deterministic_comparison(packs)
    caveats = sorted({c for p in packs for c in p.caveats})
    discussion = "需要注意的前提与局限：" + "；".join(caveats) if caveats else "结果在各自方法的适用范围内成立。"
    conclusions = ["上述结果可作为配方/材料横向比较与后续验证的统一依据。"]
    recommendations = ["对关键候选补充更长采样或更高保真度计算以收紧不确定度。"]

    return {
        "title": title,
        "abstract": abstract,
        "background": background,
        "methods": methods,
        "results_by_system": results,
        "comparison": comparison,
        "discussion": discussion,
        "conclusions": conclusions,
        "recommendations": recommendations,
    }


def _deterministic_comparison(packs: List[EvidencePack]) -> str:
    # rank MD systems by ionic conductivity when available
    cond = []
    for p in packs:
        if p.kind != "md":
            continue
        for m in p.metrics:
            if m.name == "ionic_conductivity" and isinstance(m.value, (int, float)):
                cond.append((p.system_label, float(m.value)))
    if len(cond) >= 2:
        cond.sort(key=lambda x: x[1], reverse=True)
        ranked = "；".join(f"{lbl}（{val:.3g} S/cm）" for lbl, val in cond)
        return f"按离子电导率从高到低排序：{ranked}。"
    return "本批次任务覆盖不同方法/体系，建议结合各自指标在对应维度上比较，不宜用单一指标一刀切。"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def generate_narrative(
    packs: List[EvidencePack],
    *,
    title: str,
    template_type: str = "mechanism",
    ref_map: Optional[Dict[str, int]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Try the caged LLM; fall back to deterministic. Returns (doc, meta)."""
    ref_map = ref_map or {}
    meta: Dict[str, Any] = {"generated_by": "llm", "warnings": []}

    llm_ok = False
    try:
        from app.config import settings
        llm_ok = bool(getattr(settings, "AGENT_ENABLED", True)) and bool(getattr(settings, "LLM_API_KEY", ""))
    except Exception:
        llm_ok = False

    if llm_ok:
        try:
            doc = await synthesize_narrative(packs, title=title, template_type=template_type, ref_map=ref_map)
            doc, warnings = validate_narrative(doc, packs, ref_map)
            meta["warnings"] = warnings
            doc.setdefault("title", title)
            return doc, meta
        except Exception as exc:  # noqa: BLE001 — fall back rather than fail the report
            meta["warnings"].append(f"LLM 综合失败，已回退确定性模板：{type(exc).__name__}")

    meta["generated_by"] = "deterministic"
    doc = deterministic_narrative(packs, title=title, template_type=template_type, ref_map=ref_map)
    # deterministic prose only uses valid cite/fig markers, but run validation for [[cite]]→[n]
    doc, warnings = validate_narrative(doc, packs, ref_map)
    meta["warnings"].extend(warnings)
    return doc, meta


def build_reference_section(ref_map: Dict[str, int]) -> List[Dict[str, Any]]:
    """Numbered reference entries for the References block."""
    return reference_entries(ref_map)
