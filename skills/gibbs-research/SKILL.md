---
name: gibbs-research
description: Search and read literature evidence, create and revise scientific plots, and assemble source-linked reports using Gibbs Research tools.
---

# 从文献与数据生成研究报告

1. `gibbs_literature_search` 找候选，用 `gibbs_literature_read` 读取上下文及原文位置，核对研究条件。检索排序不是论断核验；需要批量论断检查时用 `gibbs_literature_verify` 并续接其 workflow。
2. 用 `gibbs_plot_save` 绘制用户数据。保留输入数值、单位、caption 与 provenance；返回 preview_url 供用户查看。不要因美化而改变数据。
3. 用户反馈后，读取原产物，修改图表规格，再传 artifact_id、base_revision、feedback 保存新版本。`gibbs_feedback_pending` 可读取预览页意见；处理后把 feedback_id 传入保存工具。遇到版本冲突，读取最新版本并重新应用用户意见。
4. `gibbs_report_prepare` 接收 title、goal、evidence_ids、显式图表 revision 和 results，返回原文材料及写作契约。使用当前 Agent 写作，不另行运行研究编排器。
5. 文献事实用 `[[cite:ev_N]]`，图表用返回材料中 `figure_id` 的 `[[fig:ID]]`。阅读原文后选择支持对应论断的证据，保留推断与实际结果的区别。未知标记会被拒绝。
6. `gibbs_report_save` 保存草稿并生成预览。检查实际图表与报告，交付下载地址。报告保留文献标识、原文位置以及固定版本图表，后续重画不改变旧报告。

预览页的“交给 Agent 修改”保存反馈，不会自动唤醒空闲会话。用户回到会话后读取待处理反馈并继续。插件只注册工具，不提升会话权限、不自动提交用户项目。
