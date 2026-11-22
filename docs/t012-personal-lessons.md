# T012 个人经验、跨题交接与采用证据

本模块由 `collaboration_runtime/experience.py` 提供。它使用现有团队、目标、产物 ACL 和助手分支服务，不改任务生命周期或评测实现。

## 交付边界

每个正式成员拥有自己的经验记录。经验保存正文、可追溯的消息或产物版本引用、适用条件和失效条件。授权检索先按以下顺序筛选候选，再执行内容匹配：

1. 作者读取自己的经验。
2. 接收者读取显式交接给本人且目标题匹配的经验。
3. 助手仅在仍有效的 fork、匹配的活动目标和获准工具范围内读取。
4. 每次读取重新检查经验引用的消息与产物版本是否仍可读。

交接记录来源目标、目标题、来源经验、获准版本、发送者、指定接收者和用途。每次交接至少包含一项经验或固定版本，允许只用 `artifact_version_ids` 显式交接第一题版本而不附带经验正文。授权顺序为：调用者在事务内通过团队、来源目标与目标题鉴权，随后校验经验所有权及其来源，再解析接收者并校验双方的来源/版本读取权限；其他成员不会因经验或接收者是否存在而得到不同失败信息。

发送者或指定接收者可按交接 ID 复核持久记录。读取时重新检查目标题和当前来源/固定版本权限，不继承原 ACL 之外的访问；接收者失去版本权限后返回 `404`，重启后仍保持拒绝。检索候选在内容匹配前完成同样的当前权限筛选。

检索、采用动作和观察结果是三类不可变记录：

- 检索保存查询、目标题、当时可见与命中的经验 ID。
- 采用必须引用本人已有检索 ID 和其中命中的经验 ID，保存后续行动及证据。
- 观察必须引用本人已有采用 ID，结果为 `behavior_changed`、`no_change` 或 `invalidated`，并保存说明、证据和可选复验项。
- 新的采用和观察只能写入当前活动目标；目标结束后返回 `409`。已完成检索、采用、观察及其读取不受影响，重放原请求仍返回原回执，不修改冻结事实。

这使“检索过”“实际采用”和“观察到效果”可分别定位；重启后由同一 SQLite 记录复核。

## HTTP 接口

`build_router(authenticated)` 通过应用现有的 `_authenticated` 依赖完成本机鉴权，并复用 `Cache-Control: no-store` 边界，不重复实现凭据解析。调用者必须是正式成员，本地操作者由路由返回 `403`。第三个未获交接的成员只能看到空检索结果，不能直接读取经验正文或交接。

| 方法与路径 | 作用 |
|---|---|
| `POST /v1/teams/{team_id}/lessons` | 写入本人经验；同一请求键重放返回原回执，参数不同返回 `409` |
| `POST /v1/teams/{team_id}/lessons/search` | 授权筛选后检索，并持久化检索记录 |
| `GET /v1/teams/{team_id}/lessons/{lesson_id}?target_goal_id=...` | 作者可直接读取；接收者必须提供获交接的目标题 |
| `POST /v1/teams/{team_id}/lesson-handoffs` | 作者显式交接本人经验、允许版本、指定接收者与用途 |
| `GET /v1/teams/{team_id}/lesson-handoffs/{handoff_id}` | 发送者或指定接收者按当前权限复核经验和/或固定版本交接 |
| `GET /v1/teams/{team_id}/lesson-retrievals/{retrieval_id}` | 按当前权限重新投影原检索结果 |
| `POST /v1/teams/{team_id}/lesson-retrievals/{retrieval_id}/uses` | 记录引用该检索的实际行动 |
| `POST /v1/teams/{team_id}/lesson-uses/{use_id}/observations` | 记录行为改变、无变化或经验失效 |
| `GET /v1/teams/{team_id}/experience?target_goal_id=...` | 返回本人可见经验、交接和完整检索/采用/观察轨迹 |

经验来源写成 `{"kind":"message|artifact","source_id":"..."}`。`lesson_ids` 和 `artifact_version_ids` 均可省略为空，但二者不能同时为空。固定版本在交接时和每次读取时都必须对接收者可见。

## 模型工具

正式成员可见：

- `record_lesson`
- `retrieve_lessons`
- `read_lesson`
- `read_handoff`
- `handoff_lessons`
- `record_lesson_use`
- `observe_lesson`
- `get_experience`

助手只看到其 fork `allowed_tools` 明确列出且服务端允许的 `retrieve_lessons`、`read_lesson` 和 `read_handoff`，不因拥有 `read_file` 等 workspace 工具而获得经验读取。调用时同时要求 fork 处于有效状态、目标题等于 fork 目标、经验/版本已显式交接、经验来源版本仍在 fork 的材料列表中并对父代理可读。撤权或任务范围失效后工具不可用。

## 生产接线

`create_app` 已按精确 T009 合并基线完成公共接线：

1. 创建 `ForkService` 后构造并挂载：

   ```python
   experience = ExperienceService(collaboration, workspace, forks)
   experience_tools = ExperienceTools(experience, auth, workspace)
   app.state.experience = experience
   app.state.experience_tools = experience_tools
   runtime.experience_tools = experience_tools
   ```

2. `create_app` 在应用启动前注册：

   ```python
   from collaboration_runtime.api import _authenticated
   from collaboration_runtime.experience import build_router

   app.include_router(build_router(_authenticated))
   ```

   不另建凭据解析或缓存边界。
3. `Runtime._run` 将 `experience_tools.tools_for(role)` 加入真实模型工具列表。
4. `Runtime._execute_tool` 对 `experience_tools.handles(name)` 分派到 `experience_tools.execute(role, name, arguments)`，并把这些幂等或带请求回执的工具列入可重放集合。
5. `workspace.py` 接受助手将 `retrieve_lessons`、`read_lesson` 和 `read_handoff` 注册为 fork 允许工具；经验工具本身仍由 Runtime 注入，并由 fork 当前 `allowed_tools` 精确裁剪。

默认助手经验读取集合包含上述三项。验收使用真实 fork job 和确定性模型经 `Runtime._run` 调用允许的工具，不使用 `read_file` 代替经验读取。

## 验证

`tests/test_experience.py` 使用生产 `create_app`、真实 SQLite、既有团队/目标/产物服务、真实 `ForkService` 和 T009 正式目标状态接口，覆盖：

- 第一题作者、第二题接收者和未获交接第三成员的读取边界。
- 来源目标、目标经验和固定版本的第二题显式交接。
- 无经验正文的纯版本交接、发送者/指定接收者复核、第三方不可见及撤权后重启仍拒绝。
- 检索、三次采用动作及行为改变、无变化、经验失效观察，重启后复核。
- 通过 T009 正式取消/激活接口切换目标；目标结束后拒绝新采用/观察，同时保留既有检索、采用、观察和重放回执。
- 交接时及读取时来源 ACL 的正反例。
- 未授权交接不泄露经验或接收者存在性。
- 确定性模型在真实 fork job 中经 `Runtime._run`/`_execute_tool` 检索、直读经验和交接；撤权后工具列表为空且调用被拒绝。

运行：

```powershell
& 'D:/Learning/hkust/NLP assignment/Final/.venv/Scripts/python.exe' -m pytest tests/test_experience.py -q
```
