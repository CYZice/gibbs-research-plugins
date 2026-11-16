import { useCallback, useEffect, useMemo, useState } from "react";

type Props = { base: string; token: string };
type Taxonomy = {
  level: "domain" | "field" | "subfield" | "topic";
  id: string;
  display_name: string;
  works_count: number;
};
type HarvestJob = {
  job_id: string;
  name: string;
  status: string;
  estimated_items?: number;
  enumerated_items: number;
  staged_items: number;
  no_source_items: number;
  failed_items: number;
  registered_items: number;
  ingest_mode: "metadata_only" | "stage_only";
  next_retry_at?: string;
  next_poll_after_seconds: number;
  scope: Record<string, unknown>;
  filters: Record<string, unknown>;
  quota?: Record<string, any>;
};

const ACTIVE = new Set(["enumerating", "downloading", "waiting_quota"]);
const LEVEL_NAMES: Record<string, string> = {
  domain: "Domain",
  field: "Field",
  subfield: "Subfield",
  topic: "Topic",
};
const STATUS_NAMES: Record<string, string> = {
  enumerating: "枚举中",
  downloading: "下载中",
  waiting_quota: "等待免费额度",
  paused: "已暂停",
  completed: "已完成",
  completed_with_errors: "完成（有缺失）",
  cancelled: "已取消",
  failed: "失败",
  pending: "待下载",
  staged: "已暂存",
  registering: "注册中",
  registered: "已注册",
  no_source: "无可用源",
  metadata_ready: "源数据已收集",
};

export default function HarvestView({ base, token }: Props) {
  const [query, setQuery] = useState("electrochemistry");
  const [level, setLevel] = useState("");
  const [results, setResults] = useState<Taxonomy[]>([]);
  const [children, setChildren] = useState<Taxonomy[]>([]);
  const [selected, setSelected] = useState<Taxonomy | null>(null);
  const [name, setName] = useState("");
  const [yearFrom, setYearFrom] = useState("2015");
  const [yearTo, setYearTo] = useState(String(new Date().getFullYear()));
  const [types, setTypes] = useState("article,review");
  const [languages, setLanguages] = useState("");
  const [ingestMode, setIngestMode] = useState<"metadata_only" | "stage_only">("metadata_only");
  const [estimate, setEstimate] = useState<number | null>(null);
  const [jobs, setJobs] = useState<HarvestJob[]>([]);
  const [activeJob, setActiveJob] = useState<HarvestJob | null>(null);
  const [items, setItems] = useState<any[]>([]);
  const [itemStatus, setItemStatus] = useState("staged");
  const [selectedItems, setSelectedItems] = useState<string[]>([]);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("选择 OpenAlex 分类，预估后即可创建跨天采集任务。");

  const api = useCallback(async (method: string, path: string, body?: unknown) => {
    const headers: Record<string, string> = {};
    if (token.trim()) headers.Authorization = `Bearer ${token.trim()}`;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const response = await fetch(`${base.replace(/\/$/, "")}${path}`, {
      method,
      headers,
      credentials: "include",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = result.detail?.message || result.detail || result.error?.message;
      throw new Error(typeof detail === "string" ? detail : `HTTP ${response.status}`);
    }
    return result;
  }, [base, token]);

  const filters = useMemo(() => ({
    year_from: yearFrom ? Number(yearFrom) : null,
    year_to: yearTo ? Number(yearTo) : null,
    types: types.split(",").map((value) => value.trim()).filter(Boolean),
    languages: languages.split(",").map((value) => value.trim()).filter(Boolean),
  }), [languages, types, yearFrom, yearTo]);

  const scope = useMemo(() => selected ? {
    level: selected.level,
    openalex_id: selected.id,
    include_descendants: true,
    match_mode: "topics",
  } : null, [selected]);

  const loadJobs = useCallback(async () => {
    try {
      const result = await api("GET", "/api/v1/harvest/jobs?limit=50");
      setJobs(result.items || []);
    } catch (error: any) {
      setNotice(error.message === "Corpus harvest is disabled."
        ? "批量采集功能尚未启用，请管理员设置 HARVEST_ENABLED=true。"
        : error.message);
    }
  }, [api]);

  const loadItems = useCallback(async (jobId: string, status = itemStatus) => {
    try {
      const suffix = status ? `&status=${encodeURIComponent(status)}` : "";
      const result = await api("GET", `/api/v1/harvest/jobs/${jobId}/items?limit=200${suffix}`);
      setItems(result.items || []);
      setSelectedItems([]);
    } catch (error: any) {
      setNotice(error.message);
    }
  }, [api, itemStatus]);

  const openJob = useCallback(async (jobId: string) => {
    try {
      const job = await api("GET", `/api/v1/harvest/jobs/${jobId}`);
      setActiveJob(job);
      const status = job.ingest_mode === "metadata_only" ? "metadata_ready" : itemStatus;
      setItemStatus(status);
      await loadItems(jobId, status);
    } catch (error: any) {
      setNotice(error.message);
    }
  }, [api, loadItems]);

  useEffect(() => { void loadJobs(); }, [loadJobs]);
  useEffect(() => {
    if (!activeJob || !ACTIVE.has(activeJob.status)) return;
    const id = window.setTimeout(async () => {
      try {
        const job = await api("GET", `/api/v1/harvest/jobs/${activeJob.job_id}`);
        setActiveJob(job);
        setJobs((rows) => rows.map((row) => row.job_id === job.job_id ? job : row));
        if (!ACTIVE.has(job.status)) await loadItems(job.job_id);
      } catch (error: any) {
        setNotice(error.message);
      }
    }, Math.max(2, activeJob.next_poll_after_seconds || 2) * 1000);
    return () => window.clearTimeout(id);
  }, [activeJob, api, loadItems]);
  useEffect(() => {
    if (
      !activeJob
      || !items.some((item) =>
        ["pending", "running", "retrying", "waiting_external"].includes(
          item.pipeline_status,
        ),
      )
    ) return;
    const id = window.setTimeout(async () => {
      try {
        const job = await api("GET", `/api/v1/harvest/jobs/${activeJob.job_id}`);
        setActiveJob(job);
        setJobs((rows) => rows.map((row) => row.job_id === job.job_id ? job : row));
        await loadItems(job.job_id, itemStatus);
      } catch (error: any) {
        setNotice(error.message);
      }
    }, 5000);
    return () => window.clearTimeout(id);
  }, [activeJob, api, itemStatus, items, loadItems]);

  const search = async () => {
    if (!query.trim()) return;
    setBusy("search");
    try {
      const suffix = level ? `&level=${level}` : "";
      const result = await api("GET", `/api/v1/harvest/openalex/taxonomy/search?q=${encodeURIComponent(query.trim())}${suffix}`);
      setResults(result.items || []);
      setNotice(`找到 ${(result.items || []).length} 个 OpenAlex 分类。`);
    } catch (error: any) { setNotice(error.message); }
    finally { setBusy(""); }
  };

  const choose = async (item: Taxonomy) => {
    setSelected(item);
    setName(`${item.display_name} ${yearFrom || "all"}-${yearTo || "present"}`);
    setEstimate(null);
    setChildren([]);
    if (item.level === "topic") return;
    try {
      const result = await api("GET", `/api/v1/harvest/openalex/taxonomy/${item.level}/${encodeURIComponent(item.id)}/children`);
      setChildren(result.items || []);
    } catch (error: any) { setNotice(error.message); }
  };

  const estimateJob = async () => {
    if (!scope) return setNotice("请先选择一个 OpenAlex 分类。");
    setBusy("estimate");
    try {
      const result = await api("POST", "/api/v1/harvest/jobs/estimate", { scope, filters });
      setEstimate(result.estimated_items || 0);
      setNotice(`预计枚举 ${Number(result.estimated_items || 0).toLocaleString()} 篇；许可证字段不会参与下载判断。`);
    } catch (error: any) { setNotice(error.message); }
    finally { setBusy(""); }
  };

  const createJob = async () => {
    if (!scope || !name.trim()) return setNotice("请先选择分类并填写任务名称。");
    setBusy("create");
    try {
      const job = await api("POST", "/api/v1/harvest/jobs", {
        name: name.trim(),
        scope,
        filters,
        billing_mode: "free_credit_only",
        ingest_mode: ingestMode,
      });
      setJobs((rows) => [job, ...rows]);
      setActiveJob(job);
      setItems([]);
      setItemStatus(ingestMode === "metadata_only" ? "metadata_ready" : "staged");
      setNotice(ingestMode === "metadata_only"
        ? `源数据采集任务 ${job.job_id} 已启动，不会下载 PDF。`
        : `采集任务 ${job.job_id} 已启动。`);
    } catch (error: any) { setNotice(error.message); }
    finally { setBusy(""); }
  };

  const jobAction = async (action: "pause" | "resume" | "cancel" | "retry-failed") => {
    if (!activeJob) return;
    if (action === "cancel" && !window.confirm("取消后未下载项目不会继续；已暂存 PDF 会保留。确定取消吗？")) return;
    setBusy(action);
    try {
      const result = await api("POST", `/api/v1/harvest/jobs/${activeJob.job_id}/${action}`, {});
      const job = result.job || result;
      setActiveJob(job);
      await loadJobs();
      setNotice(action === "retry-failed" ? `已重新排队 ${result.retried_items || 0} 项。` : `任务状态：${STATUS_NAMES[job.status] || job.status}`);
    } catch (error: any) { setNotice(error.message); }
    finally { setBusy(""); }
  };

  const importItems = async (selection: "all_staged" | "items") => {
    if (!activeJob) return;
    if (selection === "items" && !selectedItems.length) return setNotice("请先勾选暂存文献。");
    setBusy("import");
    try {
      const result = await api("POST", `/api/v1/harvest/jobs/${activeJob.job_id}/imports`, {
        selection,
        item_ids: selection === "items" ? selectedItems : [],
      });
      setNotice(`已创建导入批次 ${result.batch_id}，共 ${result.total_items} 篇。`);
      setItemStatus("registering");
      await loadItems(activeJob.job_id, "registering");
    } catch (error: any) { setNotice(error.message); }
    finally { setBusy(""); }
  };

  const progress = activeJob?.estimated_items
    ? Math.min(100, Math.round(activeJob.enumerated_items / activeJob.estimated_items * 100))
    : 0;
  const remaining = activeJob?.quota?.daily_remaining_usd;

  return <section className="page harvest-page">
    <header className="harvest-heading">
      <h2>批量采集</h2>
      <button className="secondary" onClick={loadJobs}>刷新任务</button>
    </header>
    <div className="harvest-notice">{notice}</div>

    <div className="harvest-layout">
      <section className="surface harvest-builder">
        <h3>1. 选择分类范围</h3>
        <div className="harvest-form-row">
          <input value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={(event) => event.key === "Enter" && void search()} placeholder="检索 electrochemistry、battery…" />
          <select value={level} onChange={(event) => setLevel(event.target.value)}>
            <option value="">全部层级</option><option value="domain">Domain</option><option value="field">Field</option><option value="subfield">Subfield</option><option value="topic">Topic</option>
          </select>
          <button className="primary" disabled={busy === "search"} onClick={search}>{busy === "search" ? "检索中…" : "检索分类"}</button>
        </div>
        <div className="taxonomy-results">
          {results.map((item) => <button key={`${item.level}:${item.id}`} className={selected?.id === item.id ? "taxonomy-result selected" : "taxonomy-result"} onClick={() => choose(item)}>
            <span>{LEVEL_NAMES[item.level]}</span><b>{item.display_name}</b><small>{item.works_count.toLocaleString()} works</small>
          </button>)}
          {!results.length && <p className="muted">输入领域关键词，结果会同时覆盖 Domain、Field、Subfield 与 Topic。</p>}
        </div>
        {selected && <div className="selected-scope"><b>{selected.display_name}</b><span>{LEVEL_NAMES[selected.level]} · {selected.id}</span><small>高召回 topics 匹配；包含跨领域论文</small></div>}
        {!!children.length && <div className="taxonomy-children"><span>下属分类快照（点击可缩小范围）</span><div>{children.slice(0, 24).map((item) => <button key={item.id} onClick={() => choose(item)}>{item.display_name}<small>{item.works_count.toLocaleString()}</small></button>)}</div></div>}

        <h3>2. 过滤与预估</h3>
        <div className="harvest-grid-form">
          <label>任务名称<input value={name} onChange={(event) => setName(event.target.value)} /></label>
          <label>起始年份<input type="number" value={yearFrom} onChange={(event) => setYearFrom(event.target.value)} /></label>
          <label>结束年份<input type="number" value={yearTo} onChange={(event) => setYearTo(event.target.value)} /></label>
          <label>文献类型<input value={types} onChange={(event) => setTypes(event.target.value)} placeholder="article,review" /></label>
          <label>语言（可空）<input value={languages} onChange={(event) => setLanguages(event.target.value)} placeholder="en,zh" /></label>
          <label>采集方式<select value={ingestMode} onChange={(event) => setIngestMode(event.target.value as "metadata_only" | "stage_only")}><option value="metadata_only">仅收集元数据与来源</option><option value="stage_only">下载并暂存 PDF</option></select></label>
        </div>
        <div className="harvest-create-row">
          <div><strong>{estimate === null ? "尚未预估" : estimate.toLocaleString()}</strong><small>预计 Works 数；任务总量不设上限</small></div>
          <button className="secondary" disabled={!selected || busy === "estimate"} onClick={estimateJob}>预估数量</button>
          <button className="primary" disabled={!selected || !name.trim() || busy === "create"} onClick={createJob}>创建采集任务</button>
        </div>
      </section>

      <aside className="surface harvest-jobs">
        <h3>采集任务</h3>
        {jobs.map((job) => <button key={job.job_id} className={activeJob?.job_id === job.job_id ? "harvest-job selected" : "harvest-job"} onClick={() => openJob(job.job_id)}>
          <span className={`harvest-status ${job.status}`}>{STATUS_NAMES[job.status] || job.status}</span>
          <b>{job.name}</b>
          <small>{job.ingest_mode === "metadata_only" ? "仅源数据" : `${job.staged_items.toLocaleString()} 暂存`} · {job.enumerated_items.toLocaleString()} 枚举</small>
        </button>)}
        {!jobs.length && <p className="muted">暂无采集任务。</p>}
      </aside>
    </div>

    {activeJob && <section className="surface harvest-detail">
      <div className="harvest-detail-head">
        <div><p className="eyebrow">{activeJob.job_id}</p><h3>{activeJob.name}</h3><span className={`harvest-status ${activeJob.status}`}>{STATUS_NAMES[activeJob.status] || activeJob.status}</span></div>
        <div className="harvest-actions">
          {ACTIVE.has(activeJob.status) && <button className="secondary" onClick={() => jobAction("pause")}>暂停</button>}
          {activeJob.status === "paused" && <button className="primary" onClick={() => jobAction("resume")}>恢复</button>}
          {activeJob.failed_items + activeJob.no_source_items > 0 && <button className="secondary" onClick={() => jobAction("retry-failed")}>重试失败/无源</button>}
          {!["cancelled", "completed"].includes(activeJob.status) && <button className="danger" onClick={() => jobAction("cancel")}>取消</button>}
        </div>
      </div>
      <div className="harvest-progress"><i style={{ width: `${progress}%` }} /></div>
      <div className="harvest-metrics">
        <Metric label="预计" value={activeJob.estimated_items?.toLocaleString() || "—"} />
        <Metric label="已枚举" value={activeJob.enumerated_items.toLocaleString()} />
        <Metric label="PDF 暂存" value={activeJob.staged_items.toLocaleString()} />
        <Metric label="无源" value={activeJob.no_source_items.toLocaleString()} />
        <Metric label="失败" value={activeJob.failed_items.toLocaleString()} />
        <Metric label="已注册" value={activeJob.registered_items.toLocaleString()} />
      </div>
      <p className="muted">免费额度余额：{remaining == null ? "等待凭据数据" : `$${Number(remaining).toFixed(4)}`} · 预计还可下载 {activeJob.quota?.estimated_content_files ?? "—"} 篇 Content API PDF{activeJob.next_retry_at ? ` · ${new Date(activeJob.next_retry_at).toLocaleString()} 恢复` : ""}</p>

      <div className="harvest-items-head">
        <div><h3>任务文献</h3><select value={itemStatus} onChange={(event) => { setItemStatus(event.target.value); void loadItems(activeJob.job_id, event.target.value); }}>
          <option value="metadata_ready">源数据已收集</option><option value="staged">已暂存</option><option value="no_source">无可用源</option><option value="failed">失败</option><option value="pending">待下载</option><option value="registering">注册中</option><option value="registered">已注册</option><option value="">全部</option>
        </select></div>
        <span className="harvest-actions">
          <button className="secondary" onClick={() => loadItems(activeJob.job_id)}>刷新列表</button>
          <button className="secondary" disabled={!selectedItems.length || busy === "import"} onClick={() => importItems("items")}>导入所选</button>
          <button className="primary" disabled={!activeJob.staged_items || busy === "import"} onClick={() => importItems("all_staged")}>全部暂存入库</button>
        </span>
      </div>
      <div className="harvest-table">
        <div className="harvest-tr harvest-th"><span /><b>文献</b><b>年份</b><b>来源/状态</b><b>候选来源</b></div>
        {items.map((item) => <div className="harvest-tr" key={item.item_id}>
          <span><input type="checkbox" disabled={item.status !== "staged"} checked={selectedItems.includes(item.item_id)} onChange={() => setSelectedItems((ids) => ids.includes(item.item_id) ? ids.filter((id) => id !== item.item_id) : [...ids, item.item_id])} /></span>
          <span><b>{item.title || item.openalex_id}</b><small>{item.doi || item.openalex_id}</small></span>
          <span>{item.year || "—"}</span>
          <span>
            {item.selected_source || STATUS_NAMES[item.status] || item.status}
            {item.pipeline_stage && <small>{pipelineStage(item.pipeline_stage, item.pipeline_status)}</small>}
          </span>
          <span>{sourceAvailability(item.source_availability) || item.error_code || (item.pipeline_status === "failed" ? "PROCESSING_FAILED" : "—")}</span>
        </div>)}
        {!items.length && <p className="muted">当前筛选下暂无条目。</p>}
      </div>
    </section>}
  </section>;
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div><span>{label}</span><strong>{value}</strong></div>;
}

function pipelineStage(stage: string, status?: string) {
  if (status === "failed") return "处理失败";
  if (status === "succeeded" || stage === "succeeded") return "ready";
  return ({
    register: "注册 Paper",
    parse: "解析中",
    evidence: "内容构建",
    resource_context: "资源上下文",
    vectorize: "向量化",
    index: "索引",
  } as Record<string, string>)[stage] || stage;
}

function sourceAvailability(value?: Record<string, unknown>) {
  if (!value) return "";
  return ({
    free_candidate: "可免费尝试",
    direct_candidate_unknown: "有直链待验证",
    content_only: "仅 Content 可用",
    no_pdf_candidate: "无 PDF 候选",
  } as Record<string, string>)[String(value.classification || "")] || "";
}
