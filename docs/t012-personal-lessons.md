# T012 个人经验、跨题交接与采用证据

本模块由 `collaboration_runtime/experience.py` 提供。它使用现有团队、目标、产物 ACL 和助手分支服务，不改任务生命周期或评测实现。

## 交付边界

每个正式成员拥有自己的经验记录。经验保存正文、可追溯的消息或产物版本引用、适用条件和失效条件。授权检索先按以下顺序筛选候选，再执行内容匹配：

1. 作者读取自己的经验。
2. 接收者读取显式交接给本人且目标题匹配的经验。
3. 助手仅在仍有效的 fork、匹配的活动目标和获准工具范围内读取。
4. 每次读取重新检查经验引用的消息与产物版本是否仍可读。

交接记录来源目标、目标题、来源经验、获准版本、发送者、指定接收者和用途。接收者或版本失去当前权限后，候选在内容匹配前被排除，直接读取返回 `404`。

检索、采用动作和观察结果是三类不可变记录：

- 检索保存查询、目标题、当时可见与命中的经验 ID。
- 采用必须引用本人已有检索 ID 和其中命中的经验 ID，保存后续行动及证据。
- 观察必须引用本人已有采用 ID，结果为 `behavior_changed`、`no_change` 或 `invalidated`，并保存说明、证据和可选复验项。

这使“检索过”“实际采用”和“观察到效果”可分别定位；重启后由同一 SQLite 记录复核。

## HTTP 接口

所有接口使用现有本机鉴权，调用者必须是正式成员。第三个未获交接的成员只能看到空检索结果，不能直接读取经验正文。

| 方法与路径 | 作用 |
|---|---|
| `POST /v1/teams/{team_id}/lessons` | 写入本人经验；同一请求键重放返回原回执，参数不同返回 `409` |
| `POST /v1/teams/{team_id}/lessons/search` | 授权筛选后检索，并持久化检索记录 |
| `GET /v1/teams/{team_id}/lessons/{lesson_id}?target_goal_id=...` | 作者可直接读取；接收者必须提供获交接的目标题 |
| `POST /v1/teams/{team_id}/lesson-handoffs` | 作者显式交接本人经验、允许版本、指定接收者与用途 |
| `GET /v1/teams/{team_id}/lesson-retrievals/{retrieval_id}` | 按当前权限重新投影原检索结果 |
| `POST /v1/teams/{team_id}/lesson-retrievals/{retrieval_id}/uses` | 记录引用该检索的实际行动 |
| `POST /v1/teams/{team_id}/lesson-uses/{use_id}/observations` | 记录行为改变、无变化或经验失效 |
| `GET /v1/teams/{team_id}/experience?target_goal_id=...` | 返回本人可见经验、交接和完整检索/采用/观察轨迹 |

经验来源写成 `{"kind":"message|artifact","source_id":"..."}`。交接可以另列 `artifact_version_ids`，用于第二题引用第一题的固定版本；这些版本在交接时和每次读取时都必须对接收者可见。

## 模型工具

正式成员可见：

- `record_lesson`
- `retrieve_lessons`
- `read_lesson`
- `handoff_lessons`
- `record_lesson_use`
- `observe_lesson`
- `get_experience`

助手只能看到 `retrieve_lessons` 和 `read_lesson`，且同时要求 fork 处于有效状态、目标题等于 fork 目标、助手获准包含 `retrieve_lessons`、经验/版本已显式交接、经验来源版本仍在 fork 的材料列表中并对父代理可读。撤权或任务范围失效后工具不可用。

## 生产接线

当前分支只包含 T012 专属模块、测试和本文件。公共接线应在协调者指定的精确 T009 SHA 上单独完成：

1. 在 `create_app` 创建 `ForkService` 后构造：

   ```python
   experience = ExperienceService(collaboration, workspace, forks)
   experience_tools = ExperienceTools(experience, auth, workspace)
   app.state.experience = experience
   app.state.experience_tools = experience_tools
   runtime.experience_tools = experience_tools
   ```

2. 在应用启动前注册 `collaboration_runtime.experience.router`。
3. 在 `Runtime._run` 的工具列表中加入 `experience_tools.tools_for(role)`。
4. 在 `Runtime._execute_tool` 中，对 `experience_tools.handles(name)` 分派到 `experience_tools.execute(role, name, arguments)`。
5. 在 `workspace.py` 的助手工具白名单中加入 `retrieve_lessons` 和 `read_lesson`，使创建 fork 时能明确授予这两项能力。

测试使用构造参数 `assistant_read_tools={"read_file"}` 验证真实 fork 的服务端范围检查；生产接线必须保留默认的 `retrieve_lessons`，不要用 `read_file` 代替经验读取权限。

## 验证

`tests/test_experience.py` 使用真实 SQLite、既有团队/目标/产物服务及真实 `ForkService`，覆盖：

- 第一题作者、第二题接收者和未获交接第三成员的读取边界。
- 来源目标、目标经验和固定版本的第二题显式交接。
- 检索、三次采用动作及行为改变、无变化、经验失效观察，重启后复核。
- 交接时及读取时来源 ACL 的正反例。
- 授权助手在有效 fork 内读取、跨目标题拒绝、撤权后拒绝。

运行：

```powershell
& 'D:/Learning/hkust/NLP assignment/Final/.venv/Scripts/python.exe' -m pytest tests/test_experience.py -q
```
