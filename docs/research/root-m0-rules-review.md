# Root M0 规则：官方来源核对

状态：用于实现准备的来源比对，**不是人工核验或签核**。本文于 2026-09-23 对照 Leder Games 的 [《The Law of Root》，2025 年 10 月 13 日版](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756)完成核对。该版本也列在其[官方资源页](https://ledergames.com/pages/resources)上。这是一份有日期的 PDF；候选资料包使用的在线规则库链接没有固定到该 PDF 版本。

## 相关官方规则

| 章节 | 对局部 Move 判定的发现 |
| --- | --- |
| [1.1.1](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756#page=2) | 卡牌可以覆盖本规则书；阵营规则与通用规则不兼容时，以适用的阵营规则为准。单独引用基础移动条文不能证明行动完全合法。 |
| [2.1, 2.1.1](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756#page=3) | 第 2.1 节讲的是**卡牌**，不是林地。Bird 卡可以替代其他花色。 |
| [2.2, 2.2.1, 2.3](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756#page=3) | 林地通过路径相连；两块林地之间有路径相连时，它们相邻。除非规则另有明确说明，河流不算路径。此版本中没有名为 Path 的独立 2.2 规则。 |
| [2.5](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756#page=3) | 该规则比较每位玩家的兵和建筑总数。Token 和 pawn 不计入总数；普通平局时，该林地无人统治。 |
| [4.2, 4.2.1](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756#page=5) | 一次 Move 至少移动该玩家的一个 warrior 和/或 pawn，沿连接路径前往相邻林地；除适用例外外，该玩家必须统治起点、终点或两者。 |
| [6.5, 6.5.2](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756#page=6) | Marquise 通过 Daylight 的 March action 获得移动机会，该行动最多允许两次 Move。局部移动几何条件成立，不能证明玩家当前可以执行 March action。 |
| [7.2.2](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756#page=6) | 当 Eyrie 的兵和建筑总数并列最高，且林地中至少有一个 Eyrie 棋子时，Eyrie 也可以统治该林地。此规则不要求林地中必须有 roost。 |
| [7.4.2, 7.5.2, 7.5.2.II](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756#page=7) | Eyrie 在 Birdsong 阶段向 Decree 加入卡牌，最多加入一张新的 bird 卡。Daylight 阶段按列顺序执行 Decree。对于 Move 列卡牌，必须从符合该卡花色的林地移走至少一个 warrior。使用 bird 卡时，根据第 2.1.1 节，起点可以是任意普通花色的林地；Decree 对终点花色没有限制。未完成行动会根据第 7.7 节引发 turmoil。 |

## 原候选资料包中的差异

1. `root-2.1` 将林地/路径内容标为官方第 2.1 节；但该章节讲的是卡牌。`root-2.2` 将 `2.2` 标为 **Path**；官方标题实际为 **Clearings and Paths**，相邻关系在第 2.2.1 节。这些 ID、章节字段、`source_content` 和校验和在声称忠实于来源前都需要修订。[官方第 2.1–2.2.1 节](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756#page=3)
2. `root-2.5` 漏掉普通平局的结果，以及 Token/pawn 不计入兵力和建筑总数这一点。其表述也把来源中的“按玩家比较”改成了“按阵营比较”；在所有情形下，两者并不等价。[官方第 2.5 节](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756#page=3)
3. `root-4.2` 漏掉正数棋子、玩家自己的 pawn，以及必须沿**连接路径**移动。`root-4.2.1` 大致表达了普通规则前提，但将其与 `4.2` 的关系标为 `exception_to` 是错误的：它是前置条件。将通用 `4.2–4.2.1` 标成固有 `daylight` 阶段也夸大了规则书原文；阶段授权来自阵营行动，而非这些章节。[官方第 4.2–4.2.1 节](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756#page=5)
4. `root-7.2.2` 错把 roost 作为 Eyrie 统治的依据。应替换为“并列最高且至少有一个 Eyrie 棋子”的条件，并将其与通用平局规则的关系表示为阵营例外。现有 `clarifies` 关系低估了行为差异。[官方第 2.5、7.2.2 节](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756#page=6)
5. 资料包缺少 `2.1.1` Bird 通配花色规则、`7.5.2.II` Decree Move 规则，以及 `6.5.2` March 行动授权。这些内容与声明的 Marquise/Eyrie 移动范围有关；尤其要注意，不应将 Decree 卡牌的花色限制套在**终点**上。`7.5.2` 是 Resolve the Decree 总章节，`7.5.2.II` 才是其中的 Move 条款。[官方第 2.1.1、6.5.2、7.5.2.II 节](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756#page=7)
6. 标题和 `scope_strategy.name` 声称内容包含“battle excerpts”，但资料包没有 battle 规则，列出的行动也只有 `move`/`rule`。如果 battle 不属于 M0，应删除这项说法。当前 `coverage_obligations` 只检查相邻关系和统治，没有覆盖可移动棋子是否存在、阵营行动授权、Decree 起点花色匹配或例外。在声称覆盖完整 Move 合法性之前，应将这些列为范围限制，或补充相应规则与证据。[官方第 1.1.1、4.2、6.5.2、7.5.2.II 节](https://cdn.shopify.com/s/files/1/0106/0162/7706/files/Root_Base_Law_Oct_2025.pdf?v=1774903756#page=7)

候选资料明确将其摘录标记为未核验。本次核对提供来源定位和建议修正，但不会把规则资料包或任何 Golden Case 转为已核验。正式计分前，仍须由独立领域核验人依据固定版本规则确认转述、解释、覆盖义务和案例答案。

## 为独立审查准备的改动

本地草稿 `examples/root-m0-candidate-package.json` 现在固定到有日期的官方 PDF，将 `2.2.1` 相邻关系与 `2.2.2` 林地花色分开，增加 `2.1.1` Bird 卡牌和 `7.5.2.II` Decree Move，修正 `2.5` 和 `7.2.2` 规则摘要，并将 `6.5.2` 记录为 Marquise 行动可用性的边界。领域判定、adapter、固定工作流和测试中的对应规则 ID 也已更新。该资料包的 `checksum` 对 `source_content` 中的**候选转述文本**取摘要；它不是官方 PDF 的校验和。导入和测试证明的是 schema 与实现一致，不构成独立的语义核验。

`examples/m0-candidate-cases.json` 的版本为 `m0-candidate-v3`，包含 13 个草稿 Case，没有已核验或有争议的 Case。候选修正会在 `history` 中保留旧值：

| Case | 已准备的修正 | 人工核验重点 |
| --- | --- | --- |
| `ordinary-partial-01` | 完整调查标签由 `INSUFFICIENT_INFORMATION` 改为 `ILLEGAL`。 | A 的完整相邻清单只包含 C，因此 A 和 B 之间没有路径。确认该清单的完整性范围有效。 |
| `correction-legal-01` | 首次和完整调查标签由 `LEGAL` 改为 `INSUFFICIENT_INFORMATION`。 | 修正相邻关系，并不能证明有可移动的 warrior 或任一端点由行动者统治。 |
| `eyrie-decree-unsupported-01` | 首次和完整调查标签由 `UNRESOLVED` 改为 `INSUFFICIENT_INFORMATION`。 | 缺少阶段/卡牌背景时，可以在声明的 Eyrie Decree Move 范围内追问。 |
| `eyrie-bird-legal-01` | 新增候选 Bird 卡牌案例草稿。 | 确认双阵营前提、Eyrie 3–3 平局、起点 fox 花色上的 Bird 替代，以及局部范围裁决。 |
| `unsupported-interaction-01` | 新增候选 Marquise Decree 案例草稿。 | 确认这是不支持的阵营/行动组合，而非普通 March Move。 |

`ordinary-legal-01` 和 `eyrie-legal-01` 的输入现在声明双人对局且无例外；其完整标签仍依赖后续补充的棋子数量，以及这些事实确实完整。核验人应确认前提与来源；若不成立，应修正标签或将 Case 标为 disputed。其他草稿也需要逐例审查首次/完整标签、允许的澄清问题及规则证据。任何候选标签都不是正式真值。

当前仍缺少真正的**事实撤回**案例。现有评测 fixture 只有一条 `initial_input` 和一个事实应答器；真实撤回场景需要先有一条事实断言，再在后续消息中撤回，同时保留两个修订及其影响。仅在一句话中提到“撤回”不能算覆盖了此类场景。核验人应要求提供可回放的多轮 fixture，或在独立人工签核中记录有理由的豁免。

批准前，Root 核验人必须对照固定版本 Law 检查每条转述，并确认范围/覆盖义务没有编码案例答案或调查路线。正式计分还需要另一位核验人补齐每个 Case 的来源、标签、核验记录和检查项；解决或隔离争议；建立 family 级开发集/保留集划分；并用受保护的 key 对精确数据集摘要签名。候选资料和本文都不提供核验人身份、批准或签核。

## 第二次 AI 审查，2026-09-23

另一名 AI Agent 独立对照同一份固定版本官方 Law，复核了当前规则草稿和全部 13 个 Case 标签。它报告规则转述或局部范围标签没有实质性冲突，同时发现问题集合定义不足，以及 Eyrie 规则义务的适用条件需要说明。候选内容随后作出以下修订：

- `ordinary-legal-01`、`eyrie-legal-01` 和 `eyrie-bird-legal-01` 现在在公开输入中写明双人对局和无例外前提。Eyrie 示例也在其完整 `LEGAL` 标签前补充了缺失的 Marquise 建筑数量问题。
- `missing-fact-01` 现在分别提供 A 林地的棋子数量，让路径成为预期的局部缺失条件。
- `adversarial-prompt-01` 现在允许询问移动数量、可移动 warrior、相邻关系和端点统治情况。`eyrie-decree-unsupported-01` 现在除了 Decree 背景，也允许询问路径及棋子/统治信息。`correction-legal-01` 现在在公开输入中写明双人对局/无例外前提。
- 资料包将通用移动棋子/统治覆盖与 Eyrie 并列最高例外分开；Bird 关系明确标注为仅在使用 Bird 卡时适用。

这是 **AI 同行审查**，不是人工核验。它没有人工核验人身份，不能独立证明来源或标签正确，也不得修改任何 `draft` 状态。评测器仍无法从单条 `initial_input` 回放真正的多轮撤回案例；该覆盖需要修改回放模型，或取得有记录的人工作出理由的豁免。正式计分门禁仍未开放。

## 个人开发试跑使用的 AI attestation

`docs/research/root-m0-ai-attestation.json` 记录了两名 AI 审查者对精确 canonical 规则资料 JSON 和 `m0-candidate-v3` 数据集摘要得出的结论。文件列出全部 13 个 Case 标签、简要来源推理、已覆盖的 5 个类别，以及尚缺失的撤回类别。其 payload SHA-256 用于比较记录是否漂移，**不能**证明人工身份。`--ai-attestation` 校验和 `score_results_ai_trial` 路径只允许将其用于明确标记的个人开发试跑；它不是正式门禁认可的 `HumanSignoff`。`review.status` 仍为 `draft`，规则资料包也仍未核验；声称已经取得正式人工真值将不准确。