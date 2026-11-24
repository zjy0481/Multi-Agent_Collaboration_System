# T015 模型工具调用优化验证

2026-11-24，以 `dev` 提交 `bf602fa` 为基线。此前 HKGAI 六题运行中，10 次委派查询因为带筛选参数但省略 `compact` 被拒，7 次首次提案撞到已有协商范围，成员也调用过仅用户能用的布置和补充参数。本次用模拟模型和受控参数验证这些调用，Docker 回归使用 `collaboration-sandbox:local`。

| 改动 | 验证结果 |
| --- | --- |
| 委派查询 | 省略 `compact` 时，`goal_id` 或 `limit` 选择紧凑视图。验证无参及显式 `false` 调用保持完整视图，显式 `false` 与筛选组合仍返回 422。 |
| 提案冲突 | 成员能读取已有提案时，冲突返回提案 ID、当前版本和状态。测试把提案推进到第 2 版，验证工具与 HTTP 均返回当前版本，重复请求仍重放原回执。无权成员收到 404，受限助手收到原有的冲突回执。 |
| 成员消息 | 模型收到的工具参数仅允许 `chat` 和 `progress`，服务端拒绝布置、补充和 `required_inputs`。通过正常模型回合发送两种消息，并回归验证用户通过 HTTP 布置任务、目标归属和消息可见性。 |
| 回合步数 | 默认上限为 20 步。第 20 次模型调用可正常结束。需要继续调用时触顶，之后用同一回合的检查点恢复到第 21 次调用，已执行的 20 笔工具保持原回执。 |

三项工具改动均在现有查询、冲突回执或参数校验内完成。单次调用即可得到相应结果。

## 回归

Docker 全量回归 **207 通过、7 跳过**。六项跳过需要显式启用真实端点或检索联调，另一项测试需要 POSIX 文件描述符，当前在 Windows 运行。测试记录一条已有的 Starlette TestClient 弃用警告。

定向范围包括[工具参数](../../tests/test_tool_validation_feedback.py)、[委派](../../tests/test_delegation.py)、[协作消息](../../tests/test_collaboration.py)、[目标投递](../../tests/test_goal_delivery_scope.py)及[模型恢复](../../tests/test_model_recovery.py)。

```powershell
$testDirectory = Join-Path .tmp ('pytest-tool-friction-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path .tmp -Force | Out-Null
$env:RUN_DOCKER_TESTS = '1'
python -m pytest -q --basetemp $testDirectory
```
