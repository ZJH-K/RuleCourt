# 五组开发实验 — 2026-09-23

本目录记录对全部 13 个 M0 候选 Case 进行的真实 provider 调用。五组实验使用 DeepSeek `deepseek-flash` 生成文本；Vanilla 和 Hybrid RAG 使用 Qwen `qwen3.7-text-embedding-flash` 生成 embedding；Hybrid RAG 还使用 `qwen3.7-text-rerank`。绑定的 AI attestation 仅支持**开发阶段**计分。所有 Case 和规则资料包仍为草稿。

## 绑定的结果

`five-group-development-snapshot.json` 记录数据集、规则资料包、attestation、运行配置、原始报告及相关代码的 SHA-256 摘要。各组共用的上限为报告 token 数 32,768，单次调查 60 秒。每个 Case 只运行一次，没有保留集划分。

| 组别 | 决策型 Case 中完整调查正确裁决 | 出现系统故障的 Case | 报告 token 数 |
| --- | ---: | ---: | ---: |
| LLM-only | 3/6 | 8/13 | 17,931 |
| Vanilla Vector RAG | 3/6 | 9/13 | 20,803 |
| Hybrid RAG + Reranker | 3/6 | 8/13 | 29,948 |
| Dynamic Agent | 0/6 | 11/13 | 79,621 |
| Fixed Workflow | 4/6 | 0/13 | 0 |

完整调查正确裁决率的分母是 6 个完整标签允许给出 LEGAL 或 ILLEGAL 的 Case，不是全部 13 个 Case。两组 treatment 中有 11/13 对通过审计。`correction-legal-01` 和 `adversarial-conflict-01` 在运行早期出现状态冲突，没有记录运行策略或预算的观察值，因此未纳入配对比较。其余 11 个 Case 中，Dynamic Agent 用尽了 3 次迭代或 8 次工具调用的预算。由于 provider 价格未配置，且部分已计费用量未知，报告的成本不可靠。

基线报告为 `t13-development-results.json`；treatment 报告为 `t14-development-results.json`。`t13-development-plan.json` 和 `t14-development-config.json` 记录确切的运行设置。后续预算调优尝试保存在 `t14-tuned-smoke-*` 和 `t14-budget64-*` 文件中；较高预算的完整尝试只有 3/13 对通过审计，因为 provider 返回响应后有若干结果超过 65,536 token 上限。该次尝试没有替代已绑定的结果。

## 复现

在本地设置 `.env`。精确命令记录在快照的 `reproduction_steps` 字段中。请使用新的输出路径和 T14 标签，因为 runner 会拒绝覆盖已有结果。产物中不包含 API key。

## 正式发布状态

这是一个**已冻结的开发快照**，不是 T16 正式发布。提交的数据集包含 13 个草稿 Case，没有 family 划分或保留集，也没有独立人工 Golden Case 签核。候选规则资料包尚未通过人工核验；`run_t14_trial.py` 只在隔离的本地开发应用中注入该资料包，没有修改生产 RuleStore。正式 T14 还要求通过两个 provider 的确定性证据检查，并补齐完整的用量来源记录。`deepseek-flash` 是会变动的 provider 别名，并非固定的模型快照。满足这些门槛后，才能进行正式的五组冻结并给出结论。