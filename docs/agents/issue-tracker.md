# Issue tracker：GitHub

本仓库的 Issues 和规格文档位于 GitHub 仓库 `ZJH-K/RuleCourt`。所有操作均使用 `gh` CLI。如果本地尚未配置 Git remote，请显式传入 `--repo ZJH-K/RuleCourt`。

## 约定

- **创建 Issue**：`gh issue create --repo ZJH-K/RuleCourt --title "..." --body "..."`
- **查看 Issue**：`gh issue view <number> --repo ZJH-K/RuleCourt --comments`，并同时获取 labels。
- **列出 Issues**：`gh issue list --repo ZJH-K/RuleCourt --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'`，并按需设置 label 和状态筛选条件。
- **评论 Issue**：`gh issue comment <number> --repo ZJH-K/RuleCourt --body "..."`
- **添加或移除 label**：使用 `gh issue edit <number> --repo ZJH-K/RuleCourt --add-label "..."` 或 `--remove-label "..."`。
- **关闭 Issue**：`gh issue close <number> --repo ZJH-K/RuleCourt --comment "..."`

本地 checkout 配置 GitHub remote 后，`gh` 可以自动识别仓库，也可以省略 `--repo` 参数。

## PR 是否作为需求入口

**PR 是否接受需求：否。**

只有当仓库以后决定将外部 Pull Request 视为功能需求时，才将此项改为 `yes`。GitHub Issues 和 PR 共用编号，因此单独写 `#42` 可能指向其中任意一种。

## 当 Skill 要求“发布到任务跟踪系统”时

在 `ZJH-K/RuleCourt` 创建 GitHub Issue。

## 当 Skill 要求“获取相关任务”时

运行 `gh issue view <number> --repo ZJH-K/RuleCourt --comments`。

## Wayfinding 操作

总任务地图是添加了 `wayfinder:map` label 的一个 Issue，其余任务是子 Issue。

- 使用 `gh issue create` 创建地图和子任务。
- 优先使用 GitHub sub-issues；如果不可用，则在任务清单中链接子项，并在每个子 Issue 中添加 `Part of #<map>`。
- 使用 `wayfinder:research`、`wayfinder:prototype`、`wayfinder:grilling` 或 `wayfinder:task` 标记子任务类型。
- 使用 GitHub 原生 Issue 依赖关系表示阻塞关系。如果原生功能不可用，在子任务开头写明 `Blocked by: #<n>`。
- 使用 `gh issue edit <number> --add-assignee @me` 认领任务。
- 任务完成后，在 Issue 中评论结果、关闭子 Issue，并在总任务地图中记录上下文链接。