import { useCallback, useEffect, useState } from "react";

import PipelinePanel from "./admin/PipelinePanel";

type Props = { base: string };
type Tab = "overview" | "pipelines" | "processing" | "repairs" | "queues" | "batches" | "models" | "harvest" | "attempts" | "alerts" | "slo";
type Role = "embedding" | "structured" | "llm" | "vision" | "rerank";
const ROLES: Role[] = ["embedding", "structured", "llm", "vision", "rerank"];
const DEFAULT_MODELS: Record<Role, string> = {
  embedding: "text-embedding-v4", structured: "qwen-plus", llm: "qwen-plus",
  vision: "qwen-vl-plus", rerank: "qwen3-rerank",
};

export default function AdminView({ base }: Props) {
  const [token, setToken] = useState("");
  const [tab, setTab] = useState<Tab>("overview");
  const [sloWindow, setSloWindow] = useState("24h");
  const [data, setData] = useState<any>({});
  const [selectedBatch, setSelectedBatch] = useState("");
  const [batchItems, setBatchItems] = useState<any[]>([]);
  const [selectedProvider, setSelectedProvider] = useState<any>(null);
  const [keys, setKeys] = useState<any[]>([]);
  const [keysLoading, setKeysLoading] = useState(false);
  const [keysLoadError, setKeysLoadError] = useState("");
  const [harvestCredentials, setHarvestCredentials] = useState<any[]>([]);
  const [harvestQuota, setHarvestQuota] = useState<any>({});
  const [harvestAlias, setHarvestAlias] = useState("");
  const [harvestApiKey, setHarvestApiKey] = useState("");
  const [showProvider, setShowProvider] = useState(false);
  const [retryPreview, setRetryPreview] = useState<any>(null);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("输入 Admin Token 后连接运维后台。");
  const [form, setForm] = useState({
    role: "embedding" as Role, base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    model: DEFAULT_MODELS.embedding, keys: "",
  });

  const call = useCallback(async (path: string, method = "GET", body?: unknown) => {
    const response = await fetch(`${base.replace(/\/$/, "")}/api/v1/admin${path}`, {
      method,
      headers: { Authorization: `Bearer ${token}`, ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok || result.error) {
      const detail = result.error?.message || result.detail?.message || result.detail || result.error;
      throw new Error(typeof detail === "string" ? detail : `HTTP ${response.status}`);
    }
    return result;
  }, [base, token]);

  const refresh = useCallback(async () => {
    if (!token) return setNotice("请先输入 Admin Token。");
    setBusy("refresh");
    try {
      const [overview, processing, queues, providers, repairs, events, attempts, embeddingRebuild, alerts, slo, pipelineOverview, pipelines] = await Promise.all([
        call("/overview"), call("/processing?page_size=100"), call("/queues"), call("/providers"),
        call("/embedding-repairs"),
        call("/provider-events?page_size=30"), call("/attempts?page_size=30"), call("/embedding-rebuild"), call("/alerts?page_size=30"), call(`/slo?window=${sloWindow}`),
        call("/pipeline/overview"), call("/pipelines?page_size=100"),
      ]);
      setData({ overview, processing, repairs, queues: queues.items || [], providers: providers.items || [], secretsReady: true, events: events.items || [], attempts: attempts.items || [], batches: overview.recent_batches || [], embeddingRebuild: embeddingRebuild.task, alerts: alerts.items || [], slo, pipelineOverview, pipelines: pipelines.items || [] });
      setNotice("运维数据已更新。");
    } catch (error: any) { setNotice(error.message || "Admin 请求失败。"); }
    finally { setBusy(""); }
  }, [call, token, sloWindow]);

  useEffect(() => {
    if (!token) return;
    void refresh();
    const id = window.setInterval(() => { if (!document.hidden) void refresh(); }, 15000);
    return () => window.clearInterval(id);
  }, [refresh, token]);

  const loadHarvest = useCallback(async () => {
    if (!token) return;
    try {
      const [credentials, quota] = await Promise.all([
        call("/harvest/openalex/credentials"),
        call("/harvest/openalex/quota"),
      ]);
      setHarvestCredentials(credentials.items || []);
      setHarvestQuota(quota || {});
    } catch (error: any) { setNotice(error.message); }
  }, [call, token]);

  useEffect(() => {
    if (tab === "harvest") void loadHarvest();
  }, [loadHarvest, tab]);

  const loadBatch = async (batchId: string) => {
    setSelectedBatch(batchId);
    try { setBatchItems((await call(`/import-batches/${batchId}/items?page_size=100`)).items || []); }
    catch (error: any) { setNotice(error.message); }
  };

  const openProvider = async (provider: any) => {
    setSelectedProvider(provider);
    setKeys([]);
    setKeysLoadError("");
    setKeysLoading(true);
    try {
      const result = await call(`/providers/${provider.role}/keys`);
      if (provider.source === "database" && result.source !== "database") {
        throw new Error("密钥池接口未返回数据库配置。请重启后端服务后重试。");
      }
      setKeys(result.items || []);
    } catch (error: any) {
      const message = error.message || "密钥池加载失败。";
      setKeysLoadError(message);
      setNotice(message);
    } finally { setKeysLoading(false); }
  };

  const manageProvider = (provider: any) => {
    setForm({ role: provider.role, base_url: provider.base_url, model: provider.model, keys: "" });
    setShowProvider(true);
    void openProvider(provider);
  };

  const saveProvider = async () => {
    const modelChanged = form.role === "embedding" && selectedProvider?.source === "database" && selectedProvider.model !== form.model;
    const firstEmbedding = form.role === "embedding" && (!selectedProvider || selectedProvider.source === "unconfigured");
    if (modelChanged && !window.confirm(`将 embedding 模型从 ${selectedProvider.model} 切换为 ${form.model}，这会停止当前重建（如有）、禁用语义检索并重新构建全库向量。确定继续吗？`)) return;
    if (firstEmbedding && !window.confirm(`将 ${form.model} 设为全库 embedding 模型。以后切换模型需要重新构建全库向量。确定继续吗？`)) return;
    const keyLines = form.keys.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    const submittedKeys = keyLines.map((line, index) => {
      const comma = line.indexOf(",");
      return comma > 0
        ? { alias: line.slice(0, comma).trim(), api_key: line.slice(comma + 1).trim(), weight: 1, max_concurrency: 5 }
        : { alias: `key-${index + 1}`, api_key: line, weight: 1, max_concurrency: 5 };
    });
    setBusy("provider");
    try {
      const result = await call(`/providers/${form.role}/setup`, "PUT", { provider: "dashscope", base_url: form.base_url, model: form.model, enabled: true, timeout_seconds: form.role === "rerank" ? 2 : 60, breaker_threshold: 5, breaker_cooldown_seconds: 60, keys: submittedKeys, confirm_embedding_model_change: modelChanged });
      setShowProvider(false); setForm((value) => ({ ...value, keys: "" }));
      await refresh(); setTab("models"); setNotice(result.rebuild_task_id ? `模型已切换，正在执行全库向量重建（${result.rebuild_task_id}）。` : submittedKeys.length ? `模型配置已保存，已接管 ${submittedKeys.length} 把密钥。` : "模型配置已保存。");
    } catch (error: any) { setNotice(error.message); }
    finally { setBusy(""); }
  };

  const testKey = async (credentialId: string) => {
    setBusy(`test-${credentialId}`);
    try {
      const result = await call(`/provider-keys/${credentialId}/test`, "POST");
      setNotice(result.healthy ? `测试通过，耗时 ${result.latency_ms} ms。` : `测试失败${result.status_code ? `，HTTP ${result.status_code}` : ""}。`);
      if (selectedProvider) await openProvider(selectedProvider);
    } catch (error: any) { setNotice(error.message); }
    finally { setBusy(""); }
  };

  const addProviderKey = async (alias: string, apiKey: string) => {
    if (!selectedProvider) return;
    setBusy("keys");
    try {
      await call(`/providers/${selectedProvider.role}/keys`, "POST", { alias, api_key: apiKey, weight: 1, max_concurrency: 5 });
      await openProvider(selectedProvider);
      await refresh();
      setNotice("密钥已加入控制台密钥池。");
    } catch (error: any) { setNotice(error.message); }
    finally { setBusy(""); }
  };

  const toggleKey = async (item: any) => {
    try { await call(`/provider-keys/${item.credential_id}/${item.status === "active" ? "disable" : "enable"}`, "POST"); await openProvider(selectedProvider); await refresh(); }
    catch (error: any) { setNotice(error.message); }
  };

  const updateKeyConcurrency = async (item: any, maxConcurrency: number) => {
    if (!Number.isInteger(maxConcurrency) || maxConcurrency < 1 || maxConcurrency > 100) {
      return setNotice("单 Key 并发必须是 1–100 的整数。");
    }
    setBusy(`key-limit-${item.credential_id}`);
    try {
      await call(`/provider-keys/${item.credential_id}`, "PATCH", {
        max_concurrency: maxConcurrency,
      });
      await openProvider(selectedProvider);
      await refresh();
      setNotice(`已将 ${item.alias} 的并发上限设为 ${maxConcurrency}。`);
    } catch (error: any) { setNotice(error.message); }
    finally { setBusy(""); }
  };

  const addHarvestCredential = async () => {
    if (!harvestAlias.trim() || !harvestApiKey.trim()) return;
    setBusy("harvest-key");
    try {
      await call("/harvest/openalex/credentials", "POST", {
        alias: harvestAlias.trim(),
        api_key: harvestApiKey.trim(),
        weight: 1,
      });
      setHarvestAlias("");
      setHarvestApiKey("");
      await loadHarvest();
      setNotice("OpenAlex API Key 已加密保存并读取了当前免费额度。");
    } catch (error: any) { setNotice(error.message); }
    finally { setBusy(""); }
  };

  const testHarvestCredential = async (credentialId: string) => {
    setBusy(`harvest-test-${credentialId}`);
    try {
      await call(`/harvest/credentials/${credentialId}/test`, "POST");
      await loadHarvest();
      setNotice("OpenAlex API Key 检测通过，额度信息已刷新。");
    } catch (error: any) { setNotice(error.message); }
    finally { setBusy(""); }
  };

  const toggleHarvestCredential = async (item: any) => {
    try {
      await call(`/harvest/credentials/${item.credential_id}/${item.status === "disabled" ? "enable" : "disable"}`, "POST");
      await loadHarvest();
    } catch (error: any) { setNotice(error.message); }
  };

  const queueAction = async (name: string, paused: boolean) => {
    if (!paused && !window.confirm(`暂停 ${name} 的新任务？运行中的任务会继续。`)) return;
    try { await call(`/queues/${name}/${paused ? "resume" : "pause"}`, "POST"); await refresh(); }
    catch (error: any) { setNotice(error.message); }
  };

  const taskAction = async (id: string, action: "retry" | "cancel" | "reconcile") => {
    if (action === "cancel" && !window.confirm(`取消任务 ${id}？`)) return;
    setBusy(`${action}-${id}`);
    try {
      const result = await call(`/tasks/${id}/${action}`, "POST");
      setNotice(action === "reconcile" ? `已完成任务核对：恢复 ${result.recovered?.length || 0} 个失联任务。` : `任务 ${id} 已${action === "retry" ? "创建新的完整处理流程" : "取消"}。`);
      if (selectedBatch) await loadBatch(selectedBatch);
      await refresh();
    }
    catch (error: any) { setNotice(error.message); }
    finally { setBusy(""); }
  };

  const restartEmbeddingRebuild = async () => {
    if (!window.confirm("将按当前 embedding 模型从头重新构建全库向量。确定继续吗？")) return;
    setBusy("embedding-rebuild");
    try { const result = await call("/embedding-rebuild/restart", "POST"); setNotice(`已启动全库向量重建（${result.task_id}）。`); await refresh(); }
    catch (error: any) { setNotice(error.message); }
    finally { setBusy(""); }
  };

  const retryInterrupted = async (dryRun: boolean, includeNonRetryable = false) => {
    if (!dryRun && !window.confirm(`重试 ${retryPreview?.retry_count || 0} 个失败或中断任务？运行中的任务不会受影响。`)) return;
    setBusy("bulk-retry");
    try {
      const result = await call("/tasks/retry-failed", "POST", { dry_run: dryRun, include_non_retryable: includeNonRetryable, confirm: !dryRun && includeNonRetryable });
      setRetryPreview(result);
      setNotice(dryRun
        ? `可重试 ${result.retry_count} 个任务；跳过 ${result.skipped_active_count} 个已有活跃任务。`
        : `已创建 ${result.retry_count} 个完整处理流程；恢复 ${result.recovered_task_ids?.length || 0} 个失联任务。`);
      if (!dryRun) await refresh();
    } catch (error: any) { setNotice(error.message); }
    finally { setBusy(""); }
  };

  const repairEmbeddings = async (paperIds: string[], stopFirst = false) => {
    if (!paperIds.length) return;
    if (!window.confirm(stopFirst ? "将安全停止当前向量化，并在其结束后仅补齐缺失向量。确定继续吗？" : `将仅补齐 ${paperIds.length} 篇论文的缺失或过期向量，健康向量不会重算。确定继续吗？`)) return;
    setBusy("embedding-repair");
    try {
      const result = await call(`/embedding-repairs${stopFirst ? "/stop-and-retry" : ""}`, "POST", { paper_ids: paperIds });
      setNotice(stopFirst ? `已请求安全停止；${result.task_ids?.length || 0} 个向量恢复任务会在旧请求结束后开始。` : `已创建 ${result.created || 0} 个仅向量恢复任务。`);
      await refresh();
    } catch (error: any) { setNotice(error.message); }
    finally { setBusy(""); }
  };

  const acknowledgeAlert = async (alertId: string) => {
    const note = window.prompt("确认备注（可选）：") || undefined;
    setBusy(`ack-${alertId}`);
    try { await call(`/alerts/${alertId}/ack`, "POST", { note }); await refresh(); setNotice("告警已确认，恢复后将自动关闭。"); }
    catch (error: any) { setNotice(error.message); }
    finally { setBusy(""); }
  };

  const metrics = data.overview?.provider_24h || {};
  const tabLabels: Record<Tab, string> = { overview: "总览", pipelines: "流水线", processing: "处理任务", repairs: "向量恢复", queues: "队列与 Worker", batches: "导入记录", models: "模型与密钥", harvest: "OpenAlex 采集", attempts: "调用审计", alerts: "告警", slo: "SLO" };
  return <section className="page admin-page">
    <div className="admin-heading"><div><p className="eyebrow">OPERATIONS CONTROL</p><h2>开发者管理后台</h2></div><div className="admin-auth"><input type="password" value={token} onChange={(event) => setToken(event.target.value)} placeholder="管理员令牌" /><button className="primary" disabled={!token || busy === "refresh"} onClick={refresh}>{busy === "refresh" ? "连接中…" : "连接并刷新"}</button></div></div>
    <div className={`admin-notice ${data.secretsReady === false ? "warning" : ""}`}>{notice}</div>
    <nav className="admin-tabs">{(["overview", "pipelines", "alerts", "slo", "processing", "repairs", "models", "harvest", "queues", "batches", "attempts"] as Tab[]).map((item) => <button key={item} className={tab === item ? "active" : ""} onClick={() => setTab(item)}>{tabLabels[item]}</button>)}</nav>
    {tab === "overview" && <div className="admin-stack"><div className="admin-metrics"><Metric label="AI Ready" value={data.processing?.counts?.succeeded || 0} /><Metric label="处理中" value={(data.processing?.counts?.queued || 0) + (data.processing?.counts?.running || 0)} /><Metric label="失败" value={(data.processing?.counts?.failed || 0) + (data.processing?.counts?.interrupted || 0)} /><Metric label="健康 Worker" value={(data.overview?.workers || []).filter((item: any) => item.healthy).length} /></div><Band title="失败流程重试"><div className="admin-row"><p>{retryPreview ? `当前失败 ${retryPreview.candidate_count} 篇；本次可重试 ${retryPreview.retry_count} 篇；历史任务已排除 ${retryPreview.historical_task_count} 条。` : "外部暂态错误可一键重试；数据或配置错误需修复原因后，再确认全部重试。"}</p><span className="admin-actions"><button className="secondary" disabled={busy === "bulk-retry"} onClick={() => retryInterrupted(true)}>预览外部失败</button>{retryPreview?.retry_count > 0 && !retryPreview.include_non_retryable && <button className="primary" disabled={busy === "bulk-retry"} onClick={() => retryInterrupted(false)}>重试外部失败</button>}<button className="secondary" disabled={busy === "bulk-retry"} onClick={() => retryInterrupted(true, true)}>预览全部失败</button></span></div>{retryPreview && <Table headers={["论文", "阶段", "错误", "可重试", "原因"]} rows={(retryPreview.items || []).map((item: any) => [item.title || item.paper_id, processStage(item.stage), item.error_code || "-", item.selected ? "是" : "否", item.reason])} />}</Band><Band title="Provider 错误统计"><div className="admin-bars">{Object.entries(metrics.errors || {}).map(([name, count]: any) => <div key={name}><span>{name}</span><i style={{ width: `${Math.min(100, Number(count) * 8)}%` }} /><b>{count}</b></div>)}</div></Band></div>}
    {tab === "pipelines" && <PipelinePanel overview={data.pipelineOverview || {}} pipelines={data.pipelines || []} call={call} refresh={refresh} setNotice={setNotice} />}
    {tab === "alerts" && <div className="admin-stack"><Band title="运维告警"><p className="muted">告警在后台持续评估；确认仅表示已接手，连续两次健康检查后会自动关闭。</p><Table headers={["级别", "状态", "告警", "次数", "最近发生", "操作"]} rows={(data.alerts || []).map((item: any) => [item.severity, item.status, <span><b>{item.summary}</b><small>{item.rule}</small></span>, item.occurrence_count, formatTime(item.last_seen_at), item.status === "active" ? <button className="primary" disabled={busy === `ack-${item.alert_id}`} onClick={() => acknowledgeAlert(item.alert_id)}>确认</button> : item.acknowledged_note || "-"])} /></Band></div>}
    {tab === "slo" && <div className="admin-stack"><Band title="服务目标"><div className="admin-row"><p className="muted">按当前窗口汇总业务读取 API 与论文处理的 error budget。</p><select value={sloWindow} onChange={(event) => setSloWindow(event.target.value)}><option value="24h">24 小时</option><option value="30d">30 天</option></select></div><Table headers={["目标", "目标值", "当前值", "样本", "失败", "剩余预算", "状态"]} rows={(data.slo?.objectives || []).map((item: any) => [item.key, `${item.target}%`, item.value == null ? "暂无数据" : `${item.value}%`, item.total, item.failed, `${item.error_budget_remaining}%`, item.status])} /></Band></div>}
    {tab === "processing" && <div className="admin-stack"><Band title="当前论文处理"><div className="admin-row"><p>每篇论文只显示最新完整流程。向量恢复成功后会标为“已恢复”，不会再提供冗余的全文重处理。</p><span className="admin-actions">{retryPreview?.include_non_retryable && retryPreview.retry_count > 0 && <button className="primary" disabled={busy === "bulk-retry"} onClick={() => retryInterrupted(false, true)}>重试全部失败</button>}<button className="secondary" disabled={busy === "bulk-retry"} onClick={() => retryInterrupted(true, true)}>刷新失败预览</button></span></div><Table headers={["论文", "状态", "阶段", "进度", "尝试", "错误", "更新时间", "操作"]} rows={(data.processing?.items || []).map((item: any) => [<span><b>{item.title || item.paper_id}</b><small>{item.paper_id}</small></span>, processStatus(item.effective_status || item.status), processStage(item.stage), `${Math.round((item.progress || 0) * 100)}%`, `${item.attempt || 0}/${item.max_attempts || "-"}`, item.effective_status === "recovered" ? "向量恢复成功" : item.error_code || (item.error_message ? item.error_message.slice(0, 90) : "-"), item.updated_at, item.manual_retryable ? <button className="secondary" disabled={busy === `retry-${item.task_id}`} onClick={() => taskAction(item.task_id, "retry")}>重新处理全文</button> : "-"])} /></Band></div>}
    {tab === "repairs" && <div className="admin-stack"><Band title="向量恢复"><div className="admin-row"><p>仅补齐缺失或文本已变化的 Embedding；健康向量不会被重新调用。外部故障自动重试一次，之后等待人工恢复。</p><button className="primary" disabled={busy === "embedding-repair"} onClick={() => repairEmbeddings((data.repairs?.items || []).filter((item: any) => item.pending_count > 0).map((item: any) => item.paper_id))}>重试所有不健康项</button></div><p className="muted">当前模型：{data.repairs?.model || "-"}。请先在“模型与密钥”中检测异常密钥；熔断或配额问题修复后再发起恢复。</p><Table headers={["论文", "状态", "健康/待补", "批次", "错误", "下次重试", "操作"]} rows={(data.repairs?.items || []).map((item: any) => [<span><b>{item.title || item.paper_id}</b><small>{item.paper_id}</small></span>, repairStatus(item), `${item.healthy_count}/${item.pending_count}`, item.progress ? `${item.progress.batch || 0}/${item.progress.total_batches || 0}` : "-", item.error_code || item.error_message || "-", formatTime(item.next_retry_at), <span className="admin-actions">{item.can_stop_and_retry ? <button className="danger" disabled={busy === "embedding-repair"} onClick={() => repairEmbeddings([item.paper_id], true)}>停止并重新 Embed</button> : <button className="primary" disabled={busy === "embedding-repair" || item.pending_count === 0} onClick={() => repairEmbeddings([item.paper_id])}>立即重新 Embed</button>}<button className="secondary" onClick={() => setTab("attempts")}>查看调用审计</button></span>])} /></Band></div>}
    {tab === "models" && <div className="admin-stack"><div className="admin-row"><div><h3>模型类型与密钥</h3><p className="muted">每把 API Key 独立配置并发上限；健康 Key 的容量自动累加，不设置 Provider 全局并发上限。</p></div><button className="primary" onClick={() => { setSelectedProvider(null); setKeys([]); setKeysLoadError(""); setForm({ role: "embedding", base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: DEFAULT_MODELS.embedding, keys: "" }); setShowProvider(true); }}>+ 配置类型</button></div>{data.embeddingRebuild && <Band title="全库向量重建"><p>仅用于模型切换或全局修复，不用于单篇向量故障。{data.embeddingRebuild.details?.target_model || "-"} · {processStatus(data.embeddingRebuild.status)} · {Math.round((data.embeddingRebuild.progress || 0) * 100)}% · 已完成 {data.embeddingRebuild.children?.succeeded || 0}/{data.embeddingRebuild.children?.total || 0}</p>{!["succeeded", "failed", "cancelled"].includes(data.embeddingRebuild.status) ? <button className="danger" onClick={() => taskAction(data.embeddingRebuild.task_id, "cancel")}>停止重建</button> : <button className="primary" disabled={busy === "embedding-rebuild"} onClick={restartEmbeddingRebuild}>继续重建</button>}</Band>}<Table headers={["类型", "模型", "Base URL", "密钥来源", "健康容量", "状态", "操作"]} rows={(data.providers || []).map((item: any) => [item.role, item.model, item.base_url || "-", item.source === "database" ? `${item.healthy_key_count}/${item.key_count} 把健康` : "尚未配置", item.source === "database" ? `${item.current_leases}/${item.healthy_max_concurrency}` : "未配置", item.enabled ? "已启用" : "未配置", <button className="secondary" onClick={() => manageProvider(item)}>管理</button>])} /></div>}
    {tab === "harvest" && <HarvestCredentialPanel credentials={harvestCredentials} quota={harvestQuota} alias={harvestAlias} apiKey={harvestApiKey} busy={busy} setAlias={setHarvestAlias} setApiKey={setHarvestApiKey} add={addHarvestCredential} test={testHarvestCredential} toggle={toggleHarvestCredential} refresh={loadHarvest} />}
    {tab === "queues" && <div className="admin-stack"><Band title="队列状态"><Table headers={["队列", "用途", "DB 待投递", "Redis 排队", "执行中", "失败作业", "控制"]} rows={(data.queues || []).map((item: any) => [item.name, queuePurpose(item.name), item.database?.pending || 0, item.queued, item.started, item.failed, <button className="secondary" onClick={() => queueAction(item.name, item.paused)}>{item.paused ? "恢复" : "暂停"}</button>])} /></Band><Band title="Worker"><p className="muted">仅展示有近期心跳的 worker；stale 记录是重启后遗留的 Redis 心跳，不代表正在运行的任务。</p><Table headers={["角色", "状态", "队列", "最后心跳"]} rows={(data.overview?.workers || []).filter((item: any) => item.healthy).map((item: any) => [workerRole(item.queues || []), item.state === "idle" ? "空闲" : item.state, item.queues?.join(", "), item.last_heartbeat || "-"])} /></Band></div>}
    {tab === "batches" && <div className="admin-stack"><Band title="导入记录"><Table headers={["批次", "状态", "导入进度", "失败数", "更新时间", "查看"]} rows={(data.batches || []).map((item: any) => [item.batch_id, processStatus(item.status), `${item.completed_items}/${item.total_items}`, item.failed_items, item.updated_at, <button className="secondary" onClick={() => loadBatch(item.batch_id)}>打开</button>])} /></Band>{selectedBatch && <Band title={`批次项目 · ${selectedBatch}`}><Table headers={["来源", "状态", "阶段", "尝试", "错误", "操作"]} rows={batchItems.map((item: any) => [item.source_key, processStatus(item.status), processStage(item.stage), item.queue?.attempt ?? item.attempts, item.queue?.error_code || item.error_message || "-", item.task_id ? <TaskActions item={item} run={taskAction} /> : "-"])} /></Band>}</div>}
    {tab === "attempts" && <div className="admin-stack"><Band title="处理尝试审计"><Table headers={["对象", "次数", "阶段", "状态", "错误", "开始时间"]} rows={(data.attempts || []).map((item: any) => [item.owner_id, item.attempt, item.stage, item.status, item.error_code || "-", item.started_at])} /></Band><Band title="Provider 调用审计"><Table headers={["类型", "操作", "状态", "错误", "耗时", "时间"]} rows={(data.events || []).map((item: any) => [item.role, item.operation, item.status, item.error_code || "-", `${item.latency_ms} ms`, item.created_at])} /></Band></div>}
    {showProvider && <ProviderModal form={form} setForm={setForm} provider={selectedProvider} keys={keys} keysLoading={keysLoading} keysLoadError={keysLoadError} busy={busy} close={() => setShowProvider(false)} save={saveProvider} addKey={addProviderKey} testKey={testKey} toggleKey={toggleKey} updateKeyConcurrency={updateKeyConcurrency} reloadKeys={() => selectedProvider && openProvider(selectedProvider)} />}
  </section>;
}

function HarvestCredentialPanel({ credentials, quota, alias, apiKey, busy, setAlias, setApiKey, add, test, toggle, refresh }: any) {
  return <div className="admin-stack">
    <div className="admin-metrics">
      <Metric label="可用 Key" value={credentials.filter((item: any) => item.status === "active").length} />
      <Metric label="今日免费余额" value={`$${Number(quota.daily_remaining_usd || 0).toFixed(4)}`} />
      <Metric label="预计可下 PDF" value={quota.estimated_content_files || 0} />
      <Metric label="最近重置时间" value={formatTime(quota.next_reset_at)} />
    </div>
    <Band title="集中添加 OpenAlex API Key">
      <p className="muted">Key 仅用于 OpenAlex 分类、Works 枚举和 Content API；Content 下载只预留 daily free credit，prepaid 余额不会参与调度。</p>
      <div className="key-pool-add">
        <input value={alias} onChange={(event) => setAlias(event.target.value)} placeholder="成员或用途别名" />
        <input type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder="OpenAlex API Key" />
        <button className="primary" disabled={!alias.trim() || !apiKey.trim() || busy === "harvest-key"} onClick={add}>{busy === "harvest-key" ? "检测并保存中…" : "添加 Key"}</button>
        <button className="secondary" onClick={refresh}>刷新额度</button>
      </div>
    </Band>
    <Band title="OpenAlex 采集凭据">
      <Table headers={["别名", "后缀", "状态", "今日余额", "Prepaid（不消费）", "PDF 单价", "重置时间", "最近错误", "操作"]} rows={credentials.map((item: any) => [
        item.alias,
        `••••${item.key_suffix}`,
        item.status,
        item.daily_remaining_usd == null ? "—" : `$${Number(item.daily_remaining_usd).toFixed(4)}`,
        item.prepaid_remaining_usd == null ? "—" : `$${Number(item.prepaid_remaining_usd).toFixed(4)}`,
        `$${Number(item.content_cost_usd || .01).toFixed(4)}`,
        formatTime(item.resets_at),
        item.last_error_code || "—",
        <span className="admin-actions"><button className="secondary" disabled={busy === `harvest-test-${item.credential_id}`} onClick={() => test(item.credential_id)}>检测</button><button className="secondary" onClick={() => toggle(item)}>{item.status === "disabled" ? "启用" : "停用"}</button></span>,
      ])} />
    </Band>
  </div>;
}

function ProviderModal({ form, setForm, provider, keys, keysLoading, keysLoadError, busy, close, save, addKey, testKey, toggleKey, updateKeyConcurrency, reloadKeys }: any) {
  const requiresInitialKey = !provider || provider.source === "unconfigured";
  return <div className="modal-layer"><section className="channel-modal"><header><div><p className="eyebrow">模型配置</p><h2>{provider ? `管理 ${provider.role}` : "配置模型类型"}</h2></div><button className="icon" onClick={close}>×</button></header><div className="channel-grid"><label>类型<select disabled={Boolean(provider)} value={form.role} onChange={(event) => { const role = event.target.value as Role; setForm({ ...form, role, model: DEFAULT_MODELS[role] }); }}>{ROLES.map((role) => <option key={role}>{role}</option>)}</select></label><label className="wide">Base URL<input value={form.base_url} onChange={(event) => setForm({ ...form, base_url: event.target.value })} /></label><label className="wide">模型<input value={form.model} onChange={(event) => setForm({ ...form, model: event.target.value })} /></label></div>{provider?.source === "unconfigured" && <p className="inline-note">此类型尚未配置。保存时必须添加至少一把控制台密钥，环境变量密钥不会被读取。</p>}{requiresInitialKey && <label className="field">API 密钥（每行一个；也可使用“别名,key”；默认每把并发 5）<textarea rows={4} value={form.keys} onChange={(event) => setForm({ ...form, keys: event.target.value })} placeholder={'primary,sk-key-one\nbackup,sk-key-two'} /></label>}{provider?.source === "database" && (keysLoading ? <p className="inline-note">正在加载控制台密钥池…</p> : keysLoadError ? <p className="inline-note">密钥池加载失败：{keysLoadError} <button className="secondary" onClick={reloadKeys}>重新加载</button></p> : <ProviderKeys provider={provider} keys={keys} busy={busy} add={addKey} test={testKey} toggle={toggleKey} updateConcurrency={updateKeyConcurrency} />)}<footer><button className="secondary" onClick={close}>取消</button><button className="primary" disabled={busy === "provider" || !form.base_url || !form.model || (requiresInitialKey && !form.keys.trim())} onClick={save}>{busy === "provider" ? "保存中…" : requiresInitialKey ? "保存配置" : "保存配置"}</button></footer></section></div>;
}

function ProviderKeys({ provider, keys, busy, add, test, toggle, updateConcurrency }: any) {
  const [adding, setAdding] = useState(false);
  const [alias, setAlias] = useState("");
  const [apiKey, setApiKey] = useState("");
  const submit = async () => { if (!alias.trim() || !apiKey.trim()) return; await add(alias.trim(), apiKey.trim()); setAlias(""); setApiKey(""); setAdding(false); };
  return <section className="admin-band"><div className="key-pool-heading"><h3>{provider.role} · 控制台密钥池</h3><button className="icon" title="添加密钥" onClick={() => setAdding((value) => !value)}>+</button></div><Table headers={["别名", "密钥后缀", "状态", "健康", "租约/上限", "调用成功/总数", "最近错误", "操作"]} rows={keys.map((item: any) => [item.alias, `••••${item.key_suffix}`, item.status === "active" ? "启用" : "停用", healthLabel(item.health), <span className="key-limit"><input className="key-limit-input" type="number" min={1} max={100} defaultValue={item.max_concurrency} disabled={busy === `key-limit-${item.credential_id}`} onBlur={(event) => { const value = Number(event.target.value); if (value !== item.max_concurrency) void updateConcurrency(item, value); }} /><small>{item.active_leases}/{item.max_concurrency} · 剩余 {item.remaining_capacity}</small></span>, `${item.calls?.success || 0}/${item.calls?.total || 0}`, item.last_error_code || "-", <span className="admin-actions"><button className="secondary" disabled={busy === `test-${item.credential_id}`} onClick={() => test(item.credential_id)}>检测</button><button className="secondary" onClick={() => toggle(item)}>{item.status === "active" ? "停用" : "启用"}</button></span>])} />{adding && <div className="key-pool-add"><input autoFocus placeholder="密钥别名" value={alias} onChange={(event) => setAlias(event.target.value)} /><input type="password" placeholder="API key（默认并发 5）" value={apiKey} onChange={(event) => setApiKey(event.target.value)} /><button className="secondary" onClick={() => setAdding(false)}>取消</button><button className="primary" disabled={!alias.trim() || !apiKey.trim() || busy === "keys"} onClick={submit}>{busy === "keys" ? "添加中…" : "添加"}</button></div>}</section>;
}

function Metric({ label, value }: { label: string; value: string | number }) { return <div><span>{label}</span><strong>{value}</strong></div>; }
function Band({ title, children }: { title: string; children: any }) { return <section className="admin-band"><h3>{title}</h3>{children}</section>; }
function Table({ headers, rows }: { headers: string[]; rows: any[][] }) { return <div className="admin-table" style={{ "--cols": headers.length } as any}><div className="admin-tr admin-th">{headers.map((header) => <b key={header}>{header}</b>)}</div>{rows.map((row, index) => <div className="admin-tr" key={index}>{row.map((value, cell) => <span key={cell}>{value ?? "-"}</span>)}</div>)}</div>; }
function healthLabel(value: string | undefined) { return ({ healthy: "健康", unverified: "未检测", unhealthy: "异常", cooldown: "冷却中", disabled: "已停用" } as Record<string, string>)[value || ""] || value || "-"; }
function formatTime(value: string | undefined) { return value ? new Date(value).toLocaleString() : "-"; }
function processStatus(value: string | undefined) { return ({ queued: "排队中", running: "处理中", retrying: "等待重试", succeeded: "已完成", recovered: "已恢复", failed: "失败", interrupted: "已中断", superseded: "已替代", cancelled: "已取消" } as Record<string, string>)[value || ""] || value || "-"; }
function processStage(value: string | undefined) { return ({ parsing: "解析", evidence: "构建证据", multimodal: "资源上下文", embedding: "向量化", embedding_recovered: "向量恢复完成", indexing: "建立索引", queued: "等待", waiting_for_cancel: "等待旧任务停止", waiting_manual: "等待人工恢复", succeeded: "完成", failed: "失败", interrupted: "已中断", superseded: "已替代" } as Record<string, string>)[value || ""] || value || "-"; }
function repairStatus(item: any) { if (item.stage === "waiting_manual") return "等待人工恢复"; if (item.stage === "waiting_for_cancel") return "等待旧任务停止"; return processStatus(item.status); }
function queuePurpose(value: string) { return ({ "literature-interactive": "检索与引用工作流", "literature-ingest": "暂存文件注册", "literature-maintenance": "维护与调度", "literature-remote-fetch": "远程解析与下载", "literature-parse": "MinerU 解析", "literature-content": "Evidence 与资源上下文", "literature-embedding": "向量化 API", "literature-index": "本地索引" } as Record<string, string>)[value] || "系统任务"; }
function workerRole(queues: string[]) { const value = queues[0] || ""; return queuePurpose(value); }
function TaskActions({ item, run }: { item: any; run: (id: string, action: "retry" | "cancel" | "reconcile") => Promise<void> }) { if (item.status === "succeeded") return <>-</>; return <span className="admin-actions"><button className="secondary" onClick={() => run(item.task_id, "reconcile")}>检查失联任务</button>{item.status === "failed" ? <button className="secondary" onClick={() => run(item.task_id, "retry")}>重新处理全文</button> : <button className="danger" onClick={() => run(item.task_id, "cancel")}>取消</button>}</span>; }
