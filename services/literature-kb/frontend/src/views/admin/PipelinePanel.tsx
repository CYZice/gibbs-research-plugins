import { useState } from "react";

type Props = {
  overview: any;
  pipelines: any[];
  call: (path: string, method?: string, body?: unknown) => Promise<any>;
  refresh: () => Promise<void>;
  setNotice: (value: string) => void;
};

const STAGES = [
  "remote_fetch",
  "register",
  "parse",
  "evidence",
  "resource_context",
  "vectorize",
  "index",
];

export default function PipelinePanel({
  overview,
  pipelines,
  call,
  refresh,
  setNotice,
}: Props) {
  const [selected, setSelected] = useState<any>(null);
  const [busy, setBusy] = useState("");

  const open = async (taskId: string) => {
    setBusy(`open-${taskId}`);
    try {
      setSelected(await call(`/pipelines/${taskId}/stages`));
    } catch (error: any) {
      setNotice(error.message || "流水线详情加载失败。");
    } finally {
      setBusy("");
    }
  };

  const act = async (
    path: string,
    message: string,
    confirm?: string,
  ) => {
    if (confirm && !window.confirm(confirm)) return;
    setBusy(path);
    try {
      await call(path, "POST");
      setNotice(message);
      await refresh();
      if (selected?.root?.task_id) await open(selected.root.task_id);
    } catch (error: any) {
      setNotice(error.message || "流水线操作失败。");
    } finally {
      setBusy("");
    }
  };

  const totals = Object.values(overview?.stage_counts || {}).reduce(
    (result: Record<string, number>, statuses: any) => {
      Object.entries(statuses || {}).forEach(([status, count]) => {
        result[status] = (result[status] || 0) + Number(count);
      });
      return result;
    },
    {},
  );

  return (
    <div className="admin-stack pipeline-workspace">
      <div className="admin-metrics">
        <Metric label="数据库待投递" value={sum(overview?.queue_backlog)} />
        <Metric label="运行 / 等待外部" value={(totals.running || 0) + (totals.waiting_external || 0)} />
        <Metric label="超时待处理" value={overview?.overdue || 0} />
        <Metric label="过期租约" value={(overview?.expired_delivery_leases || 0) + (overview?.expired_run_leases || 0)} />
      </div>

      <section className="pipeline-stage-strip" aria-label="流水线阶段概览">
        {STAGES.map((stage, index) => {
          const counts = overview?.stage_counts?.[stage] || {};
          const active = (counts.running || 0) + (counts.waiting_external || 0);
          return (
            <div key={stage} className={active ? "active" : ""}>
              <span>{String(index + 1).padStart(2, "0")}</span>
              <b>{stageLabel(stage)}</b>
              <small>{counts.pending || 0} 待处理 · {active} 活跃 · {counts.failed || 0} 失败</small>
            </div>
          );
        })}
      </section>

      <div className="pipeline-split">
        <section className="pipeline-list">
          <header>
            <div>
              <h3>最近流水线</h3>
              <p>每篇论文一条 root；点击查看阶段尝试和租约。</p>
            </div>
            <div className="pipeline-controls">
              <span className={overview?.enabled ? "pipeline-flag on" : "pipeline-flag"}>
                V2 {overview?.enabled ? "ON" : "OFF"}
              </span>
              <button
                className="secondary"
                disabled={Boolean(busy) || !overview?.enabled}
                onClick={() => act(
                  `/pipeline/intake/${overview?.intake_paused ? "resume" : "pause"}`,
                  overview?.intake_paused ? "批量导入入口已恢复。" : "批量导入入口已暂停。",
                  overview?.intake_paused
                    ? undefined
                    : "暂停后不再接受新批次和追加项目，现有流水线继续运行。确定继续吗？",
                )}
              >
                {overview?.intake_paused ? "恢复 Intake" : "暂停 Intake"}
              </button>
            </div>
          </header>
          {pipelines.map((item) => (
            <button
              key={item.task_id}
              className={`pipeline-row ${selected?.root?.task_id === item.task_id ? "selected" : ""}`}
              onClick={() => open(item.task_id)}
              disabled={busy === `open-${item.task_id}`}
            >
              <span className={`pipeline-dot ${item.status}`} />
              <span>
                <b>{item.title || item.paper_id || "等待远程登记"}</b>
                <small>{item.batch_id || "单篇导入"} · generation {item.pipeline_generation}</small>
              </span>
              <span>
                <b>{stageLabel(item.stage)}</b>
                <small>{Math.round((item.progress || 0) * 100)}%</small>
              </span>
              <span className={`pipeline-state ${item.status}`}>{statusLabel(item.status)}</span>
            </button>
          ))}
        {!pipelines.length && <p className="empty">尚无 Pipeline 任务。</p>}
        </section>

        <section className={`pipeline-detail ${selected ? "visible" : ""}`}>
          {!selected ? (
            <div className="pipeline-detail-empty">
              <span>→</span>
              <p>选择一条流水线查看严格阶段顺序、尝试记录和恢复操作。</p>
            </div>
          ) : (
            <>
              <header>
                <div>
                  <p className="eyebrow">PIPELINE TIMELINE</p>
                  <h3>{selected.root.title || selected.root.paper_id || selected.root.task_id}</h3>
                  <small>{selected.root.task_id}</small>
                </div>
                <div className="admin-actions">
                  <button
                    className="secondary"
                    disabled={Boolean(busy)}
                    onClick={() => act(`/pipelines/${selected.root.task_id}/reconcile`, "流水线状态已重新聚合。")}
                  >
                    Reconcile
                  </button>
                  {!["succeeded", "cancelled"].includes(selected.root.status) && (
                    <button
                      className="danger"
                      disabled={Boolean(busy)}
                      onClick={() => act(
                        `/pipelines/${selected.root.task_id}/cancel`,
                        "已请求取消流水线。",
                        "取消后不会创建后续阶段，正在执行的外部调用会在返回后停止推进。确定继续吗？",
                      )}
                    >
                      取消
                    </button>
                  )}
                </div>
              </header>
              <div className="pipeline-timeline">
                {(selected.stages || []).map((stage: any) => (
                  <article key={stage.task_id} className={stage.status}>
                    <i />
                    <div>
                      <header>
                        <b>{stageLabel(stage.operation)}</b>
                        <span>{statusLabel(stage.status)}</span>
                      </header>
                      <p>{stage.queue_name || "数据库状态"} · action {stage.next_action || "-"}</p>
                      <small>
                        attempt {stage.attempt_count}/{stage.max_attempts} · worker {stage.worker_id || "-"}
                      </small>
                      {stage.error_code && <em>{stage.error_code} · {stage.error_message}</em>}
                      {(stage.attempts || []).length > 0 && (
                        <details>
                          <summary>{stage.attempts.length} 次执行记录</summary>
                          {(stage.attempts || []).map((attempt: any) => (
                            <p key={`${attempt.attempt}-${attempt.started_at}`}>
                              #{attempt.attempt} {statusLabel(attempt.status)} · {formatTime(attempt.started_at)}
                              {attempt.error_code ? ` · ${attempt.error_code}` : ""}
                            </p>
                          ))}
                        </details>
                      )}
                      {["failed", "cancelled"].includes(stage.status) && (
                        <button
                          className="secondary"
                          disabled={Boolean(busy)}
                          onClick={() => act(
                            `/pipeline-stages/${stage.task_id}/retry`,
                            `已从 ${stageLabel(stage.operation)} 重新启动流水线。`,
                            `将从 ${stageLabel(stage.operation)} 重跑，并使其下游结果失效。确定继续吗？`,
                          )}
                        >
                          从此阶段重试
                        </button>
                      )}
                    </div>
                  </article>
                ))}
              </div>
            </>
          )}
        </section>
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: number }) {
  return <div><span>{label}</span><strong>{value}</strong></div>;
}

function sum(values: Record<string, number> | undefined) {
  return Object.values(values || {}).reduce((total, value) => total + Number(value), 0);
}

function stageLabel(value: string | undefined) {
  return ({
    remote_fetch: "远程下载",
    register: "注册 Paper",
    parse: "MinerU 解析",
    evidence: "Evidence",
    resource_context: "资源上下文",
    vectorize: "向量化",
    index: "本地索引",
    succeeded: "完成",
    cancelling: "取消中",
  } as Record<string, string>)[value || ""] || value || "-";
}

function statusLabel(value: string | undefined) {
  return ({
    pending: "待投递",
    running: "运行中",
    waiting_external: "等待外部",
    retry_wait: "等待重试",
    retrying: "等待重试",
    succeeded: "已完成",
    skipped: "已跳过",
    failed: "失败",
    superseded: "已失效",
    cancelling: "取消中",
    cancelled: "已取消",
  } as Record<string, string>)[value || ""] || value || "-";
}

function formatTime(value: string | undefined) {
  return value ? new Date(value).toLocaleString() : "-";
}
