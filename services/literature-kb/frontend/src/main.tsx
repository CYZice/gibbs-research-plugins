import { StrictMode, Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./theme.css";
import "./styles.css";
import "./harvest.css";
import "./workbench.css";
import { PdfReader } from "./PdfReader";
import { Icon } from "./icons";

type View = "dashboard" | "library" | "harvest" | "admin" | "reference";
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
  const [resetVersion, setResetVersion] = useState(0);
  const [paper, setPaper] = useState<Paper | null>(null);
  const [content, setContent] = useState<any>(null);
  const [assets, setAssets] = useState<any[]>([]);
  const [drawerTab, setDrawerTab] = useState("overview");
  const paperRequest = useRef(0);
  const evidenceRequest = useRef(0);
  const [custom, setCustom] = useState<string[]>(() => read(K.custom, []));
  const [scope, setScope] = useState<Scope>(() => read(K.scope, "library"));
  const [tab, setTab] = useState<Tab>(() => read(K.tab, "search"));
  const [question, setQuestion] = useState("");
  const [hits, setHits] = useState<Hit[]>([]);
  const [searchMeta, setSearchMeta] = useState<any>(null);
  const [evidenceId, setEvidenceId] = useState(() => read(K.evidence, ""));
  const [context, setContext] = useState<any>(null);
  const [source, setSource] = useState<any>(null);
  const [resource, setResource] = useState<any>(null);
  const [pack, setPack] = useState<any>(null);
  const [packClaims, setPackClaims] = useState("");
  const [claim, setClaim] = useState("");
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
  const [notice, setNotice] = useState("");
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
  useEffect(() => { if (resetVersion) void loadPapers(1); }, [resetVersion]);
  const resetFilters = () => { setQuery(""); setYearFrom(""); setYearTo(""); setReady(""); setResetVersion((v) => v + 1); };
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
  const openPaper = useCallback(async (item: Paper, nextTab = "overview", keepEvidence = false) => {
    const sequence = ++paperRequest.current;
    if (!keepEvidence) { ++evidenceRequest.current; setBusy((current) => ["context", "source", "resource"].includes(current) ? "" : current); }
    setPaper(item); setDrawerTab(nextTab); setContent(null); setAssets([]); setContext(null); setSource(null); setResource(null); setEvidenceId(""); setTab("search");
    try {
      const [detail, body, assetResponse] = await Promise.all([
        api("GET", `/api/v1/papers/${item.paper_id}`),
        api("GET", `/api/v1/papers/${item.paper_id}/content?offset=0&limit=6000`),
        api("GET", `/api/v1/papers/${item.paper_id}/assets?page=1&page_size=48`),
      ]);
      if (sequence !== paperRequest.current) return;
      setPaper(detail); setContent(body); setAssets(assetResponse.items || []);
    } catch (error) { if (sequence === paperRequest.current) setNotice(`Paper detail failed: ${message(error)}`); }
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
      setHits(result.items || result.evidence || []); setSearchMeta(result); setTab("search"); setNotice("");
    } catch (error) { setNotice(`Evidence search failed: ${message(error)}`); }
    finally { setBusy(""); }
  };
  const readEvidence = async (id: string, kind: "context" | "source") => {
    const target = id.trim();
    if (!target) return setNotice("请选择证据或输入 evidence_id。");
    const sequence = ++evidenceRequest.current;
    if (target !== evidenceId) { setContext(null); setSource(null); }
    setEvidenceId(target); setBusy(kind);
    try {
      const result = await api("GET", `/api/v1/evidence/${encodeURIComponent(target)}/${kind === "context" ? "content?before=1200&after=2400" : "source"}`);
      if (sequence !== evidenceRequest.current) return;
      const ref = result.paper || result.citation?.paper;
      if (ref?.paper_id && ref.paper_id !== paper?.paper_id) await openPaper(ref, "overview", true);
      if (sequence !== evidenceRequest.current) return;
      setEvidenceId(target);
      if (kind === "context") setContext(result); else setSource(result);
      setTab(kind); setNotice("");
    } catch (error) { if (sequence === evidenceRequest.current) setNotice(`证据读取失败：${message(error)}`); }
    finally { if (sequence === evidenceRequest.current) setBusy(""); }
  };
  const runContext = (id = evidenceId) => readEvidence(id, "context");
  const runSource = (id = evidenceId) => readEvidence(id, "source");
  const openContext = (hit: Hit) => runContext(hit.evidence_id);
  const openSource = (hit: Hit) => runSource(hit.evidence_id);
  const openResource = async (id: string, related?: Paper) => {
    const sequence = ++evidenceRequest.current; setBusy("resource");
    try {
      if (related && related.paper_id !== paper?.paper_id) await openPaper(related, "assets", true);
      if (sequence !== evidenceRequest.current) return;
      const result = await api("GET", `/api/v1/assets/${id}`);
      if (sequence === evidenceRequest.current) { setResource(result); setTab("resource"); }
    } catch (error) { if (sequence === evidenceRequest.current) setNotice(`Resource failed: ${message(error)}`); }
    finally { if (sequence === evidenceRequest.current) setBusy(""); }
  };
  const waitWorkflow = async (runId: string) => { for (let attempt = 0; attempt < 180; attempt += 1) { const run = await api("GET", `/api/v1/workflows/${runId}`); setWorkflowRun(run); if (run.status === "succeeded") return run.result; if (run.status === "failed") throw new Error(run.error_message || "Workflow failed."); await new Promise((resolve) => window.setTimeout(resolve, 1000)); } throw new Error("Workflow timed out while waiting for a result."); };
  const buildPack = async () => { const claims = packClaims.split(/\r?\n/).map((text) => text.trim()).filter(Boolean); if (!claims.length) return setNotice("Enter one explicit claim per line before building a pack."); if (scope !== "library" && !scopeIds.length) return setNotice("The selected scope has no papers. Select papers in Library first."); setBusy("pack"); try { const run = await api("POST", "/api/v1/evidence/packs", { body: { claims: claims.map((text) => ({ text })), scope: { paper_ids: scopeIds } } }); setWorkflowRun(run); setPack(await waitWorkflow(run.run_id)); setTab("pack"); } catch (error) { setNotice(`Evidence Pack failed: ${message(error)}`); } finally { setBusy(""); } };
  const groundClaim = async () => { if (!claim.trim()) return setNotice("Enter a draft before grounding."); if (scope !== "library" && !scopeIds.length) return setNotice("The selected scope has no papers. Select papers in Library first."); setBusy("grounding"); try { const run = await api("POST", "/api/v1/citations/ground", { body: { draft: claim.trim(), scope: { paper_ids: scopeIds } } }); setWorkflowRun(run); setGrounding(await waitWorkflow(run.run_id)); setTab("grounding"); } catch (error) { setNotice(`Citation grounding failed: ${message(error)}`); } finally { setBusy(""); } };


  const props: any = { base, setBase, token, setToken, debug, setDebug, health, healthError, refreshHealth, overview, refreshOverview, papers, total, page, query, setQuery, yearFrom, setYearFrom, yearTo, setYearTo, ready, setReady, catalogSource, setCatalogSource, discoverySources, literatureMeta, importLiterature, importSources, setImportSources, loading, loadPapers, resetFilters, paper, setPaper, content, assets, drawerTab, setDrawerTab, openPaper, custom, toggleCustom, showUpload, setShowUpload, upload, pipeline, setPipeline, task, scope, setScope, scopeIds, tab, setTab, question, setQuestion, searchEvidence, hits, searchMeta, context, source, resource, pack, packClaims, setPackClaims, claim, setClaim, grounding, workflowRun, evidenceId, setEvidenceId, runContext, runSource, openContext, openSource, openResource, buildPack, groundClaim, busy, call, recent, notice, showInspector, setShowInspector, showDeveloper, setShowDeveloper, authenticated, setShowLogin, logout, api };
  return <div className="app-shell"><Sidebar view={view} setView={setView} developer={() => setShowDeveloper(true)} admin={() => setView("admin")} controls={<ConnectionActions {...props} />} /><main className="workspace">{notice && <div className="noticebar" role="status">{notice}</div>}{view === "dashboard" && <Dashboard {...props} />}{view === "library" && <LiteratureWorkbench {...props} />}{view === "harvest" && <Suspense fallback={<div className="page muted">正在加载采集工作台…</div>}><HarvestView base={base} token={token} /></Suspense>}{view === "admin" && <Suspense fallback={<div className="page muted">Loading admin workspace...</div>}><AdminView base={base} /></Suspense>}{view === "reference" && <Reference {...props} />}</main>{showInspector && <Inspector call={call} recent={recent} debug={debug} close={() => setShowInspector(false)} />}{paper && view !== "library" && <PaperDrawer {...props} close={() => setPaper(null)} />}{showUpload && <UploadModal close={() => setShowUpload(false)} submit={upload} busy={busy === "upload"} />}{showLogin && <LoginModal close={() => setShowLogin(false)} login={login} />}{showDeveloper && <Developer {...props} close={() => setShowDeveloper(false)} />}</div>;
}

function Sidebar({ view, setView, developer, admin, controls }: { view: View; setView: (view: View) => void; developer: () => void; admin: () => void; controls: React.ReactNode }) {
  const links = [["library", "library", "文献工作台"], ["harvest", "download", "批量采集"], ["dashboard", "dashboard", "工作概览"]] as const;
  return <aside className="sidebar" aria-label="文献库导航"><nav>{links.map(([id, icon, text]) => <button key={id} className={view === id ? "nav active" : "nav"} title={text} aria-label={text} aria-current={view === id ? "page" : undefined} onClick={() => setView(id)}><Icon name={icon} /></button>)}</nav><div className="sidebar-bottom"><a className="icon" href="./docs/" target="_blank" rel="noreferrer" title="使用指南" aria-label="使用指南"><Icon name="guide" /></a><button className="icon" onClick={developer} title="开发者工具" aria-label="开发者工具"><Icon name="code" /></button><button className="icon" onClick={admin} title="管理控制台" aria-label="管理控制台"><Icon name="settings" /></button>{controls}</div></aside>;
}
function ConnectionActions(p: any) {
  const healthy = p.health?.status === "healthy";
  return <><button className={`icon health-icon ${healthy ? "healthy" : "bad"}`} title={healthy ? "服务正常 · 刷新" : "服务不可用 · 重试"} aria-label={healthy ? "服务正常 · 刷新" : "服务不可用 · 重试"} onClick={p.refreshHealth}><Icon name="refresh" /></button><button className="icon" title={p.authenticated ? "退出登录" : "登录"} aria-label={p.authenticated ? "退出登录" : "登录"} onClick={p.authenticated ? p.logout : () => p.setShowLogin(true)}><Icon name={p.authenticated ? "logout" : "login"} /></button><button className="icon" title="请求检查器" aria-label="请求检查器" onClick={() => p.setShowInspector(!p.showInspector)}><Icon name="dashboard" /></button></>;
}
function Dashboard(p: any) { const active = p.overview?.active_task; const progress = active?.progress == null ? "—" : `${Math.round(active.progress * 100)}%`; return <section className="page"><Heading title="工作概览" action={<div className="heading-actions"><a className="secondary docs-shortcut" href="./docs/" target="_blank" rel="noreferrer">Integration guide ↗</a><button className="primary" onClick={() => p.setShowUpload(true)}>Upload PDF</button></div>} /><div className="metrics"><Metric title="Papers in catalog" value={String(p.overview?.papers ?? p.total)} note="Whole library" /><Metric title="Searchable" value={String(p.overview?.searchable ?? "—")} note="Whole-library evidence ready" /><Metric title="AI ready" value={String(p.overview?.ai_ready ?? "—")} note="Whole-library embedding ready" /><Metric title="Active pipeline" value={progress} note={active ? `${active.operation || "task"} · ${active.status}` : "No active task"} /></div><div className="dashboard-grid"><section className="surface"><SectionTitle title="Health monitor" action={<button className="text" onClick={() => { p.refreshHealth(); p.refreshOverview(); }}>Refresh</button>} />{p.healthError && <p className="error">{p.healthError}</p>}<div className="health-list">{(p.health?.components || []).map((item: any) => <div className="health-row" key={item.name}><i className={`dot ${item.status}`} /><b>{item.name}</b><span>{formatDetail(item.detail ?? item.status)}</span><small>{item.latency_ms ?? 0} ms</small></div>)}{!p.health?.components?.length && <Empty text="Health response is not available." />}</div></section><section className="surface"><SectionTitle title="Current active task" /><Pipeline task={active} paper={null} /><p className="muted">Browser-tracked task: {p.task?.task_id || "none"}</p></section></div></section>; }
function Library(p: any) {
  const remoteMode = p.catalogSource !== "local";
  return <section className="page">
    <Heading title="论文库" action={<button className="primary icon-action" onClick={() => p.setShowUpload(true)} title="上传 PDF" aria-label="上传 PDF"><Icon name="upload" /></button>} />
    <div className="library-tools"><div className="search"><span><Icon name="search" /></span><input value={p.query} onChange={(event) => p.setQuery(event.target.value)} onKeyDown={(event) => event.key === "Enter" && p.loadPapers(1)} placeholder="搜索标题、摘要或 DOI" /><button onClick={() => p.loadPapers(1)} title="搜索" aria-label="搜索"><Icon name="search" /></button></div>
      <div className="filters"><select aria-label="文献来源" value={p.catalogSource} onChange={(event) => p.setCatalogSource(event.target.value)}><option value="local">本地</option><option value="all">全部</option>{p.discoverySources.map((source: any) => <option key={source.source} value={source.source} disabled={source.configured === false}>{source.source === "openalex" ? "OpenAlex" : source.source === "arxiv" ? "arXiv" : source.source.toUpperCase()}{source.configured === false ? "（未配置）" : ""}</option>)}</select><input value={p.yearFrom} onChange={(event) => p.setYearFrom(event.target.value)} aria-label="起始年" placeholder="起始年" /><input value={p.yearTo} onChange={(event) => p.setYearTo(event.target.value)} aria-label="结束年" placeholder="结束年" />{!remoteMode && <select aria-label="处理状态" value={p.ready} onChange={(event) => p.setReady(event.target.value)}><option value="">全部状态</option><option value="true">可检索</option><option value="false">处理中</option></select>}<button className="secondary icon-action" title="重置筛选" aria-label="重置筛选" onClick={p.resetFilters}><Icon name="refresh" /></button></div>
    </div>
    {p.literatureMeta?.source_status && <div className="library-summary">{p.literatureMeta.source_status.map((source: any) => <span key={source.source}><Status text={`${source.source}: ${source.status}`} /> {source.latency_ms} ms</span>)}</div>}
    <div className="library-summary"><b>{p.total} 篇</b>{p.custom.length > 0 && <span>已选 {p.custom.length} 篇</span>}</div>
    <section className="paper-list">{p.loading && <div className="loader" />}{p.papers.map((item: Paper) => {
      const hit = item._literature; const isLocal = !hit || Boolean(hit.local_paper); const selected = p.importSources[hit?.identity_key] || ""; const selectedRemote = selected ? hit.remote_options?.find((option: any) => option.source === selected) : hit?.remote;
      return <article className={`paper-row${p.paper?.paper_id === item.paper_id ? " selected" : ""}`} key={item.paper_id}><label className="check"><input type="checkbox" disabled={!isLocal} aria-label={`选择 ${item.title || item.paper_id}`} checked={isLocal && p.custom.includes(item.paper_id)} onChange={() => isLocal && p.toggleCustom(item.paper_id)} /><span /></label><button className="paper-main" disabled={!isLocal} onClick={() => isLocal && p.openPaper(item)}><h3>{item.title || "Untitled paper"} {item.year && <em>{item.year}</em>}</h3><p>{[item.authors?.slice(0, 3).map((author: any) => author.name).filter(Boolean).join(", "), item.venue, item.doi].filter(Boolean).join(" · ") || item.paper_id}</p><small>{item.abstract ? clip(item.abstract, 210) : "Metadata extraction has not completed yet."}</small></button><div className="paper-state">{hit ? <><Status text={hit.availability} /><Status text={selectedRemote?.license_policy || "local"} /><small>{hit.origins?.map((origin: any) => origin.source).join(" + ")}{hit.possible_duplicate ? " · possible duplicate" : ""}</small>{!isLocal && <select value={selected} onChange={(event) => p.setImportSources((current: Record<string, string>) => ({ ...current, [hit.identity_key]: event.target.value }))}><option value="">自动选择（推荐）</option>{(hit.remote_options || []).map((option: any) => <option key={`${option.source}:${option.external_id}`} value={option.source} disabled={!option.downloadable}>{option.source === "arxiv" ? "arXiv" : option.source === "openalex" ? "OpenAlex" : option.source.toUpperCase()}{!option.downloadable ? "（不可用）" : ""}</option>)}</select>}</> : <><Status text={item.processing_status?.parse || "not started"} /><Status text={item.capabilities?.ai_ready ? "AI ready" : item.capabilities?.can_search_evidence ? "Evidence ready" : "not indexed"} tone={item.capabilities?.ai_ready ? "success" : undefined} /><small>{resourceCount(item.asset_counts)} resources</small></>}</div>{isLocal ? <button className="row-action icon-action" title="查看资源" aria-label="查看资源" onClick={() => p.openPaper(item, "assets")}><Icon name="next" /></button> : <button className="row-action" disabled={!selectedRemote?.downloadable || p.busy === `import:${selectedRemote?.external_id}`} onClick={() => p.importLiterature(item)}>{p.busy === `import:${selectedRemote?.external_id}` ? "Importing…" : selectedRemote?.downloadable ? "Import →" : "Unavailable"}</button>}</article>;
    })}{!p.loading && !p.papers.length && <Empty text="暂无匹配文献" />}</section>
    {!remoteMode && <div className="pagination"><button disabled={p.page <= 1} title="上一页" aria-label="上一页" onClick={() => p.loadPapers(p.page - 1)}><Icon name="previous" /></button><span>{p.page} / {Math.max(1, Math.ceil(p.total / 20))}</span><button disabled={p.papers.length < 20} title="下一页" aria-label="下一页" onClick={() => p.loadPapers(p.page + 1)}><Icon name="next" /></button></div>}
  </section>;
}
function LiteratureWorkbench(p: any) {
  const [pane, setPane] = useState("library");
  const [documentTab, setDocumentTab] = useState("overview");
  const [tool, setTool] = useState("search");
  const [textContent, setTextContent] = useState<any>(null);
  const [reading, setReading] = useState(false);
  const [readError, setReadError] = useState("");
  const textRequest = useRef(0);
  useEffect(() => { setTextContent(p.content); }, [p.content]);
  useEffect(() => { ++textRequest.current; setReadError(""); setReading(false); }, [p.paper?.paper_id]);
  useEffect(() => {
    if (p.tab === "context" || p.tab === "source" || p.tab === "resource") {
      setDocumentTab(p.tab); setPane("document");
    } else if (p.tab === "pack" || p.tab === "grounding") setTool(p.tab);
  }, [p.tab, p.context, p.source, p.resource]);
  const selectPaper = async (item: Paper, tab = "overview") => {
    setDocumentTab(tab === "assets" ? "assets" : "pdf"); setPane("document");
    await p.openPaper(item, tab);
  };
  const pdfAsset = p.assets.find((asset: any) => asset.asset_type === "pdf");
  const plainSource = useMemo(() => pdfAsset ? { pdf_asset_id: pdfAsset.asset_id } : null, [pdfAsset]);
  const citation = p.source?.citation || p.source;
  const sourcePdf = documentTab === "source" ? p.source : plainSource;
  const text = textContent?.content || textContent;
  const readMore = async () => {
    if (reading || !text?.more || text.next_offset == null || !p.paper) return;
    const sequence = ++textRequest.current; const paperId = p.paper.paper_id;
    setReading(true); setReadError("");
    try {
      const next = await p.api("GET", `/api/v1/papers/${paperId}/content?offset=${text.next_offset}&limit=6000`);
      if (sequence === textRequest.current) setTextContent({ ...next, content: { ...next.content, text: text.text + next.content.text } });
    } catch (error) { if (sequence === textRequest.current) setReadError(message(error)); }
    finally { if (sequence === textRequest.current) setReading(false); }
  };
  const documentTabs = [["overview", "摘要"], ["content", "正文"], ["pdf", "PDF"], ["assets", "图表"], ["pipeline", "处理"]];
  if (p.context) documentTabs.push(["context", "证据正文"]);
  if (p.source) documentTabs.push(["source", "证据原文"]);
  if (p.resource) documentTabs.push(["resource", "资源"]);
  return <section className={`literature-workbench pane-${pane}`} aria-label="文献工作台">
    <nav className="workbench-mobile" aria-label="工作台分栏">{[["library", "library", "文献"], ["document", "guide", "原文"], ["evidence", "search", "证据"]].map(([id, icon, label]) => <button key={id} className={pane === id ? "active" : ""} aria-pressed={pane === id} onClick={() => setPane(id)}><Icon name={icon as "library"} /><span>{label}</span></button>)}</nav>
    <div className="workbench-columns">
      <aside className="workbench-library" aria-label="文献列表"><Library {...p} openPaper={selectPaper} /></aside>
      <section className="workbench-document" aria-label="原文阅读">
        {p.paper ? <><header className="document-heading"><h1>{p.paper.title || p.paper.paper_id}</h1><p>{[p.paper.authors?.slice(0, 3).map((a: any) => a.name).filter(Boolean).join(", "), p.paper.venue, p.paper.year].filter(Boolean).join(" · ")}</p></header>
          <nav className="document-tabs" aria-label="阅读视图">{documentTabs.map(([id, label]) => <button key={id} className={documentTab === id ? "active" : ""} aria-pressed={documentTab === id} onClick={() => setDocumentTab(id)}>{label}</button>)}</nav>
          <div className="document-body">
            {documentTab === "overview" && <article className="reading-text"><h2>摘要</h2><p>{p.paper.abstract || "暂无摘要"}</p>{p.paper.doi && <a href={`https://doi.org/${p.paper.doi}`} target="_blank" rel="noreferrer">{p.paper.doi} ↗</a>}</article>}
            {documentTab === "content" && <article className="reading-text"><p className="paper-content">{text?.text || "正文尚未就绪"}</p>{readError && <p className="error" role="alert">{readError}</p>}{text?.more && <button className="secondary" disabled={reading} onClick={() => void readMore()}>{reading ? "加载中…" : "继续阅读"}</button>}</article>}
            {(documentTab === "pdf" || documentTab === "source") && (sourcePdf ? <PdfReader base={p.base} token={p.token} source={sourcePdf} /> : <Empty text={p.content ? "暂无 PDF，切换正文阅读" : "正在读取文献…"} />)}
            {documentTab === "assets" && <div className="reader-assets">{p.assets.filter((asset: any) => asset.asset_type !== "pdf").map((asset: any) => <button key={asset.asset_id} onClick={() => p.openResource(asset.asset_id)}><Icon name="studio" /><span>{asset.caption || asset.asset_type}</span><small>{asset.location?.page ? `p. ${asset.location.page}` : asset.mime_type}</small></button>)}{!p.assets.some((asset: any) => asset.asset_type !== "pdf") && <Empty text="暂无图表资源" />}</div>}
            {documentTab === "pipeline" && <div className="reading-text">{p.task?.paper_id === p.paper.paper_id ? <Pipeline task={p.task} paper={p.paper} /> : <div className="status-grid">{Object.entries(p.paper.processing_status || {}).map(([key, value]) => <div key={key}><small>{key}</small><Status text={String(value)} /></div>)}</div>}</div>}
            {documentTab === "context" && <article className="reading-text"><h2>{p.context?.location?.section_title || "证据正文"}</h2><p className="muted">{p.context?.location?.page ? `p. ${p.context.location.page}` : p.evidenceId}</p><p className="paper-content">{p.context?.content?.text || p.context?.context_text || "暂无上下文"}</p><button className="secondary icon-action" title="定位原文" aria-label="定位原文" disabled={Boolean(p.busy)} onClick={() => p.runSource()}><Icon name="library" /></button></article>}
            {documentTab === "resource" && <ResourcePanel base={p.base} token={p.token} resource={p.resource} />}
          </div></> : <div className="document-empty"><Icon name="guide" /><p>选择一篇文献开始阅读</p></div>}
      </section>
      <aside className="workbench-evidence" aria-label="证据与核验"><nav className="document-tabs" aria-label="证据工具">{[["search", "证据"], ["pack", "论断核验"], ["grounding", "引用校验"]].map(([id, label]) => <button key={id} aria-pressed={tool === id} className={tool === id ? "active" : ""} onClick={() => { setTool(id); p.setTab(id); }}>{label}</button>)}</nav>
        <div className="evidence-controls"><select aria-label="检索范围" value={p.scope} onChange={(event) => p.setScope(event.target.value)}><option value="library">全库</option><option value="selected" disabled={!p.paper}>当前论文</option><option value="custom">已选 ({p.custom.length})</option></select>{tool === "search" && <form className="evidence-query" onSubmit={(event) => { event.preventDefault(); void p.searchEvidence(); }}><input aria-label="检索问题" placeholder="检索文献证据…" value={p.question} onChange={(event) => p.setQuestion(event.target.value)} /><button className="primary icon-action" type="submit" disabled={Boolean(p.busy)} title="检索证据" aria-label="检索证据"><Icon name="search" /></button></form>}</div>
        <div className="evidence-body">{tool === "search" && <><SearchResults {...p} /><details className="evidence-direct"><summary>按编号定位</summary><EvidenceLookup {...p} /></details></>}{tool === "pack" && <PackPanel {...p} />}{tool === "grounding" && <GroundingPanel {...p} />}{documentTab === "source" && citation?.quote && !p.hits.some((hit: Hit) => hit.evidence_id === p.evidenceId) && <blockquote className="selected-quote">{citation.quote}</blockquote>}</div>
      </aside>
    </div>
  </section>;
}
function SearchResults(p: any) {
  return <section className="surface results">{p.hits.length > 0 && <SectionTitle title={`${p.hits.length} 条证据`} />}{p.hits.map((hit: Hit) => <article className="hit" key={hit.evidence_id}><div className="hit-top"><div><h4>{hit.paper?.title || hit.paper?.paper_id || "未命名论文"}</h4><p>{[hit.location?.section_title, hit.location?.section_type, hit.location?.page && `p. ${hit.location.page}`].filter(Boolean).join(" · ")}</p></div>{typeof hit.score === "number" && <b className="score">{hit.score.toFixed(3)}</b>}</div><blockquote>{hit.content?.snippet || hit.content?.quote || "暂无摘要"}</blockquote><div className="hit-actions"><button title="阅读正文" aria-label="阅读正文" onClick={() => p.openContext(hit)}><Icon name="guide" /></button><button title="查看原文" aria-label="查看原文" onClick={() => p.openSource(hit)}><Icon name="library" /></button>{hit.assets?.[0] && <button title="查看资源" aria-label="查看资源" onClick={() => p.openResource(hit.assets?.[0]?.asset_id, hit.paper)}><Icon name="download" /></button>}</div>{p.debug !== "off" && <details><summary>检索详情</summary><pre>{pretty({ evidence_id: hit.evidence_id, score: hit.score, representation: hit.representation, location: hit.location, assets: hit.assets, raw: p.debug === "verbose" ? hit : undefined })}</pre></details>}</article>)}{!p.hits.length && <div className="empty"><Icon name="search" /><span>{p.searchMeta ? "未找到匹配证据" : "暂无检索结果"}</span></div>}</section>;
}
function EvidenceLookup({ evidenceId, setEvidenceId, runContext, runSource, busy }: any) { return <div className="direct-runner"><label>evidence_id<input value={evidenceId} onChange={(event) => setEvidenceId(event.target.value)} placeholder="paper_xxx_p1_b8" /></label><button className="secondary icon-action" title="阅读正文" aria-label="阅读正文" disabled={busy === "context"} onClick={() => runContext()}><Icon name="guide" /></button><button className="secondary icon-action" title="查看原文" aria-label="查看原文" disabled={busy === "source"} onClick={() => runSource()}><Icon name="library" /></button></div>; }
function ResourcePanel({ base, token, resource }: { base: string; token: string; resource: any }) { if (!resource) return <Empty text="Choose Resource from an evidence hit or Paper Library asset." />; const asset = resource.asset || resource; const url = `${base.replace(/\/$/, "")}/api/v1/assets/${asset.asset_id}/download`; return <section className="surface detail-panel"><SectionTitle title={`${asset.asset_type || "asset"} · ${asset.asset_id}`} action={<a className="secondary" href={url} target="_blank" rel="noreferrer">Download / open</a>} />{asset.mime_type?.startsWith("image/") ? <ImagePreview url={url} token={token} alt={asset.asset_id} /> : asset.content_text ? <pre className="asset-text">{asset.content_text}</pre> : <p className="muted">Use Download / open to retrieve this resource from the live backend.</p>}<details><summary>Raw resource response</summary><pre>{pretty(resource)}</pre></details></section>; }
function WorkflowStatus({ run }: { run: any }) { return run ? <p className="muted">Run <code>{run.run_id}</code> · <Status text={run.status} /> · {run.details?.stage || "queued"}{run.error_message ? ` · ${run.error_message}` : ""}</p> : null; }
function PackPanel(p: any) { return <section className="surface detail-panel"><SectionTitle title="论断核验" action={<button className="primary" title="核验论断" aria-label="核验论断" disabled={Boolean(p.busy)} onClick={p.buildPack}><Icon name="next" /></button>} /><textarea rows={4} value={p.packClaims} onChange={(event) => p.setPackClaims(event.target.value)} aria-label="待核验论断" placeholder="每行一个论断" /><WorkflowStatus run={p.workflowRun} />{p.pack ? <div className="pack-list">{p.pack.claims?.flatMap((claim: any) => claim.evidence?.map((item: any) => <article key={`${claim.claim_id}-${item.evidence_id}`}><div><Status text={item.verdict} /><b>{typeof item.confidence === "number" ? item.confidence.toFixed(3) : "—"}</b></div><p>{item.quote || "暂无原文"}</p><small>{claim.claim} · {item.paper?.title || item.evidence_id}</small></article>))}</div> : <Empty text="暂无核验结果" />}</section>; }
function GroundingPanel(p: any) { return <section className="surface detail-panel"><SectionTitle title="引用校验" action={<button className="primary" title="校验引用" aria-label="校验引用" disabled={Boolean(p.busy) || !p.claim.trim()} onClick={p.groundClaim}><Icon name="next" /></button>} /><textarea aria-label="待校验草稿" placeholder="粘贴待校验草稿…" rows={5} value={p.claim} onChange={(event) => p.setClaim(event.target.value)} /><WorkflowStatus run={p.workflowRun} />{p.grounding ? <div className="ground-list"><article><p>{p.grounding.grounded_answer}</p></article>{p.grounding.claims?.map((item: any) => <article key={item.claim_id}><div><Status text={item.status} /></div><p>{item.evidence?.[0]?.quote || "No supporting citation returned."}</p><small>{item.claim}</small></article>)}{p.grounding.unverified_claims?.map((item: any) => <article key={`unverified-${item.claim_id}`}><div><Status text="unverified" /></div><small>{item.claim} · {item.reason}</small></article>)}{p.grounding.contradictory_evidence?.map((item: any) => <article key={`contradiction-${item.claim_id}`}><div><Status text="contradicted" /></div><small>{item.claim} · conflicting evidence retained outside the citation list</small></article>)}</div> : <Empty text="暂无引用校验结果" />}</section>; }

function PaperDrawer(p: any) { const paper = p.paper as Paper; const currentTask = p.task?.paper_id === paper.paper_id ? p.task : null; return <div className="drawer-layer"><aside className="paper-drawer"><header><div><p className="eyebrow">PAPER DETAIL</p><h2>{paper.title || paper.paper_id}</h2><p>{[paper.venue, paper.year, paper.doi].filter(Boolean).join(" · ")}</p></div><button className="icon" onClick={p.close}>×</button></header><div className="drawer-tabs">{["overview", "content", "assets", "pipeline"].map((item) => <button key={item} className={p.drawerTab === item ? "active" : ""} onClick={() => p.setDrawerTab(item)}>{item}</button>)}</div>{p.drawerTab === "overview" && <div className="drawer-body"><p>{paper.abstract || "Metadata extraction has not completed."}</p><dl><div><dt>Paper ID</dt><dd>{paper.paper_id}</dd></div><div><dt>DOI</dt><dd>{paper.doi || "—"}</dd></div><div><dt>Authors</dt><dd>{paper.authors?.map((author: any) => author.name).filter(Boolean).join(", ") || "—"}</dd></div></dl><div className="status-grid">{Object.entries(paper.processing_status || {}).map(([key, value]) => <div key={key}><small>{key}</small><Status text={String(value)} /></div>)}</div></div>}{p.drawerTab === "content" && <div className="drawer-body"><p className="muted">First 6,000 characters from the real content endpoint.</p><article className="paper-content">{p.content?.content?.text || p.content?.text || "Content is not ready."}</article></div>}{p.drawerTab === "assets" && <div className="drawer-body"><div className="asset-grid">{p.assets.map((asset: any) => <button key={asset.asset_id} onClick={() => p.openResource(asset.asset_id)}><span>{asset.asset_type}</span><b>{asset.location?.page ? `Page ${asset.location.page}` : asset.mime_type || "resource"}</b><small>{asset.asset_id}</small></button>)}</div>{!p.assets.length && <Empty text="No assets were returned." />}</div>}{p.drawerTab === "pipeline" && <div className="drawer-body">{currentTask ? <Pipeline task={currentTask} paper={paper} /> : <Empty text="Processing is automatic. Failed or interrupted processing can be retried by an administrator." />}<p className="muted">Figures, tables and formulas use MinerU captions and surrounding text only. No Vision or structured model is called.</p></div>}</aside></div>; }
function UploadModal({ close, submit, busy }: { close: () => void; submit: (file: File, title: string) => void; busy: boolean }) { const [file, setFile] = useState<File | null>(null); const [title, setTitle] = useState(""); const [error, setError] = useState(""); const send = () => { if (!file) return setError("Choose a PDF first."); if (file.type !== "application/pdf" && !file.name.toLowerCase().endsWith(".pdf")) return setError("Only PDF files are accepted."); submit(file, title); }; return <div className="modal-layer"><section className="upload-modal"><header><div><p className="eyebrow">INGEST INTO LIBRARY</p><h2>Upload a paper</h2><p>One click uploads the PDF and starts the complete automatic processing pipeline.</p></div><button className="icon" onClick={close}>×</button></header><label className="file-drop"><input type="file" accept="application/pdf,.pdf" onChange={(event) => { setFile(event.target.files?.[0] || null); setError(""); }} /><b>{file ? file.name : "Choose a PDF file"}</b><small>{file ? `${(file.size / 1024 / 1024).toFixed(2)} MB` : "PDF only"}</small></label><label className="field">Optional title<input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Use if metadata is not yet known" /></label>{error && <p className="error">{error}</p>}<footer><button className="secondary" onClick={close}>Cancel</button><button className="primary" disabled={busy} onClick={send}>{busy ? "Uploading…" : "Upload and process"}</button></footer></section></div>; }
function LoginModal({ close, login }: { close: () => void; login: (token: string) => Promise<void> }) { const [token, setToken] = useState(""); const [error, setError] = useState(""); const [busy, setBusy] = useState(false); const submit = async () => { if (!token.trim()) return setError("请输入登录密钥。"); setBusy(true); setError(""); try { await login(token.trim()); } catch (reason) { setError(message(reason)); } finally { setBusy(false); } }; return <div className="modal-layer"><section className="upload-modal"><header><div><p className="eyebrow">ACCESS CONTROL</p><h2>登录</h2><p>登录后可上传、检索、运行工作流和查看完整文献内容。</p></div><button className="icon" onClick={close}>×</button></header><label className="field">登录密钥<input type="password" autoFocus value={token} onChange={(event) => setToken(event.target.value)} onKeyDown={(event) => event.key === "Enter" && void submit()} /></label>{error && <p className="error">{error}</p>}<footer><button className="secondary" onClick={close}>取消</button><button className="primary" disabled={busy} onClick={() => void submit()}>{busy ? "登录中…" : "登录"}</button></footer></section></div>; }
function Inspector({ call, recent, debug, close }: { call: Call | null; recent: Call[]; debug: Debug; close: () => void }) { return <aside className="inspector"><header><div><p className="eyebrow">DEVELOPER</p><h2>API Inspector</h2></div><button className="icon" onClick={close}>×</button></header><p>Every visible result uses the live backend. {debug === "off" ? "Core fields only." : "Request and response details are available."}</p>{call ? <><div className="inspector-call"><Status text={String(call.status || "ERR")} tone={call.error ? "danger" : "success"} /><b>{call.method}</b><code>{call.path}</code><small>{call.latency} ms {call.requestId ? `· ${call.requestId}` : ""}</small></div>{call.error && <p className="error">{call.error}</p>}{debug !== "off" && <details open><summary>Request</summary><pre>{pretty(call.body ?? "No body")}</pre></details>}<details open={debug === "verbose"}><summary>Response</summary><pre>{pretty(call.response ?? "No response")}</pre></details></> : <Empty text="Run an operation to inspect its real request and response." />}{recent.length > 1 && <div className="history"><p className="eyebrow">RECENT</p>{recent.slice(1, 7).map((item, index) => <div key={`${item.at}-${index}`}><span>{item.status || "ERR"}</span><code>{item.method} {item.path}</code></div>)}</div>}</aside>; }
function Developer(p: any) { const [schema, setSchema] = useState<any>(null); const [method, setMethod] = useState("POST"); const [path, setPath] = useState("/api/v1/papers/search"); const [body, setBody] = useState('{\n  "query": "electrolyte",\n  "filters": {},\n  "page": 1,\n  "page_size": 20\n}'); const [result, setResult] = useState<any>(null); const run = async () => { try { setResult(await p.api(method, path, { body: body.trim() ? JSON.parse(body) : undefined })); } catch (error) { setResult({ error: message(error) }); } }; return <div className="drawer-layer"><aside className="developer-drawer"><header><div><p className="eyebrow">RAW API ACCESS</p><h2>Developer Drawer</h2><p>{p.base}</p></div><button className="icon" onClick={p.close}>×</button></header><p className="notice">This sends a real backend request. Multipart uploads stay in Paper Library.</p><div className="developer-form"><select value={method} onChange={(event) => setMethod(event.target.value)}><option>GET</option><option>POST</option></select><input value={path} onChange={(event) => setPath(event.target.value)} /><textarea rows={11} value={body} onChange={(event) => setBody(event.target.value)} /><div><button className="secondary" onClick={async () => setSchema(await p.api("GET", "/openapi.json"))}>Load OpenAPI</button><button className="primary" onClick={run}>Run live request</button></div></div>{schema && <details><summary>OpenAPI paths ({Object.keys(schema.paths || {}).length})</summary><pre>{pretty(Object.keys(schema.paths || {}))}</pre></details>}{result && <details open><summary>Response</summary><pre>{pretty(result)}</pre></details>}</aside></div>; }
function ImagePreview({ url, token, alt }: { url: string; token: string; alt: string }) { const [src, setSrc] = useState(""); const [error, setError] = useState(""); useEffect(() => { let current = ""; setSrc(""); setError(""); void fetch(url, { headers: token ? { Authorization: `Bearer ${token}` } : undefined }).then(async (response) => { if (!response.ok) { const payload = await response.json().catch(() => null); throw new Error(errorOf(payload, response.status)); } return response.blob(); }).then((blob) => { current = URL.createObjectURL(blob); setSrc(current); }).catch((reason) => setError(message(reason))); return () => { if (current) URL.revokeObjectURL(current); }; }, [token, url]); if (error) return <p className="error">Asset preview unavailable from backend: {error}</p>; return src ? <img className="asset-image" src={src} alt={alt} onError={() => setError("The downloaded response is not a valid image.")} /> : <p className="muted">Loading asset preview…</p>; }
function Reference(p: any) { const endpoints = [["POST", "/api/v1/papers/search", "Search catalog metadata.", { query: "electrolyte", filters: {}, page: 1, page_size: 20 }], ["POST", "/api/v1/evidence/search", "Retrieve candidate Evidence IDs. Scope uses scope.paper_ids.", { query: "Which electrolyte is non-flammable?", scope: { paper_ids: ["paper_id"] }, filters: {}, page: 1, page_size: 10 }], ["GET", "/api/v1/evidence/{evidence_id}/content", "Expand a selected hit to surrounding context before citation.", "?before=1200&after=2400"], ["POST", "/api/v1/evidence/packs", "Queue a verified evidence pack for explicit claims.", { claims: [{ text: "A non-flammable electrolyte improves stability." }], scope: { paper_ids: ["paper_id"] } }], ["POST", "/api/v1/citations/ground", "Queue draft grounding and citation insertion.", { draft: "A non-flammable electrolyte improves stability.", scope: { paper_ids: ["paper_id"] } }], ["GET", "/api/v1/workflows/{run_id}", "Poll a queued workflow until it succeeds or fails.", "No body"]]; return <section className="page"><Heading title="API Reference" action={<button className="primary" onClick={() => p.setShowDeveloper(true)}>Open live playground</button>} /><div className="reference-flow"><span>Claims / draft</span><b>→</b><span>Workflow run</span><b>→</b><span>Verified evidence / citations</span></div><div className="api-list">{endpoints.map(([method, path, when, example]: any[]) => <article key={path}><div><Status text={method} tone={method === "GET" ? "info" : "warning"} /><code>{path}</code></div><p>{when}</p><pre>{pretty(example)}</pre></article>)}</div></section>; }
function Heading({ title, action }: { title: string; action?: any }) { return <div className="heading"><h2>{title}</h2>{action}</div>; }
function SectionTitle({ title, action }: { title: string; action?: any }) { return <div className="section-title"><h3>{title}</h3>{action}</div>; }
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

