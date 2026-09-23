# RuleCourt M0

本 M0 切片保存 Case 和公开调查事件。启用经过人工核验的 Root 规则资料包后，固定工作流可对 Marquise 普通移动作出 LEGAL、ILLEGAL 或 INSUFFICIENT_INFORMATION 裁决；超出支持范围的行动和缺少规则证据的情况返回 UNRESOLVED。

## 项目与任务入口

- [M0 设计与任务地图](https://github.com/ZJH-K/RuleCourt/issues/1)是 GitHub Issues 中的项目入口。[所有开放任务](https://github.com/ZJH-K/RuleCourt/issues)在各自验收条件满足前保持开放。
- 审查资料包括[候选规则资料包](examples/root-m0-candidate-package.json)、[候选 Case](examples/m0-candidate-cases.json)和[正式发布指南](docs/t16-release.md)。
- [五组开发实验快照](experiments/2026-09-23/README.md)记录了对 13 个草稿 Case 的真实 provider 调用。这些结果**仅用于开发阶段**：规则资料包和 Case 尚未经过独立人工签核，Case 未按 family 划分保留集，T14/T16 正式证据也尚不完整。
- [草稿审查 PR #18](https://github.com/ZJH-K/RuleCourt/pull/18)包含候选来源修正和 AI 试跑 attestation。AI 试跑标签不能替代正式 Golden Case 核验标签。

下一步验收顺序为：[规则人工审查 #4](https://github.com/ZJH-K/RuleCourt/issues/4)、[产品与回放的剩余检查 #8](https://github.com/ZJH-K/RuleCourt/issues/8)、[#9](https://github.com/ZJH-K/RuleCourt/issues/9)和[#12](https://github.com/ZJH-K/RuleCourt/issues/12)、[Case 独立核验与 family 划分 #16](https://github.com/ZJH-K/RuleCourt/issues/16)、[双 provider 配对比较 #15](https://github.com/ZJH-K/RuleCourt/issues/15)，最后是[正式冻结与验收 #17](https://github.com/ZJH-K/RuleCourt/issues/17)。基线任务[#13](https://github.com/ZJH-K/RuleCourt/issues/13)和[#14](https://github.com/ZJH-K/RuleCourt/issues/14)已有开发集试跑，但尚未完成正式验收。

## 启动

需要安装 [uv](https://docs.astral.sh/uv/)。nanobot 依赖在 pyproject.toml 中固定到 HKUDS/nanobot@12a9a8a692c7e8fc6d3d293afdddcdaa390436df，并记录在 uv.lock 中。

将 [.env.example](.env.example) 复制为本地忽略文件 .env，按所用流程填写 provider key 和 API Host。不要提交 .env。

```powershell
uv sync --extra dev
uv run uvicorn rulecourt.api:app --reload --env-file .env
```

打开 <http://127.0.0.1:8000/>。默认数据库为 rulecourt.sqlite3。设置 RULECOURT_DB 可指定其他路径；设置 OPENAI_BASE_URL 可指定兼容的 provider endpoint。Case 页面可通过 URL 重新打开。Provider 只会收到受限的公开调查工具，模型生成的文本不能直接授权裁决。

## 检查

```powershell
uv run pytest tests/test_case_api.py -q
uv run basedpyright rulecourt
uv run ruff check rulecourt tests
uv run ruff format --check rulecourt tests
uv run pytest -q
```

## T02 规则审阅

打开 <http://127.0.0.1:8000/rules> 进入本地维护界面。该界面可以导入版本化规则资料包并标为 draft，记录带审阅人和依据的 verified 或 disputed 审查状态，并且只允许启用已经审阅且核验通过的资料包。公开规则可通过 /api/rules 搜索；私有覆盖义务只在资料包审阅界面返回。

启动 server 前，在本地忽略文件 .env 中设置 RULECOURT_MAINTENANCE_TOKEN。在审阅页面输入该值即可使用维护操作。密钥仅保留在页面内存中，并通过 X-RuleCourt-Maintenance-Token header 发送。未配置 token 时，维护 API 返回 503；token 不正确时返回 403。公开规则搜索仍可读取。本地审阅记录用于 T02 演示；独立来源核验和正式人工签核属于 T03。

未核验的演示资料包位于
[examples/root-m0-candidate-package.json](examples/root-m0-candidate-package.json)。
其中固定引用了官方 [2025 年 10 月《Law of Root》PDF](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756)，但候选转述和资料包内部 ID 仍需 T03 人工审查。

## T05 固定普通移动工作流

启用涵盖 Root 规则 2.2.1、2.5、4.2 和 4.2.1 的已核验资料包后，在 Case 中提交完整的 Marquise 普通移动描述。结果会记录确定性 Decision、Verification、规则证据、裁决范围以及未检查的条件。Eyrie 移动和完整回合合法性不在此任务范围内。

## T06 澄清与恢复

当当前事实不足以确定局部移动结果时，Case 返回 INSUFFICIENT_INFORMATION，并给出 missing_fields 和确定性的 clarification_questions。向同一 Case 补充所需事实即可重新运行工作流。消息、状态证据、修订、裁决历史，以及 clarification_requested / clarification_resumed 事件都可以查询。

## T09 Dynamic Agent 策略

创建 Case 时可选择 Automatic、Fixed Workflow 或 Dynamic Agent。API 使用相同选项，例如 {"strategy":"dynamic_agent"}；另外两个值为 auto 和 fixed_workflow。Dynamic Agent 可以搜索并检查已核验的公开规则、提出带来源的状态更新、解析公开规则关系、模拟行动并提交确定性结果。覆盖义务和规划提示保持私有；只有 Controller 能记录 LEGAL 或 ILLEGAL 裁决。

## T10 调查预算

Controller 对 Dynamic Agent 和固定工作流的调查使用同一试跑预算。可通过 create_app(..., budget=...) 或环境变量 RULECOURT_MAX_DURATION_SECONDS、RULECOURT_MAX_ITERATIONS、RULECOURT_MAX_TOOL_CALLS 和 RULECOURT_MAX_TOTAL_TOKENS 配置。默认值标记为 development_trial，不属于已冻结的评测阈值。

Case 响应会公开累计用量、剩余预算、provider/model、工具失败、时延和停止原因。澄清后恢复调查以及从可恢复的 provider 故障中重试时，会继续使用同一调查台账；提交新消息不会重置预算。

## T11 评测回放

rulecourt.evaluation 将候选 Case 标签和事实保存在被测应用之外。EvaluationRunner 只通过注入的 adapter 发送初始输入和明确请求的事实；score_results 分别报告首次结果和完整调查指标。草稿和有争议的 Case 会保留在报告中，但不纳入正式计分。

验证候选 fixture 或查看已导出的报告：

```powershell
uv run rulecourt-eval validate examples/m0-candidate-cases.json
uv run rulecourt-eval report evaluation-report.json --format markdown
```

仓库中的候选 fixture 有意保持 draft 状态；它是试跑数据集，不是经过独立核验的 Golden Case 集。

## T15 Golden Case 人工核验与签核

正式计分必须经过独立人工审查。每个核验通过的 Case 都必须记录核验人、固定规则版本、核验时间、证据，以及对标签、范围、事实可用性、证据、允许提出的问题和独立标签来源的全部必要检查。修正和歧义处理保留在 Case 历史中；有争议的 Case 记录争议原因并排除在正式计分之外。

正式门禁还要求覆盖部分信息、未知、事实撤回、Eyrie、Bird 和不支持的交互。人工核验人可以说明理由并明确豁免某项覆盖要求，但豁免必须记录在独立签核产物中，不能放进候选数据集。该产物绑定精确的数据集摘要，并由受保护的 RULECOURT_GOLDEN_SIGNOFF_KEY 生成 HMAC 签名。

发布计分清单前，先按 family 确定性划分数据：

~~~python
dataset.split_by_family(holdout_fraction=0.2, seed=7)
dataset.save_json("golden-cases-v1.json")
~~~

人工审查完成后，写入独立的 HumanSignoff JSON，记录批准的 Case ID、核验人 ID、覆盖核验或豁免声明及签名。缺少该产物、摘要/签名不匹配或未按 family 划分时，清单命令会 fail closed。命令只导出 Case/family ID 和审查元数据，不导出 gold label 或隐藏事实：

~~~powershell
$env:RULECOURT_GOLDEN_SIGNOFF_KEY = "<protected key>"
uv run rulecourt-eval validate golden-cases-v1.json --formal --signoff golden-signoff.json
uv run rulecourt-eval manifest golden-cases-v1.json --signoff golden-signoff.json
~~~

仓库中的候选 fixture 仍为 draft；回放 runner 不能将其升级，也不能将其作为正式真值计分。

个人开发试跑可以使用单独标记的 AI attestation，并根据精确的候选数据集和规则资料包进行验证：

```powershell
uv run rulecourt-eval validate examples/m0-candidate-cases.json --ai-attestation docs/research/root-m0-ai-attestation.json --rules examples/root-m0-candidate-package.json
```

score_results_ai_trial(...) 可使用该 attestation 对回放结果计分，报告会标明 review_basis: ai_trial。AI payload checksum 可以检测一般性内容漂移，但不能证明签名者身份。正式 --formal 校验仍要求独立人工审查、family 划分和独立签核。

## T12 LLM-only 与 Vanilla Vector RAG 基线

两种基线都使用 T11 回放 runner、事实应答器、澄清上限和离线评分器。LLM-only 获取固定版本的规则资料和用户事实，不接收检索片段。Vanilla Vector RAG 对每条公开规则摘录和累计用户事实生成 embedding，再按 cosine top-k 将匹配内容提供给模型。它不使用词法搜索、reranker、领域引擎或私有覆盖表。保留模型声明的四状态结果；引用来源错误和必需引用 ID 覆盖率分别报告，因此无依据的 LEGAL 仍计为错误放行。引用覆盖率本身不能证明语义支持。

先使用可复现的开发配置进行 dry run：

~~~powershell
uv run rulecourt-baselines examples/m0-candidate-cases.json --rules examples/root-m0-candidate-package.json --config examples/t12-baseline-config.json --no-hybrid --dry-run
~~~

将配置复制到新的实验文件，把生成模型和 embedding 模型占位值替换为固定 provider 版本，并设置 embedding endpoint。生成服务使用 OPENAI_BASE_URL（或 provider 默认值）；两种服务都从环境读取 OPENAI_API_KEY。如需估算费用，在配置中填写每 1,000 token 的价格，然后运行：

~~~powershell
uv run rulecourt-baselines examples/m0-candidate-cases.json --rules examples/root-m0-candidate-package.json --config my-t12-config.json --no-hybrid --output t12-trial-001.json
~~~

添加 --format markdown 可生成易读报告。为保留既有运行记录，runner 会拒绝覆盖已有输出文件。BaselineReport.from_json(path).to_markdown() 可将已保存的 JSON 报告渲染为 Markdown，无需重新调用 provider。runner 类也提供 run_case 和 run_dataset，可单独运行任一策略；使用异步 provider 时，结束后调用 close()。

报告会保留首次和完整调查结果、每轮检索、来源文本、规则 ID、语料库/索引摘要、模型/配置/代码指纹，以及累计生成和 embedding 用量。索引构建成本计入首次构建该索引的 Case；后续 Case 复用索引。用户等待时间不计入系统时延。超时或请求失败可能产生未上报的计费；用量或价格缺失时，总成本显示为 null/N/A，并单独报告已知用量。token 预算依据已报告用量执行，因此输入 token 超限要等响应返回后才能发现，并记为预算失败。

这些都属于开发试跑：runner 会拒绝保留集，模型提示和检索内容中不会出现任何标签。附带规则与 Case 只是未经核验的演示资料，合成测试不能代替人工签核。仅凭两个外部基线不能证明 Dynamic Agent 的规划价值。

## T13 Hybrid RAG + Reranker 基线

默认 runner 现在加入第三个外部基线。Hybrid RAG 使用确定性的 BM25 风格词法搜索和 T12 向量索引检索公开规则摘录，通过 reciprocal-rank fusion 合并重复 ID，然后只把合并后的候选项交给配置好的 reranker 排序。Reranker 分数只用于排列证据，不能当作权威规则含义、领域计算或覆盖表判定。检索结果为空或检索失败时，作为正常的 UNRESOLVED 观察结果返回，而不是信息追问。

使用仓库中的 T13 配置进行可复现的 dry run，然后把生成、embedding 和 reranker 模型占位值替换为固定版本：

~~~powershell
uv run rulecourt-baselines examples/m0-candidate-cases.json --rules examples/root-m0-candidate-package.json --config examples/t13-hybrid-baseline-config.json --dry-run
uv run rulecourt-baselines examples/m0-candidate-cases.json --rules examples/root-m0-candidate-package.json --config my-t13-config.json --output t13-trial-001.json
~~~

三种策略的首次及完整调查都由同一个 EvaluationRunner 和确定性事实应答器驱动。T13 报告会保留词法/向量候选 ID、融合和 reranker 分数、索引与模型版本、每轮检索、reranker 调用和 token 用量，以及累计成本。如需估算费用，设置 reranker_cost_per_1k_tokens 和 T12 的价格配置。使用 --no-hybrid 可运行原有的 T12 两组基线。

## T14 Dynamic Agent 与 Fixed Workflow treatment 对比

T14 让 Dynamic Agent 和 Fixed Workflow 两组使用相同的回放契约、事实应答器、规则集、领域 Controller、可见信息投影和预算配置。固定路线和模板是经过版本管理、由人工编写的配置；不会根据覆盖义务或诊断路线生成固定流程。Dynamic Agent 的所有 provider 用量都计入累计资源；只有 provider 明确提供规划专用计数器时，报告才单独列出规划用量。

先对仓库中的开发试跑方案进行 dry run：

~~~powershell
uv run rulecourt-compare examples/m0-candidate-cases.json --config examples/t14-treatment-config.json --dry-run
~~~

实际比较时注入两个 CaseAdapter 实例。每个 adapter 都必须声明完整的共享契约，实现 configure_budget，并提供 get_case / get_events 投影，其中包含权威的策略、版本和预算事件。Fixed Workflow 事件还必须标明人工编写的模板、路线来源以及已禁用 LLM 路由；缺少审计观察值时，该配对无效。FastAPICaseAdapter 支持对本地 FastAPI TestClient 实例使用这份契约：

~~~python
import json

from rulecourt.comparison import TreatmentComparisonRunner, TreatmentConfig
from rulecourt.evaluation import FastAPICaseAdapter

config = TreatmentConfig.model_validate(json.load(open("my-t14-config.json")))
shared = config.shared_contract
runner = TreatmentComparisonRunner({
    "dynamic_agent": FastAPICaseAdapter(
        dynamic_client,
        strategy="dynamic_agent",
        metadata={
            **shared,
            "strategy_version": config.dynamic_strategy_version,
        },
    ),
    "fixed_workflow": FastAPICaseAdapter(
        fixed_client,
        strategy="fixed_workflow",
        metadata={
            **shared,
            "strategy_version": config.fixed_workflow_version,
            "fixed_route_source": config.fixed_route_source,
            "fixed_template_version": config.fixed_template_version,
            "fixed_workflow_llm_routing": False,
        },
    ),
}, config=config)
report = runner.run_dataset(
    dataset,
    determinism_adapters={
        "provider-a/model-a": {...},
        "provider-b/model-b": {...},
    },
)
report.save_json("t14-trial-001.json")
~~~

runner 对两组应用同一 InvestigationBudget。报告配对比较首次和完整结果，记录每个 Case 的资源差值，分别报告两组的正确裁决率、错误放行率和覆盖率，并保留用于数据泄漏审计的公开上下文/工具/日志投影。只有 provider 提供规划专用计数器时才单独报告规划用量；Dynamic Agent 的累计用量和成本始终计入。

报告会保留无效配对，但不将其计入主比较。开发试跑会拒绝保留集。正式回放还要求预算已冻结、数据划分和覆盖核验通过、具备独立人工签核、至少两组模型/provider adapter、完整的资源维度，以及两组完整的用量来源记录。导出正式回放时，同时传入 --signoff 和 --determinism-manifest；manifest 会将每个 provider 或模型身份映射到其 Dynamic 和 Fixed StrategyRunReport 产物及逐 Case 公开观察值。正式 StrategyRunReport 产物会对精确的配置、结果和观察数据生成 HMAC；回放时必须使用受保护的 RULECOURT_GOLDEN_SIGNOFF_KEY 验证签名。

## T16 M0 正式冻结

rulecourt.release 将 T12/T13 外部基线和 T14 Dynamic Agent / Fixed Workflow treatment 汇总为一个带签名的正式发布结果。在运行保留集前，先根据开发集冻结所有预算、澄清和事实请求上限、重复次数、统计方法、最小质量增益、错误放行约束、模型/策略版本、规则集/覆盖表版本及缓存条件。examples/t16-freeze-config.json 是刻意不完整的模板；复现步骤见 [docs/t16-release.md](docs/t16-release.md)。

发布报告包含首次与完整调查结果、分类及配对的 Dynamic/Fixed 指标、资源、产物摘要、family/重复次数、审查引用和限制，并给出以下结论之一：quality_gain、efficiency_gain、insufficient_evidence 或 unproven_gain。重复运行和 canonical family 使用唯一样本分母。签名配置摘要可防止事后修改阈值来覆盖正式失败结果。查看已有产物：

~~~powershell
$env:RULECOURT_T16_RELEASE_KEY = "<protected release key>"
uv run rulecourt-release report t16-release.json --format markdown
~~~