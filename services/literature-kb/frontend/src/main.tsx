import { StrictMode, Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";
import "./harvest.css";

type View = "dashboard" | "library" | "studio" | "harvest" | "admin" | "reference";
type Tab = "search" | "context" | "source" | "resource" | "pack" | "grounding";
type Scope = "library" | "selected" | "custom";
type Debug = "off" | "basic" | "verbose";
type CatalogSource = "local" | "all" | string;
type Paper = Record<string, any> & { paper_id: string; title?: string };
type Task = Record<string, any> & { task_id: string; paper_id?: string; status: string; progress?: number };
type Hit = Record<string, any> & { evidence_id: string; paper?: Paper; content?: Record<string, string>; assets?: any[] };
type Call = { method: string; path: string; body?: unknown; response?: unknown; status?: number; latency: number; requestId?: string | null; error?: string; at: string };
const K = { base: "lkb-base", debug: "lkb-debug", scope: "lkb-scope", custom: "lkb-custom", task: "lkb-task", tab: "lkb-tab", evidence: "lkb-evidence-id" };
const DEFAULT_BASE = import.meta.env.VITE_API_BASE_URL || (import.meta.env.PROD ? "/" : "http://127.0.0.1:8000");
const RETIRED_LOCAL_BASES = new Set(["http://127.0.0.1:8012", "http://localhost:8012"]);
let pdfjsPromise: Promise<typeof import("pdfjs-dist")> | undefined;
function loadPdfjs() { pdfjsPromise ??= import("pdfjs-dist").then((pdfjs) => { pdfjs.GlobalWorkerOptions.workerSrc = new URL("pdfjs-dist/build/pdf.worker.min.mjs", import.meta.url).toString(); return pdfjs; }); return pdfjsPromise; }
const AdminView = lazy(() => import("./views/AdminView"));
const HarvestView = lazy(() => import("./views/HarvestView"));

function App() {
  const [view, setView] = useState<View>("library");
  const [base, setBase] = useState(() => {
    const saved = read(K.base, DEFAULT_BASE);
    if (import.meta.env.DEV && RETIRED_LOCAL_BASES.has(saved)) return DEFAULT_BASE;
    return import.meta.env.PROD && saved === "http://127.0.0.1:8000" ? "/" : saved;
  });
  const [token, setToken] = useState("");
  const [debug, setDebug] = useState<Debug>(() => read(K.debug, "basic"));
  const [health, setHealth] = useState<any>(null);
  const [healthError, setHealthError] = useState("");
  const [overview, setOverview] = useState<any>(null);
  const [papers, setPapers] = useState<Paper[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [query, setQuery] = useState("");
  const [yearFrom, setYearFrom] = useState("");
  const [yearTo, setYearTo] = useState("");
  const [ready, setReady] = useState("");
  const [catalogSource, setCatalogSource] = useState<CatalogSource>("local");
  const [discoverySources, setDiscoverySources] = useState<any[]>([{ source: "openalex", role: "search_and_resolve" }]);
  const [importSources, setImportSources] = useState<Record<string, string>>({});
  const [literatureMeta, setLiteratureMeta] = useState<any>(null);
  const [loading, setLoading] = useState(false);
  const [paper, setPaper] = useState<Paper | null>(null);
  const [content, setContent] = useState<any>(null);
  const [assets, setAssets] = useState<any[]>([]);
  const [drawerTab, setDrawerTab] = useState("overview");
  const [custom, setCustom] = useState<string[]>(() => read(K.custom, []));
  const [scope, setScope] = useState<Scope>(() => read(K.scope, "library"));
  const [tab, setTab] = useState<Tab>(() => read(K.tab, "search"));
  const [question, setQuestion] = useState("non-flammable electrolyte");
  const [hits, setHits] = useState<Hit[]>([]);
  const [searchMeta, setSearchMeta] = useState<any>(null);
  const [evidenceId, setEvidenceId] = useState(() => read(K.evidence, ""));
  const [context, setContext] = useState<any>(null);
  const [source, setSource] = useState<any>(null);
  const [resource, setResource] = useState<any>(null);
  const [pack, setPack] = useState<any>(null);
  const [packClaims, setPackClaims] = useState("Non-flammable electrolyte enables Li-metal batteries with aggressive cathode chemistries.");
  const [claim, setClaim] = useState("Non-flammable electrolyte enables Li-metal batteries with aggressive cathode chemistries.");
  const [grounding, setGrounding] = useState<any>(null);
  const [workflowRun, setWorkflowRun] = useState<any>(null);
  const [pipeline, setPipeline] = useState<any>(() => read(K.task, null));
  const [task, setTask] = useState<Task | null>(null);
  const [showUpload, setShowUpload] = useState(false);
  const [showLogin, setShowLogin] = useState(false);
  const [authenticated, setAuthenticated] = useState(false);
  const [showInspector, setShowInspector] = useState(false);
  const [showDeveloper, setShowDeveloper] = useState(false);
  const [call, setCall] = useState<Call | null>(null);
  const [recent, setRecent] = useState<Call[]>([]);
  const [notice, setNotice] = useState("Ready. Workspace actions use the live backend.");
  const [busy, setBusy] = useState("");

  const api = useCallback(async (method: string, path: string, options: { body?: any; form?: FormData } = {}) => {
    const start = performance.now();
    const headers: Record<string, string> = {};
    if (token.trim()) headers.Authorization = `Bearer ${token.trim()}`;
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    const requested = options.body ?? (options.form ? Object.fromEntries(options.form.entries()) : undefined);
    try {
      const response = await fetch(`${base.replace(/\/$/, "")}${path}`, { method, headers, body: options.form ?? (options.body === undefined ? undefined : JSON.stringify(options.body)), credentials: "include" });
      const text = await response.text(); let result: any = text;
      try { result = text ? JSON.parse(text) : null; } catch { /* non-json body */ }
      const record: Call = { method, path, body: requested, response: result, status: response.status, latency: Math.round(performance.now() - start), requestId: response.headers.get("X-Request-ID"), at: new Date().toLocaleTimeString() };
      if (!response.ok) record.error = errorOf(result, response.status);
      setCall(record); setRecent((items) => [record, ...items].slice(0, 12));
      if (!response.ok) throw new Error(record.error);
      return result;
    } catch (error) {
      const record: Call = { method, path, body: requested, latency: Math.round(performance.now() - start), error: message(error), at: new Date().toLocaleTimeString() };
      setCall(record); setRecent((items) => [record, ...items].slice(0, 12));
      throw error;
    }
  }, [base, token]);

  const refreshSession = useCallback(async () => {
    try {
      const response = await fetch(`${base.replace(/\/$/, "")}/auth/session`, { credentials: "include" });
      const result = await response.json();
      setAuthenticated(Boolean(result.authenticated));
    } catch { setAuthenticated(false); }
  }, [base]);
  const login = async (loginToken: string) => {
    const response = await fetch(`${base.replace(/\/$/, "")}/auth/login`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: loginToken }), credentials: "include" });
    if (!response.ok) throw new Error("登录密钥无效。");
    setAuthenticated(true); setShowLogin(false); setNotice("登录成功，已获得完整访问权限。");
    void refreshHealth(); void refreshOverview(); void loadPapers(1);
  };
  const logout = async () => {
    await fetch(`${base.replace(/\/$/, "")}/auth/logout`, { method: "POST", credentials: "include" });
    setAuthenticated(false); setNotice("已退出登录。");
  };

  const refreshHealth = useCallback(async () => {
    try { setHealth(await api("GET", "/api/v1/system/health")); setHealthError(""); }
    catch (error) { setHealth(null); setHealthError(message(error)); }
  }, [api]);
  const refreshOverview = useCallback(async () => {
    try { setOverview(await api("GET", "/api/v1/library/overview")); }
    catch (error) { setNotice(`Library overview failed: ${message(error)}`); }
  }, [api]);
  const loadPapers = useCallback(async (requested = page) => {
    setLoading(true);
    const filters: Record<string, any> = {};
    const from = numberOrNull(yearFrom), to = numberOrNull(yearTo);
    if (from !== null || to !== null) filters.year = { gte: from, lte: to };
    if (ready) filters.ai_ready = ready === "true";
    try {
      if (catalogSource !== "local") {
        if (!query.trim()) { setPapers([]); setTotal(0); setLiteratureMeta(null); setNotice("远程搜索需要输入关键词。"); return; }
        const remoteSources = discoverySources.filter((item: any) => item.configured !== false).map((item: any) => item.source);
        const result = await api("POST", "/api/v1/literature/search", { body: { query: query.trim(), search_mode: "hybrid", sources: catalogSource === "all" ? ["local", ...remoteSources] : [catalogSource], filters: { year_from: from, year_to: to }, page_size: 20, cursor: null } });
        const items = (result.items || []).map((hit: any) => ({ ...(hit.local_paper || {}), ...hit.metadata, paper_id: hit.local_paper?.paper_id || hit.identity_key, _literature: hit }));
        setPapers(items); setTotal(items.length); setPage(1); setLiteratureMeta(result);
        if (result.partial) setNotice("远程来源发生降级；当前结果仍包含可用的本地数据或缓存。");
      } else {
        const result = await api("POST", "/api/v1/papers/search", { body: { query: query.trim() || null, filters, sort: { field: "updated_at", order: "desc" }, page: requested, page_size: 20 } });
        setPapers(result.items || []); setTotal(result.pagination?.total || 0); setPage(requested); setLiteratureMeta(null);
      }
    } catch (error) { setNotice(`Library load failed: ${message(error)}`); }
    finally { setLoading(false); }
  }, [api, catalogSource, discoverySources, page, query, ready, yearFrom, yearTo]);
  const importLiterature = async (item: Paper) => {
    const hit = item._literature;
    const preferred = importSources[hit?.identity_key] || "";
    const options = hit?.remote_options || (hit?.remote ? [hit.remote] : []);
    const remote = preferred ? options.find((option: any) => option.source === preferred) : hit?.remote;
    if (!remote || !remote.downloadable) return;
    setBusy(`import:${remote.external_id}`);
    try {
      let batch = await api("POST", "/api/v1/literature/imports", { body: { items: [{ options: options.map((option: any) => ({ source: option.source, external_id: option.external_id })), preferred_source: preferred || null }] } });
      setNotice(`已提交远程导入 ${batch.batch_id}。`);
      for (let attempt = 0; attempt < 180 && !["succeeded", "partial_failed", "failed", "completed", "completed_with_errors", "cancelled"].includes(batch.status); attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
        batch = await api("GET", `/api/v1/import-batches/${batch.batch_id}`);
      }
      const imported = batch.items?.[0];
      setNotice(["succeeded", "completed"].includes(batch.status) ? `论文已导入并完成处理：${imported?.paper_id || remote.external_id}` : `导入结束：${batch.status}${imported?.error_message ? ` · ${imported.error_message}` : ""}`);
      await loadPapers(1);
    } catch (error) { setNotice(`Remote import failed: ${message(error)}`); }
    finally { setBusy(""); }
  };
  const openPaper = useCallback(async (item: Paper, nextTab = "overview") => {
    setPaper(item); setDrawerTab(nextTab);
    try {
      const [detail, body, assetResponse] = await Promise.all([
        api("GET", `/api/v1/papers/${item.paper_id}`),
        api("GET", `/api/v1/papers/${item.paper_id}/content?offset=0&limit=6000`),
        api("GET", `/api/v1/papers/${item.paper_id}/assets?page=1&page_size=48`),
      ]);
      setPaper(detail); setContent(body); setAssets(assetResponse.items || []);
    } catch (error) { setNotice(`Paper detail failed: ${message(error)}`); }
  }, [api]);

  useEffect(() => { void refreshHealth(); void refreshOverview(); }, [refreshHealth, refreshOverview]);
  useEffect(() => { void refreshSession(); }, [refreshSession]);
  useEffect(() => { void api("GET", "/api/v1/discovery/sources").then((result) => setDiscoverySources((result.items || []).filter((item: any) => item.role === "search_and_resolve"))).catch(() => undefined); }, [api]);
  useEffect(() => { void loadPapers(1); }, [base]); // load after API host changes
  useEffect(() => {
    const refresh = () => {
      if (document.hidden) return;
      void refreshHealth(); void refreshOverview(); if (catalogSource === "local") void loadPapers(page);
    };
    const id = window.setInterval(refresh, 15000);
    return () => window.clearInterval(id);
  }, [catalogSource, loadPapers, page, refreshHealth, refreshOverview]);
  useEffect(() => { localStorage.setItem(K.base, JSON.stringify(base)); }, [base]);
  useEffect(() => { localStorage.setItem(K.debug, JSON.stringify(debug)); }, [debug]);
  useEffect(() => { localStorage.setItem(K.scope, JSON.stringify(scope)); }, [scope]);
  useEffect(() => { localStorage.setItem(K.custom, JSON.stringify(custom)); }, [custom]);
  useEffect(() => { localStorage.setItem(K.tab, JSON.stringify(tab)); }, [tab]);
  useEffect(() => { localStorage.setItem(K.evidence, JSON.stringify(evidenceId)); }, [evidenceId]);
  useEffect(() => { if (pipeline) localStorage.setItem(K.task, JSON.stringify(pipeline)); else localStorage.removeItem(K.task); }, [pipeline]);
  useEffect(() => {
    if (!pipeline?.taskId) return;
    let stopped = false;
    const poll = async () => {
      try {
        let latest = await api("GET", `/api/v1/tasks/${pipeline.taskId}`);
        if (["queued", "running"].includes(latest.status)) latest = await api("POST", `/api/v1/tasks/${pipeline.taskId}/refresh`, { body: {} });
        if (stopped) return;
        setTask(latest);
        if (latest.paper_id && !pipeline.paperId) {
          setPipeline((current: any) => current ? { ...current, paperId: latest.paper_id } : current);
          const detail = await api("GET", `/api/v1/papers/${latest.paper_id}`);
          if (!stopped) void openPaper(detail, "pipeline");
        }
        if (["succeeded", "completed"].includes(latest.status)) { void loadPapers(1); void refreshOverview(); }
      } catch (error) { if (!stopped) setNotice(`Pipeline monitoring failed: ${message(error)}`); }
    };
    void poll(); const id = window.setInterval(() => void poll(), 7000);
    return () => { stopped = true; window.clearInterval(id); };
  }, [api, loadPapers, openPaper, pipeline, refreshOverview]);

  const toggleCustom = (paperId: string) => setCustom((ids) => ids.includes(paperId) ? ids.filter((id) => id !== paperId) : [...ids, paperId]);
  const scopeIds = useMemo(() => scope === "selected" ? (paper ? [paper.paper_id] : []) : scope === "custom" ? custom : [], [custom, paper, scope]);
  const upload = async (file: File, title: string) => {
    setBusy("upload");
    try {
      const form = new FormData(); form.set("file", file); if (title.trim()) form.set("title", title.trim());
      const ingested = await api("POST", "/api/v1/papers", { form });
      const processing = ingested.task;
      setPipeline({ taskId: processing.task_id, batchId: ingested.batch_id, itemId: ingested.item_id, operation: processing.operation }); setTask(processing); setShowUpload(false);
      setNotice(`Uploaded ${file.name}; registration and processing have started.`);
    } catch (error) { setNotice(`Upload failed: ${message(error)}`); }
    finally { setBusy(""); }
  };
  const searchEvidence = async () => {
    if (!question.trim()) return setNotice("Enter an evidence question.");
    if (scope !== "library" && !scopeIds.length) return setNotice("The selected scope has no papers. Select papers in Library first.");
    setBusy("search");
    try {
      const scope = scopeIds.length ? { paper_ids: scopeIds } : {};
      const result = await api("POST", "/api/v1/evidence/search", { body: { query: question.trim(), scope, filters: {}, top_k: 20, page: 1, page_size: 20 } });
      setHits(result.items || result.evidence || []); setSearchMeta(result); setTab("search"); setNotice(`${(result.items || result.evidence || []).length} live evidence hits returned.`);
    } catch (error) { setNotice(`Evidence search failed: ${message(error)}`); }
    finally { setBusy(""); }
  };
  const runContext = async (id = evidenceId) => { const target = id.trim(); if (!target) return setNotice("Enter an evidence_id or choose Context from a search hit."); setEvidenceId(target); setBusy("context"); try { setContext(await api("GET", `/api/v1/evidence/${target}/content?before=1200&after=2400`)); setTab("context"); } catch (error) { setNotice(`Context failed: ${message(error)}`); } finally { setBusy(""); } };
  const runSource = async (id = evidenceId) => { const target = id.trim(); if (!target) return setNotice("Enter an evidence_id or choose PDF source from a search hit."); setEvidenceId(target); setBusy("source"); try { setSource(await api("GET", `/api/v1/evidence/${target}/source`)); setTab("source"); } catch (error) { setNotice(`Source failed: ${message(error)}`); } finally { setBusy(""); } };
  const openContext = async (hit: Hit) => runContext(hit.evidence_id);
  const openSource = async (hit: Hit) => runSource(hit.evidence_id);
  const openResource = async (id: string) => { setBusy("resource"); try { setResource(await api("GET", `/api/v1/assets/${id}`)); setTab("resource"); } catch (error) { setNotice(`Resource failed: ${message(error)}`); } finally { setBusy(""); } };
  const waitWorkflow = async (runId: string) => { for (let attempt = 0; attempt < 180; attempt += 1) { const run = await api("GET", `/api/v1/workflows/${runId}`); setWorkflowRun(run); if (run.status === "succeeded") return run.result; if (run.status === "failed") throw new Error(run.error_message || "Workflow failed."); await new Promise((resolve) => window.setTimeout(resolve, 1000)); } throw new Error("Workflow timed out while waiting for a result."); };
  const buildPack = async () => { const claims = packClaims.split(/\r?\n/).map((text) => text.trim()).filter(Boolean); if (!claims.length) return setNotice("Enter one explicit claim per line before building a pack."); if (scope !== "library" && !scopeIds.length) return setNotice("The selected scope has no papers. Select papers in Library first."); setBusy("pack"); try { const run = await api("POST", "/api/v1/evidence/packs", { body: { claims: claims.map((text) => ({ text })), scope: { paper_ids: scopeIds } } }); setWorkflowRun(run); setPack(await waitWorkflow(run.run_id)); setTab("pack"); } catch (error) { setNotice(`Evidence Pack failed: ${message(error)}`); } finally { setBusy(""); } };
  const groundClaim = async () => { if (!claim.trim()) return setNotice("Enter a draft before grounding."); if (scope !== "library" && !scopeIds.length) return setNotice("The selected scope has no papers. Select papers in Library first."); setBusy("grounding"); try { const run = await api("POST", "/api/v1/citations/ground", { body: { draft: claim.trim(), scope: { paper_ids: scopeIds } } }); setWorkflowRun(run); setGrounding(await waitWorkflow(run.run_id)); setTab("grounding"); } catch (error) { setNotice(`Citation grounding failed: ${message(error)}`); } finally { setBusy(""); } };
  const loadWorkflowSample = () => { const target = paper || papers.find((item) => item.capabilities?.can_search_evidence || item.processing_status?.embedding === "ready"); if (!target) return setNotice("No searchable paper is loaded. Build evidence and embeddings for a paper first."); const sample = "Non-flammable electrolyte enables Li-metal batteries with aggressive cathode chemistries."; setPackClaims(sample); setClaim(sample); setCustom([target.paper_id]); setScope("custom"); setNotice(`Loaded a workflow sample scoped to ${target.title || target.paper_id}.`); };

  const props: any = { base, setBase, token, setToken, debug, setDebug, health, healthError, refreshHealth, overview, refreshOverview, papers, total, page, query, setQuery, yearFrom, setYearFrom, yearTo, setYearTo, ready, setReady, catalogSource, setCatalogSource, discoverySources, literatureMeta, importLiterature, importSources, setImportSources, loading, loadPapers, paper, setPaper, content, assets, drawerTab, setDrawerTab, openPaper, custom, toggleCustom, showUpload, setShowUpload, upload, pipeline, setPipeline, task, scope, setScope, scopeIds, tab, setTab, question, setQuestion, searchEvidence, hits, searchMeta, context, source, resource, pack, packClaims, setPackClaims, claim, setClaim, grounding, workflowRun, evidenceId, setEvidenceId, runContext, runSource, openContext, openSource, openResource, buildPack, groundClaim, loadWorkflowSample, busy, call, recent, notice, showInspector, setShowInspector, showDeveloper, setShowDeveloper, authenticated, setShowLogin, logout, api };
  return <div className="app-shell"><Sidebar view={view} setView={setView} developer={() => setShowDeveloper(true)} admin={() => setView("admin")} /><main className="workspace"><Topbar {...props} /><div className="noticebar">{notice}</div>{view === "dashboard" && <Dashboard {...props} />}{view === "library" && <Library {...props} />}{view === "studio" && <Studio {...props} />}{view === "harvest" && <Suspense fallback={<div className="page muted">正在加载采集工作台…</div>}><HarvestView base={base} token={token} /></Suspense>}{view === "admin" && <Suspense fallback={<div className="page muted">Loading admin workspace...</div>}><AdminView base={base} /></Suspense>}{view === "reference" && <Reference {...props} />}</main>{showInspector && <Inspector call={call} recent={recent} debug={debug} close={() => setShowInspector(false)} />}{paper && <PaperDrawer {...props} close={() => setPaper(null)} />}{showUpload && <UploadModal close={() => setShowUpload(false)} submit={upload} busy={busy === "upload"} />}{showLogin && <LoginModal close={() => setShowLogin(false)} login={login} />}{showDeveloper && <Developer {...props} close={() => setShowDeveloper(false)} />}</div>;
}

function Sidebar({ view, setView, developer, admin }: { view: View; setView: (view: View) => void; developer: () => void; admin: () => void }) {
  const links: Array<[View, string, string]> = [["library", "▤", "论文库"], ["harvest", "⇣", "批量采集"], ["studio", "⌁", "证据工作台"], ["dashboard", "◈", "工作概览"]];
  return <aside className="sidebar"><div className="brand"><span>L</span><div><strong>Literature KB</strong><small>Research workspace</small></div></div><nav>{links.map(([id, icon, text]) => <button key={id} className={view === id ? "nav active" : "nav"} onClick={() => setView(id)}><i>{icon}</i><b>{text}</b></button>)}</nav><div className="sidebar-bottom"><a className="developer-link guide-link" href="./docs/" target="_blank" rel="noreferrer">使用指南 ↗</a><button className="developer-link" onClick={developer}>开发者工具</button><button className="admin-entry" onClick={admin}>管理控制台</button></div></aside>;
}
function Topbar(p: any) { const healthy = p.health?.status === "healthy"; return <header className="topbar"><div><p className="eyebrow">RESEARCH WORKSPACE</p><h1>文献与证据</h1></div><div className="top-controls"><button className={healthy ? "health healthy" : "health bad"} onClick={p.refreshHealth}><i />{healthy ? "服务正常" : "服务不可用"}</button><button className="secondary" onClick={p.authenticated ? p.logout : () => p.setShowLogin(true)}>{p.authenticated ? "退出登录" : "登录"}</button><button className="icon" title="打开开发者检查器" onClick={() => p.setShowInspector(!p.showInspector)}>◫</button></div></header>; }
function Dashboard(p: any) { const active = p.overview?.active_task; const progress = active?.progress == null ? "—" : `${Math.round(active.progress * 100)}%`; return <section className="page"><Heading eyebrow="OPERATIONS OVERVIEW" title="Library readiness and service health" text="Live data from the current backend. No mock papers or mock evidence are rendered." action={<div className="heading-actions"><a className="secondary docs-shortcut" href="./docs/" target="_blank" rel="noreferrer">Integration guide ↗</a><button className="primary" onClick={() => p.setShowUpload(true)}>Upload PDF</button></div>} /><div className="metrics"><Metric title="Papers in catalog" value={String(p.overview?.papers ?? p.total)} note="Whole library" /><Metric title="Searchable" value={String(p.overview?.searchable ?? "—")} note="Whole-library evidence ready" /><Metric title="AI ready" value={String(p.overview?.ai_ready ?? "—")} note="Whole-library embedding ready" /><Metric title="Active pipeline" value={progress} note={active ? `${active.operation || "task"} · ${active.status}` : "No active task"} /></div><div className="dashboard-grid"><section className="surface"><SectionTitle eyebrow="DEPENDENCIES" title="Health monitor" action={<button className="text" onClick={() => { p.refreshHealth(); p.refreshOverview(); }}>Refresh</button>} />{p.healthError && <p className="error">{p.healthError}</p>}<div className="health-list">{(p.health?.components || []).map((item: any) => <div className="health-row" key={item.name}><i className={`dot ${item.status}`} /><b>{item.name}</b><span>{formatDetail(item.detail ?? item.status)}</span><small>{item.latency_ms ?? 0} ms</small></div>)}{!p.health?.components?.length && <Empty text="Health response is not available." />}</div></section><section className="surface"><SectionTitle eyebrow="PIPELINE" title="Current active task" /><Pipeline task={active} paper={null} /><p className="muted">Browser-tracked task: {p.task?.task_id || "none"}</p></section><section className="surface guide-surface"><SectionTitle eyebrow="AGENT HANDOFF" title="Integration guide" action={<a className="text-link" href="./docs/" target="_blank" rel="noreferrer">Open docs ↗</a>} /><p className="guide-copy">前端主界面已经挂上单独的接入指南页，给 agent 组同学梳理“发现论文 → 检索 evidence → 原文核验 → Pack / Ground”的推荐路径。</p><div className="guide-badges"><span>Retrieval-first</span><span>Task ≠ Workflow</span><span>Search hit ≠ cite-ready</span></div><ul className="guide-list"><li>如果要先联调最短链路，优先看 evidence search / content / source。</li><li>如果要产出引用结果，再进入 Evidence Pack 或 Citation Grounding。</li></ul><div className="guide-actions"><a className="secondary" href="./docs/" target="_blank" rel="noreferrer">View guide</a><button className="text" onClick={() => p.setShowDeveloper(true)}>Live endpoint tester</button></div></section><section className="surface capability"><SectionTitle eyebrow="AGENT CHAIN" title="Live capability flow" /><div className="chain">{["Paper search", "Evidence search", "Context", "Evidence pack", "Citation grounding", "PDF / asset"].map((text, index) => <div key={text}><span>{String(index + 1).padStart(2, "0")}</span><b>{text}</b><small>{index > 2 && index < 5 ? "Whole library contract" : "Live endpoint"}</small></div>)}</div></section><section className="surface recent"><SectionTitle eyebrow="RECENT REQUESTS" title="API activity" />{p.recent.length ? p.recent.slice(0, 7).map((item: Call, index: number) => <div className="request" key={`${item.at}-${index}`}><Status text={String(item.status || "ERR")} tone={item.error ? "danger" : "success"} /><b>{item.method}</b><code>{item.path}</code><small>{item.latency} ms · {item.at}</small></div>) : <Empty text="Calls will appear here as you work." />}</section></div></section>; }
function Library(p: any) {
  const remoteMode = p.catalogSource !== "local";
  return <section className="page">
    <Heading eyebrow="CATALOG AND INGESTION" title="Paper Library" text="同时检索本地、OpenAlex、CORE 与 arXiv；远程论文确认后进入统一处理流程。" action={<button className="primary" onClick={() => p.setShowUpload(true)}>+ Upload PDF</button>} />
    <div className="library-tools"><div className="search"><span>⌕</span><input value={p.query} onChange={(event) => p.setQuery(event.target.value)} onKeyDown={(event) => event.key === "Enter" && p.loadPapers(1)} placeholder="Search title, abstract, venue or DOI" /><button onClick={() => p.loadPapers(1)}>Search</button></div>
      <div className="filters"><select value={p.catalogSource} onChange={(event) => p.setCatalogSource(event.target.value)}><option value="local">本地</option><option value="all">全部</option>{p.discoverySources.map((source: any) => <option key={source.source} value={source.source} disabled={source.configured === false}>{source.source === "openalex" ? "OpenAlex" : source.source === "arxiv" ? "arXiv" : source.source.toUpperCase()}{source.configured === false ? "（未配置）" : ""}</option>)}</select><input value={p.yearFrom} onChange={(event) => p.setYearFrom(event.target.value)} placeholder="Year from" /><input value={p.yearTo} onChange={(event) => p.setYearTo(event.target.value)} placeholder="Year to" />{!remoteMode && <select value={p.ready} onChange={(event) => p.setReady(event.target.value)}><option value="">All readiness</option><option value="true">AI ready</option><option value="false">Not AI ready</option></select>}<button className="secondary" onClick={() => { p.setQuery(""); p.setYearFrom(""); p.setYearTo(""); p.setReady(""); window.setTimeout(() => p.loadPapers(1)); }}>Reset</button></div>
    </div>
    {p.literatureMeta?.source_status && <div className="library-summary">{p.literatureMeta.source_status.map((source: any) => <span key={source.source}><Status text={`${source.source}: ${source.status}`} /> {source.latency_ms} ms</span>)}</div>}
    <div className="library-summary"><b>{p.total} papers</b><span>{p.custom.length} in custom Agent scope</span><small>{remoteMode ? "来源互相隔离；自动导入会在来源失效时回退。" : "Select a row checkbox to build a Studio scope."}</small></div>
    <section className="paper-list">{p.loading && <div className="loader" />}{p.papers.map((item: Paper) => {
      const hit = item._literature; const isLocal = !hit || Boolean(hit.local_paper); const selected = p.importSources[hit?.identity_key] || ""; const selectedRemote = selected ? hit.remote_options?.find((option: any) => option.source === selected) : hit?.remote;
      return <article className="paper-row" key={item.paper_id}><label className="check"><input type="checkbox" disabled={!isLocal} checked={isLocal && p.custom.includes(item.paper_id)} onChange={() => isLocal && p.toggleCustom(item.paper_id)} /><span /></label><button className="paper-main" disabled={!isLocal} onClick={() => isLocal && p.openPaper(item)}><h3>{item.title || "Untitled paper"} {item.year && <em>{item.year}</em>}</h3><p>{[item.authors?.slice(0, 3).map((author: any) => author.name).filter(Boolean).join(", "), item.venue, item.doi].filter(Boolean).join(" · ") || item.paper_id}</p><small>{item.abstract ? clip(item.abstract, 210) : "Metadata extraction has not completed yet."}</small></button><div className="paper-state">{hit ? <><Status text={hit.availability} /><Status text={selectedRemote?.license_policy || "local"} /><small>{hit.origins?.map((origin: any) => origin.source).join(" + ")}{hit.possible_duplicate ? " · possible duplicate" : ""}</small>{!isLocal && <select value={selected} onChange={(event) => p.setImportSources((current: Record<string, string>) => ({ ...current, [hit.identity_key]: event.target.value }))}><option value="">自动选择（推荐）</option>{(hit.remote_options || []).map((option: any) => <option key={`${option.source}:${option.external_id}`} value={option.source} disabled={!option.downloadable}>{option.source === "arxiv" ? "arXiv" : option.source === "openalex" ? "OpenAlex" : option.source.toUpperCase()}{!option.downloadable ? "（不可用）" : ""}</option>)}</select>}</> : <><Status text={item.processing_status?.parse || "not started"} /><Status text={item.capabilities?.ai_ready ? "AI ready" : item.capabilities?.can_search_evidence ? "Evidence ready" : "not indexed"} tone={item.capabilities?.ai_ready ? "success" : undefined} /><small>{resourceCount(item.asset_counts)} resources</small></>}</div>{isLocal ? <button className="row-action" onClick={() => p.openPaper(item, "assets")}>Inspect →</button> : <button className="row-action" disabled={!selectedRemote?.downloadable || p.busy === `import:${selectedRemote?.external_id}`} onClick={() => p.importLiterature(item)}>{p.busy === `import:${selectedRemote?.external_id}` ? "Importing…" : selectedRemote?.downloadable ? "Import →" : "Unavailable"}</button>}</article>;
    })}{!p.loading && !p.papers.length && <Empty text="No papers match the current query." />}</section>
    {!remoteMode && <div className="pagination"><button disabled={p.page <= 1} onClick={() => p.loadPapers(p.page - 1)}>Previous</button><span>Page {p.page} · {p.total} papers</span><button disabled={p.papers.length < 20} onClick={() => p.loadPapers(p.page + 1)}>Next</button></div>}
  </section>;
}
function Studio(p: any) {
  const tabs: Array<[Tab, string]> = [["search", "Search"], ["context", "Context"], ["source", "PDF Source"], ["resource", "Resource"], ["pack", "Evidence Pack"], ["grounding", "Citation Grounding"]];
  const scopeText = p.scope === "library" ? "Search sends no paper filter." : p.scope === "selected" ? (p.paper ? `Filtering to ${p.paper.title || p.paper.paper_id}.` : "Select a paper in Library first.") : p.scopeIds.length ? `Filtering to ${p.scopeIds.length} selected paper(s).` : "Choose papers with Library checkboxes.";
  return <section className="page"><Heading eyebrow="AGENT INTERFACE" title="Agent Studio" text="Run live evidence retrieval and citation endpoints, then follow each result back to context, source PDF and resources." /><section className="studio-controls"><div><label className="control-label">Search scope</label><div className="segment">{(["library", "selected", "custom"] as Scope[]).map((item) => <button key={item} className={p.scope === item ? "active" : ""} onClick={() => p.setScope(item)}>{item === "library" ? "Whole library" : item === "selected" ? "Selected paper" : `Custom papers (${p.custom.length})`}</button>)}</div><small>{scopeText}</small><button className="secondary" onClick={p.loadWorkflowSample}>Load real workflow sample</button></div><div className="question"><label>Evidence question<input value={p.question} onChange={(event) => p.setQuestion(event.target.value)} onKeyDown={(event) => event.key === "Enter" && p.searchEvidence()} /></label><button className="primary" disabled={p.busy === "search"} onClick={p.searchEvidence}>{p.busy === "search" ? "Searching…" : "Search evidence"}</button></div></section><div className="tabs">{tabs.map(([id, label]) => <button key={id} className={p.tab === id ? "active" : ""} onClick={() => p.setTab(id)}>{label}</button>)}</div><div className="studio-stage">{p.tab === "search" && <SearchResults {...p} />}{p.tab === "context" && <ContextPanel context={p.context} evidenceId={p.evidenceId} setEvidenceId={p.setEvidenceId} runContext={p.runContext} runSource={p.runSource} busy={p.busy} />}{p.tab === "source" && <SourcePanel base={p.base} token={p.token} source={p.source} evidenceId={p.evidenceId} setEvidenceId={p.setEvidenceId} runSource={p.runSource} busy={p.busy} />}{p.tab === "resource" && <ResourcePanel base={p.base} token={p.token} resource={p.resource} />}{p.tab === "pack" && <PackPanel {...p} />}{p.tab === "grounding" && <GroundingPanel {...p} />}</div></section>;
}
function SearchResults(p: any) { return <div className="result-layout"><section className="surface results"><SectionTitle eyebrow="RETRIEVAL RESULTS" title={p.hits.length ? `${p.hits.length} evidence hits` : "Search the evidence index"} action={p.searchMeta && <small className="muted">Live backend response</small>} />{p.hits.map((hit: Hit) => <article className="hit" key={hit.evidence_id}><div className="hit-top"><div><h4>{hit.paper?.title || hit.paper?.paper_id || "Unknown paper"}</h4><p>{[hit.location?.section_title, hit.location?.section_type, hit.location?.page && `p. ${hit.location.page}`].filter(Boolean).join(" · ")}</p></div>{typeof hit.score === "number" && <b className="score">{hit.score.toFixed(3)}</b>}</div><blockquote>{hit.content?.snippet || hit.content?.quote || "No snippet was returned."}</blockquote><div className="hit-actions"><button onClick={() => p.openContext(hit)}>Context</button><button onClick={() => p.openSource(hit)}>PDF source</button>{hit.assets?.[0] && <button onClick={() => p.openResource(hit.assets?.[0]?.asset_id)}>Resource</button>}<code>{hit.evidence_id}</code></div>{p.debug !== "off" && <details><summary>Retrieval details</summary><pre>{pretty({ score: hit.score ?? "Not returned by backend", representation: hit.representation ?? "Not returned by backend", location: hit.location, assets: hit.assets, raw: p.debug === "verbose" ? hit : undefined })}</pre></details>}</article>)}{!p.hits.length && <div className="empty"><b>{p.searchMeta ? "No evidence matched this live request." : "Enter a question and run a live evidence search."}</b>{p.searchMeta && <span>Semantic embeddings are not built for this paper yet, so the backend currently falls back to strict keyword matching. Try <button className="text" onClick={() => { p.setQuestion("non-flammable electrolyte"); window.setTimeout(() => p.searchEvidence()); }}>non-flammable electrolyte</button>, or build the embedding index in Paper Library → Pipeline.</span>}</div>}</section><aside className="scope-note"><p className="eyebrow">SCOPE CONTRACT</p><h3>{p.scope === "library" ? "Whole library" : p.scope === "selected" ? "Selected paper" : "Custom papers"}</h3><p>Search, Evidence Pack, and Citation Grounding all send this choice as <code>scope.paper_ids</code>.</p><hr /><p>Workflow results retain the selected scope and can be polled with their <code>run_id</code>.</p></aside></div>; }
function EvidenceLookup({ evidenceId, setEvidenceId, runContext, runSource, busy }: any) { return <div className="direct-runner"><label>evidence_id<input value={evidenceId} onChange={(event) => setEvidenceId(event.target.value)} placeholder="paper_xxx_p1_b8" /></label><button className="secondary" disabled={busy === "context"} onClick={() => runContext()}>{busy === "context" ? "Loading…" : "Run Content"}</button><button className="secondary" disabled={busy === "source"} onClick={() => runSource()}>{busy === "source" ? "Loading…" : "Run Source"}</button></div>; }
function ContextPanel(p: any) { const context = p.context; const current = context?.current || context; const windowText = context?.content?.text || context?.context_text || current?.content?.text || current?.quote || ""; const location = context?.location || current?.location; return <section className="surface detail-panel"><SectionTitle eyebrow="EVIDENCE CONTENT" title={context ? (context.paper?.title || current.paper?.title || current.evidence_id || "Current evidence") : "Run content by evidence_id"} /><EvidenceLookup {...p} />{!context ? <Empty text="Run Content with an evidence_id, or choose Context from a search hit." /> : <><p className="muted">This is the real v1 context window around the selected evidence{location?.page ? ` · page ${location.page}` : ""}.</p><article className="paper-content context-window">{windowText || "The backend did not return a content window."}</article><div className="context-columns"><article><h4>Evidence range</h4><p>{context.content?.evidence_char_start != null ? `${context.content.evidence_char_start}–${context.content.evidence_char_end}` : "Not returned by backend"}</p></article><article><h4>Window range</h4><p>{context.content?.window_char_start != null ? `${context.content.window_char_start}–${context.content.window_char_end}` : "Not returned by backend"}</p></article></div><details><summary>Raw context response</summary><pre>{pretty(context)}</pre></details></>}</section>; }
function SourcePanel(p: any) { const { base, token, source } = p; const citation = source?.citation || source; return <section className="surface detail-panel"><SectionTitle eyebrow="PDF SOURCE" title={source ? (citation.paper?.title || source.paper?.title || citation.evidence_id || "Evidence source") : "Run source by evidence_id"} /><EvidenceLookup {...p} />{!source ? <Empty text="Run Source with an evidence_id, or choose PDF source from a search hit." /> : <><p className="muted">Original PDF for evidence {citation.evidence_id}{citation.location?.page ? ` · page ${citation.location.page}` : ""}.</p>{citation.quote && <blockquote className="focus">{clip(citation.quote, 460)}</blockquote>}<PdfCanvas base={base} token={token} source={source} /><details><summary>Source metadata</summary><pre>{pretty(source)}</pre></details></>}</section>; }
function ResourcePanel({ base, token, resource }: { base: string; token: string; resource: any }) { if (!resource) return <Empty text="Choose Resource from an evidence hit or Paper Library asset." />; const asset = resource.asset || resource; const url = `${base.replace(/\/$/, "")}/api/v1/assets/${asset.asset_id}/download`; return <section className="surface detail-panel"><SectionTitle eyebrow="RESOURCE ASSET" title={`${asset.asset_type || "asset"} · ${asset.asset_id}`} action={<a className="secondary" href={url} target="_blank" rel="noreferrer">Download / open</a>} />{asset.mime_type?.startsWith("image/") ? <ImagePreview url={url} token={token} alt={asset.asset_id} /> : asset.content_text ? <pre className="asset-text">{asset.content_text}</pre> : <p className="muted">Use Download / open to retrieve this resource from the live backend.</p>}<details><summary>Raw resource response</summary><pre>{pretty(resource)}</pre></details></section>; }
function WorkflowStatus({ run }: { run: any }) { return run ? <p className="muted">Run <code>{run.run_id}</code> · <Status text={run.status} /> · {run.details?.stage || "queued"}{run.error_message ? ` · ${run.error_message}` : ""}</p> : null; }
function PackPanel(p: any) { return <section className="surface detail-panel"><SectionTitle eyebrow="EVIDENCE PACK" title="Build a reusable evidence pack" action={<button className="primary" disabled={p.busy === "pack"} onClick={p.buildPack}>{p.busy === "pack" ? "Building…" : "Build pack"}</button>} /><p className="muted">One explicit, independently verifiable claim per line. Scope: {p.scopeIds.length ? `${p.scopeIds.length} selected paper(s)` : "whole library"}.</p><textarea rows={4} value={p.packClaims} onChange={(event) => p.setPackClaims(event.target.value)} placeholder="One claim per line" /><WorkflowStatus run={p.workflowRun} />{p.pack ? <div className="pack-list">{p.pack.claims?.flatMap((claim: any) => claim.evidence?.map((item: any) => <article key={`${claim.claim_id}-${item.evidence_id}`}><div><Status text={item.verdict} /><b>{typeof item.confidence === "number" ? item.confidence.toFixed(3) : "—"}</b></div><p>{item.quote || "No quote"}</p><small>{claim.claim} · {item.paper?.title || item.evidence_id}</small></article>))}</div> : <Empty text="Enter explicit claims, then build a reusable evidence pack." />}</section>; }
function GroundingPanel(p: any) { return <section className="surface detail-panel"><SectionTitle eyebrow="CITATION GROUNDING" title="Ground a draft with citations" action={<button className="primary" disabled={p.busy === "grounding" || !p.claim.trim()} onClick={p.groundClaim}>{p.busy === "grounding" ? "Grounding…" : "Ground draft"}</button>} /><p className="muted">The backend fixes sentence spans; the workflow verifies selected spans and only adds citation markers. Scope: {p.scopeIds.length ? `${p.scopeIds.length} selected paper(s)` : "whole library"}.</p><textarea rows={5} value={p.claim} onChange={(event) => p.setClaim(event.target.value)} /><WorkflowStatus run={p.workflowRun} />{p.grounding ? <div className="ground-list"><article><p>{p.grounding.grounded_answer}</p></article>{p.grounding.claims?.map((item: any) => <article key={item.claim_id}><div><Status text={item.status} /></div><p>{item.evidence?.[0]?.quote || "No supporting citation returned."}</p><small>{item.claim}</small></article>)}{p.grounding.unverified_claims?.map((item: any) => <article key={`unverified-${item.claim_id}`}><div><Status text="unverified" /></div><small>{item.claim} · {item.reason}</small></article>)}{p.grounding.contradictory_evidence?.map((item: any) => <article key={`contradiction-${item.claim_id}`}><div><Status text="contradicted" /></div><small>{item.claim} · conflicting evidence retained outside the citation list</small></article>)}</div> : <Empty text="Enter a draft to ground it against the current library scope." />}</section>; }

function PaperDrawer(p: any) { const paper = p.paper as Paper; const currentTask = p.task?.paper_id === paper.paper_id ? p.task : null; return <div className="drawer-layer"><aside className="paper-drawer"><header><div><p className="eyebrow">PAPER DETAIL</p><h2>{paper.title || paper.paper_id}</h2><p>{[paper.venue, paper.year, paper.doi].filter(Boolean).join(" · ")}</p></div><button className="icon" onClick={p.close}>×</button></header><div className="drawer-tabs">{["overview", "content", "assets", "pipeline"].map((item) => <button key={item} className={p.drawerTab === item ? "active" : ""} onClick={() => p.setDrawerTab(item)}>{item}</button>)}</div>{p.drawerTab === "overview" && <div className="drawer-body"><p>{paper.abstract || "Metadata extraction has not completed."}</p><dl><div><dt>Paper ID</dt><dd>{paper.paper_id}</dd></div><div><dt>DOI</dt><dd>{paper.doi || "—"}</dd></div><div><dt>Authors</dt><dd>{paper.authors?.map((author: any) => author.name).filter(Boolean).join(", ") || "—"}</dd></div></dl><div className="status-grid">{Object.entries(paper.processing_status || {}).map(([key, value]) => <div key={key}><small>{key}</small><Status text={String(value)} /></div>)}</div></div>}{p.drawerTab === "content" && <div className="drawer-body"><p className="muted">First 6,000 characters from the real content endpoint.</p><article className="paper-content">{p.content?.content?.text || p.content?.text || "Content is not ready."}</article></div>}{p.drawerTab === "assets" && <div className="drawer-body"><div className="asset-grid">{p.assets.map((asset: any) => <button key={asset.asset_id} onClick={() => p.openResource(asset.asset_id)}><span>{asset.asset_type}</span><b>{asset.location?.page ? `Page ${asset.location.page}` : asset.mime_type || "resource"}</b><small>{asset.asset_id}</small></button>)}</div>{!p.assets.length && <Empty text="No assets were returned." />}</div>}{p.drawerTab === "pipeline" && <div className="drawer-body">{currentTask ? <Pipeline task={currentTask} paper={paper} /> : <Empty text="Processing is automatic. Failed or interrupted processing can be retried by an administrator." />}<p className="muted">Figures, tables and formulas use MinerU captions and surrounding text only. No Vision or structured model is called.</p></div>}</aside></div>; }
function UploadModal({ close, submit, busy }: { close: () => void; submit: (file: File, title: string) => void; busy: boolean }) { const [file, setFile] = useState<File | null>(null); const [title, setTitle] = useState(""); const [error, setError] = useState(""); const send = () => { if (!file) return setError("Choose a PDF first."); if (file.type !== "application/pdf" && !file.name.toLowerCase().endsWith(".pdf")) return setError("Only PDF files are accepted."); submit(file, title); }; return <div className="modal-layer"><section className="upload-modal"><header><div><p className="eyebrow">INGEST INTO LIBRARY</p><h2>Upload a paper</h2><p>One click uploads the PDF and starts the complete automatic processing pipeline.</p></div><button className="icon" onClick={close}>×</button></header><label className="file-drop"><input type="file" accept="application/pdf,.pdf" onChange={(event) => { setFile(event.target.files?.[0] || null); setError(""); }} /><b>{file ? file.name : "Choose a PDF file"}</b><small>{file ? `${(file.size / 1024 / 1024).toFixed(2)} MB` : "PDF only"}</small></label><label className="field">Optional title<input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Use if metadata is not yet known" /></label>{error && <p className="error">{error}</p>}<footer><button className="secondary" onClick={close}>Cancel</button><button className="primary" disabled={busy} onClick={send}>{busy ? "Uploading…" : "Upload and process"}</button></footer></section></div>; }
function LoginModal({ close, login }: { close: () => void; login: (token: string) => Promise<void> }) { const [token, setToken] = useState(""); const [error, setError] = useState(""); const [busy, setBusy] = useState(false); const submit = async () => { if (!token.trim()) return setError("请输入登录密钥。"); setBusy(true); setError(""); try { await login(token.trim()); } catch (reason) { setError(message(reason)); } finally { setBusy(false); } }; return <div className="modal-layer"><section className="upload-modal"><header><div><p className="eyebrow">ACCESS CONTROL</p><h2>登录</h2><p>登录后可上传、检索、运行工作流和查看完整文献内容。</p></div><button className="icon" onClick={close}>×</button></header><label className="field">登录密钥<input type="password" autoFocus value={token} onChange={(event) => setToken(event.target.value)} onKeyDown={(event) => event.key === "Enter" && void submit()} /></label>{error && <p className="error">{error}</p>}<footer><button className="secondary" onClick={close}>取消</button><button className="primary" disabled={busy} onClick={() => void submit()}>{busy ? "登录中…" : "登录"}</button></footer></section></div>; }
function Inspector({ call, recent, debug, close }: { call: Call | null; recent: Call[]; debug: Debug; close: () => void }) { return <aside className="inspector"><header><div><p className="eyebrow">DEVELOPER</p><h2>API Inspector</h2></div><button className="icon" onClick={close}>×</button></header><p>Every visible result uses the live backend. {debug === "off" ? "Core fields only." : "Request and response details are available."}</p>{call ? <><div className="inspector-call"><Status text={String(call.status || "ERR")} tone={call.error ? "danger" : "success"} /><b>{call.method}</b><code>{call.path}</code><small>{call.latency} ms {call.requestId ? `· ${call.requestId}` : ""}</small></div>{call.error && <p className="error">{call.error}</p>}{debug !== "off" && <details open><summary>Request</summary><pre>{pretty(call.body ?? "No body")}</pre></details>}<details open={debug === "verbose"}><summary>Response</summary><pre>{pretty(call.response ?? "No response")}</pre></details></> : <Empty text="Run an operation to inspect its real request and response." />}{recent.length > 1 && <div className="history"><p className="eyebrow">RECENT</p>{recent.slice(1, 7).map((item, index) => <div key={`${item.at}-${index}`}><span>{item.status || "ERR"}</span><code>{item.method} {item.path}</code></div>)}</div>}</aside>; }
function Developer(p: any) { const [schema, setSchema] = useState<any>(null); const [method, setMethod] = useState("POST"); const [path, setPath] = useState("/api/v1/papers/search"); const [body, setBody] = useState('{\n  "query": "electrolyte",\n  "filters": {},\n  "page": 1,\n  "page_size": 20\n}'); const [result, setResult] = useState<any>(null); const run = async () => { try { setResult(await p.api(method, path, { body: body.trim() ? JSON.parse(body) : undefined })); } catch (error) { setResult({ error: message(error) }); } }; return <div className="drawer-layer"><aside className="developer-drawer"><header><div><p className="eyebrow">RAW API ACCESS</p><h2>Developer Drawer</h2><p>{p.base}</p></div><button className="icon" onClick={p.close}>×</button></header><p className="notice">This sends a real backend request. Multipart uploads stay in Paper Library.</p><div className="developer-form"><select value={method} onChange={(event) => setMethod(event.target.value)}><option>GET</option><option>POST</option></select><input value={path} onChange={(event) => setPath(event.target.value)} /><textarea rows={11} value={body} onChange={(event) => setBody(event.target.value)} /><div><button className="secondary" onClick={async () => setSchema(await p.api("GET", "/openapi.json"))}>Load OpenAPI</button><button className="primary" onClick={run}>Run live request</button></div></div>{schema && <details><summary>OpenAPI paths ({Object.keys(schema.paths || {}).length})</summary><pre>{pretty(Object.keys(schema.paths || {}))}</pre></details>}{result && <details open><summary>Response</summary><pre>{pretty(result)}</pre></details>}</aside></div>; }
function PdfCanvas({ base, token, source }: { base: string; token: string; source: any }) { const canvas = useRef<HTMLCanvasElement | null>(null); const renderTask = useRef<any>(null); const serial = useRef(0); const [error, setError] = useState(""); const [bboxNotice, setBboxNotice] = useState(""); useEffect(() => { let alive = true; const sequence = ++serial.current; renderTask.current?.cancel(); setError(""); setBboxNotice(""); const render = async () => { try { const assetId = source.pdf_asset_id || source.pdf?.asset_id || source.citation?.assets?.find((asset: any) => asset.asset_type === "pdf")?.asset_id; const url = source.pdf_url || (assetId ? `${base.replace(/\/$/, "")}/api/v1/assets/${assetId}/download` : null); if (!url || !canvas.current) throw new Error("Source response did not include a PDF asset."); const pdfjsLib = await loadPdfjs(); const pdf = await pdfjsLib.getDocument({ url, httpHeaders: token ? { Authorization: `Bearer ${token}` } : undefined }).promise; const location = source.citation?.location || source.location || {}; const page = await pdf.getPage(location.page || source.page || 1); const viewport = page.getViewport({ scale: 1.25 }); const element = canvas.current; const context = element.getContext("2d"); if (!context || !alive || sequence !== serial.current) return; element.width = Math.floor(viewport.width); element.height = Math.floor(viewport.height); context.clearRect(0, 0, element.width, element.height); renderTask.current = page.render({ canvas: element, canvasContext: context, viewport } as any); await renderTask.current.promise; if (!alive || sequence !== serial.current) return; const bbox = location.bbox || source.bbox; const valid = Array.isArray(bbox) && location.coordinate_space === "normalized" && bbox.length === 4 && bbox.every((value: unknown) => typeof value === "number" && value >= 0 && value <= 1) && bbox[2] > bbox[0] && bbox[3] > bbox[1]; if (bbox !== undefined && !valid) { setBboxNotice("Source location is not a valid normalized bbox; highlight is unavailable."); return; } if (valid) { const [x0, y0, x1, y1] = bbox; context.save(); context.fillStyle = "rgba(217,113,48,.22)"; context.strokeStyle = "#bd5920"; context.lineWidth = 2; context.fillRect(x0 * viewport.width, y0 * viewport.height, (x1 - x0) * viewport.width, (y1 - y0) * viewport.height); context.strokeRect(x0 * viewport.width, y0 * viewport.height, (x1 - x0) * viewport.width, (y1 - y0) * viewport.height); context.restore(); } } catch (reason: any) { if (alive && reason?.name !== "RenderingCancelledException") setError(reason?.message || "PDF rendering failed."); } }; void render(); return () => { alive = false; renderTask.current?.cancel(); }; }, [base, source, token]); return <div className="pdf-frame">{error && <p className="error">PDF unavailable from backend: {error}</p>}{bboxNotice && <p className="muted">{bboxNotice}</p>}<canvas ref={canvas} /></div>; }
function ImagePreview({ url, token, alt }: { url: string; token: string; alt: string }) { const [src, setSrc] = useState(""); const [error, setError] = useState(""); useEffect(() => { let current = ""; setSrc(""); setError(""); void fetch(url, { headers: token ? { Authorization: `Bearer ${token}` } : undefined }).then(async (response) => { if (!response.ok) { const payload = await response.json().catch(() => null); throw new Error(errorOf(payload, response.status)); } return response.blob(); }).then((blob) => { current = URL.createObjectURL(blob); setSrc(current); }).catch((reason) => setError(message(reason))); return () => { if (current) URL.revokeObjectURL(current); }; }, [token, url]); if (error) return <p className="error">Asset preview unavailable from backend: {error}</p>; return src ? <img className="asset-image" src={src} alt={alt} onError={() => setError("The downloaded response is not a valid image.")} /> : <p className="muted">Loading asset preview…</p>; }
function Reference(p: any) { const endpoints = [["POST", "/api/v1/papers/search", "Search catalog metadata.", { query: "electrolyte", filters: {}, page: 1, page_size: 20 }], ["POST", "/api/v1/evidence/search", "Retrieve candidate Evidence IDs. Scope uses scope.paper_ids.", { query: "Which electrolyte is non-flammable?", scope: { paper_ids: ["paper_id"] }, filters: {}, page: 1, page_size: 10 }], ["GET", "/api/v1/evidence/{evidence_id}/content", "Expand a selected hit to surrounding context before citation.", "?before=1200&after=2400"], ["POST", "/api/v1/evidence/packs", "Queue a verified evidence pack for explicit claims.", { claims: [{ text: "A non-flammable electrolyte improves stability." }], scope: { paper_ids: ["paper_id"] } }], ["POST", "/api/v1/citations/ground", "Queue draft grounding and citation insertion.", { draft: "A non-flammable electrolyte improves stability.", scope: { paper_ids: ["paper_id"] } }], ["GET", "/api/v1/workflows/{run_id}", "Poll a queued workflow until it succeeds or fails.", "No body"]]; return <section className="page"><Heading eyebrow="AGENT CONTRACT" title="API Reference" text="Use evidence primitives directly, or submit claims/drafts to the workflow layer and poll its run." action={<button className="primary" onClick={() => p.setShowDeveloper(true)}>Open live playground</button>} /><div className="reference-flow"><span>Claims / draft</span><b>→</b><span>Workflow run</span><b>→</b><span>Verified evidence / citations</span></div><div className="api-list">{endpoints.map(([method, path, when, example]: any[]) => <article key={path}><div><Status text={method} tone={method === "GET" ? "info" : "warning"} /><code>{path}</code></div><p>{when}</p><pre>{pretty(example)}</pre></article>)}</div></section>; }
function Heading({ eyebrow, title, text, action }: { eyebrow: string; title: string; text: string; action?: any }) { return <div className="heading"><div><p className="eyebrow">{eyebrow}</p><h2>{title}</h2><p>{text}</p></div>{action}</div>; }
function SectionTitle({ eyebrow, title, action }: { eyebrow: string; title: string; action?: any }) { return <div className="section-title"><div><p className="eyebrow">{eyebrow}</p><h3>{title}</h3></div>{action}</div>; }
function Metric({ title, value, note }: { title: string; value: string; note: string }) { return <article className="metric"><span>{title}</span><strong>{value}</strong><small>{note}</small></article>; }
function Pipeline({ task, paper }: { task: Task | null; paper?: Paper | null }) { if (!task) return <Empty text="No pipeline task is being tracked in this browser." />; const pct = Math.round((task.progress || 0) * 100); return <div className="pipeline"><div><div><b>{task.operation || "pipeline"}</b><Status text={task.status} /></div><strong>{pct}%</strong></div><div className="progress"><span style={{ width: `${pct}%` }} /></div><p>{paper?.title || task.paper_id || "Paper"}</p>{task.error_message && <p className="error">{task.error_message}</p>}<small>{task.task_id}</small></div>; }
function Status({ text, tone }: { text: string; tone?: string }) { const kind = tone || (/(healthy|ready|succeeded|completed|searchable|supported)/i.test(text) ? "success" : /(failed|error|unavailable)/i.test(text) ? "danger" : /(running|processing|pending|not.started|not indexed)/i.test(text) ? "warning" : "info"); return <span className={`status ${kind}`}>{text.replaceAll("_", " ")}</span>; }
function formatDetail(value: unknown) { return typeof value === "object" && value !== null ? JSON.stringify(value) : String(value); }
function Empty({ text }: { text: string }) { return <div className="empty">{text}</div>; }
function read<T>(key: string, fallback: T): T { try { const raw = localStorage.getItem(key); return raw ? JSON.parse(raw) as T : fallback; } catch { return fallback; } }
function numberOrNull(value: string) { const result = Number(value); return value.trim() && Number.isFinite(result) ? result : null; }
function resourceCount(counts: any) { return Object.entries(counts || {}).filter(([key]) => key !== "pdf").reduce((sum, [, value]) => sum + (Number(value) || 0), 0); }
function clip(text: string, length: number) { return text.length > length ? `${text.slice(0, length).trim()}…` : text; }
function pretty(value: unknown) { return JSON.stringify(value, null, 2); }
function message(error: unknown) { return error instanceof Error ? error.message : String(error); }
function errorOf(value: any, status: number) { return value?.detail?.message || value?.message || value?.detail || `HTTP ${status}`; }
createRoot(document.getElementById("root")!).render(<StrictMode><App /></StrictMode>);

