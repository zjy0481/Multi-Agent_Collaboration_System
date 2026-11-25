# T016 GitHub 交付验收

2026 年 11 月 25 日，使用 `c797938b25249a08c404c03b4d85425c419d1815` 完成一次真人批准的 GitHub 推送。平台动作终态为 `succeeded`。GitHub REST 独立读取的目标 ref 与获准 SHA 一致。 [T016 验收条件](../plans/agent-collaboration/revisions/r0003/tickets/T016.md#acceptance)

| 项目 | 验收结果 |
| --- | --- |
| 测试仓库 | `zjy0481/MACS_testing` |
| 目标 ref | `refs/heads/test/t016-github-delivery-20261125` |
| 获准及实读 SHA | [751417bdd8bcfd880b51acefdbc9d68740dba823](https://github.com/zjy0481/MACS_testing/commit/751417bdd8bcfd880b51acefdbc9d68740dba823) |
| 测试提交 | 单个根提交，只包含一行验收文本 `t016-delivery-proof.txt` |
| 审批 ID | `acbbb7147701444e85e6f6889d682cae` |
| 动作 ID 及原工具调用 | `e8d7ec0e4a084dc5be426ae3fd604819` · `t016-request-push` |

## 执行记录

1. 平台创建测试提交并通过 `request_push` 进入待审批。Git HTTPS 和 GitHub REST 均确认目标 ref 此时尚未创建。
2. 用户在协调会话确认本次推送，协调者通过平台操作者 API 记录批准，得到 HTTP 200。审批 worker 将同一动作持久领取为 `executing`，随后向 GitHub 执行一次 HTTPS 推送。
3. 推送退出码为 0。worker 用 Git HTTPS 读回目标 ref，得到获准 SHA。原执行会话收到 `git.result`，并经 `get_git_action` 得到 `succeeded`，回执可追溯到原始调用、审批 ID 和动作 ID。
4. GitHub REST 独立读回同一 SHA。验收前后分别读取测试仓库的 `refs/heads/main`，两次均为 `2b3aae80c22ea5c10570f5ecefbec843f08a9965`。

## 验证范围

隔离验收使用基线的调度器、Docker 工作区、提交工具、审批 API、SQLite 持久领取和审批 worker。确定性测试模型产生工具调用。首次验收通过 `create_app(git_executor=...)` 注入固定仓库和 ref 的 GitHub HTTPS 传输。正式启动入口的后续实测见下节。 [执行器](../../collaboration_runtime/git_executor.py) · [测试仓库配置](../environment-setup.md)

Windows 宿主上，T011 的 5 项回归通过，覆盖拒绝、参数不匹配、审批重放及未知结果复核。命令沙箱运行同组用例时，有 3 项推送相关失败。SSE 返回 `snapshot_required`，本轮从隔离 SQLite 只读导出待批和结果事件。 [Git 审批测试](../../tests/test_git_approvals.py) · [执行器测试](../../tests/test_git_executor.py)

本机原始证据位于 `.tmp/t016-github-delivery-20261125/`，其中 `human-approval-receipt.json`、`status-current.json`、`github-rest-current.json` 分别保存真人决定、动作及工具回执、独立远端读取。工作台进程已退出，监听端口已释放，验收容器数为 0，保留 4 个隔离工作卷供取证。

## 正式启动入口复验

2026 年 11 月 25 日，使用 `collaboration_runtime.api:app` 启动独立工作台。通过 `COLLABORATION_GITHUB_TARGET` 配置一个 GitHub 仓库及完整 ref，由宿主已登录的 `gh` 提供凭据。试验使用确定性模型申请推送一个合成文件。在 Chrome 点击一次「批准此提交」，审批 API 返回 `approved`，worker 执行推送并核对远端，原执行会话收到 `succeeded`。 [正式配置](../environment-setup.md#github-推送)

- 目标 `zjy0481/MACS_testing` 的 `refs/heads/test/t016-workbench-20261125` 在批准前不存在。GitHub REST 独立确认该 ref 指向获准提交。 [实测提交](https://github.com/zjy0481/MACS_testing/commit/6361abf87fbfc10ae3d147577dc556acb4750068)
- 真实 GitHub 实测覆盖首次创建分支。后续快进、拒绝审批、参数不匹配和远端 SHA 不符由本地可重复测试覆盖。实测后的修订区分了明确拒绝和推送结果未知，两种状态以及重启后的只读核对均已通过本地回归。 [GitHub 交付测试](../../tests/test_github_delivery.py)
- 原始浏览器批准、动作和远端证据位于本机 `.tmp/t016-workbench-github-20261125/`。工作台和验收浏览器均已退出。

## 自动任务分支复验

2026 年 11 月 25 日，使用正式入口启动独立工作台，GitHub 配置只指定仓库和别名。固定测试模型通过三个关联回合各创建一个测试文件。用户在工作台逐次批准，平台自动创建 `refs/heads/macs/task-87d8f74bdeb04e81a525cfc92fd15e0b`，随后两次更新同一分支。三个动作均为 `succeeded`，每次获准 SHA 都与平台读回和独立 GitHub REST 核对结果一致。

| 顺序 | 获准及实读提交 | 操作 |
| --- | --- | --- |
| 1 | [`c13577c`](https://github.com/zjy0481/MACS_testing/commit/c13577cd42da1346ef04e4b35039d138bd30b562) | 创建远端分支 |
| 2 | [`5e32474`](https://github.com/zjy0481/MACS_testing/commit/5e324743e58556a7a762f77af4da5bf5c0d123c8) | 更新同一分支 |
| 3 | [`fd2186d`](https://github.com/zjy0481/MACS_testing/commit/fd2186d4e7416e054dccf6a02e686f3fcef0ec7e) | 再次更新同一分支 |

测试分支从 `main` 的 `2b3aae8` 创建，之后增加三个连续提交。原始审批与远端核对证据位于本机 `.tmp/task-branch-three-live-20261125/`。 [GitHub 分支对比](https://github.com/zjy0481/MACS_testing/compare/2b3aae80c22ea5c10570f5ecefbec843f08a9965...fd2186d4e7416e054dccf6a02e686f3fcef0ec7e)
