# T16 M0 正式发布

`rulecourt.release` 是对 T11–T15 产物执行最终证据核验的门禁。它不会改变 Case 产品，也不会增加第二条裁决路径。

## 根据开发集冻结配置

先完成 family 划分和开发集试跑。使用 `FrozenExperimentConfig.freeze_from_development(...)` 冻结预算、澄清与事实请求上限、重复次数、统计方法、最小质量增益、错误放行约束、模型/策略版本、规则资料、私有覆盖表版本和缓存条件。生成的配置应为 `run_kind = formal`，包含已冻结的 `InvestigationBudget`，并绑定精确的数据集摘要。系统会拒绝只有保留集或开发集划分不完整的配置。

`examples/t16-freeze-config.json` 是有意留空部分字段的模板，不能作为正式配置使用。

## 正式回放与报告

使用同一份独立的 `HumanSignoff` 和已冻结配置，运行 T12/T13 基线报告及 T14 实验组报告。T12/T13 正式 runner 只有在 `configuration_status = frozen` 时才接受保留集案例；其正式模式默认选择保留集。T14 提供相同的 `holdout_only` 选项，并保留数据泄漏与确定性门禁。

根据这些报告构建并签署发布产物：

```python
from rulecourt.release import ReleaseBuilder

release = ReleaseBuilder(frozen_config).build(
    dataset,
    baseline_reports=[t13_report],
    treatment_reports=[t14_report],
    signoff=human_signoff,
    signing_key=release_key,
)
release.save_json("t16-release.json")
```

报告包含五组结果（`llm_only`、`vanilla_rag`、`hybrid_rag`、`dynamic_agent`、`fixed_workflow`）、首次与完整调查指标、分类指标、Dynamic/Fixed 配对结果、资源使用、保留产物摘要、family 与重复次数、复现步骤、限制，以及人工核验记录引用。重复运行使用不同的运行 ID，但按唯一 Case 计数；canonical family 单独报告，不能视为相互独立的样本。

可使用 CLI 查看报告，无需重新调用模型：

```powershell
$env:RULECOURT_T16_RELEASE_KEY = "<protected release key>"
uv run rulecourt-release report t16-release.json --format markdown
```

结论根据已冻结策略计算，取值之一为 `quality_gain`、`efficiency_gain`、`insufficient_evidence` 或 `unproven_gain`。回放后修改阈值会使配置摘要和签名失效。即使实验未能证明 Agent 带来增益，完整交付实验仍然有效。

## 公开回归与限制

现有公开 FastAPI 入口和浏览器视图仍作为回归边界：公开 API 测试会覆盖 Case 创建、状态、事件/调查，以及当前和历史裁决。T16 不增加游戏能力，也不重新设计界面。示例规则和候选 Case 尚未核验；规则审阅、Golden Case 签核、provider 账单完整性和已观察到的模型限制，仍是明确的前置条件或限制。