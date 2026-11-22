# Multi-Agent_Collaboration_System

本机工作台支持导入角色、建组、增员、群聊和双方私聊。Python 连接课程模型的 Chat Completions 接口，SQLite 保存团队、契约、消息、待办与会话回合，Docker 提供私有执行空间。回合与中途输入参考 [Codex App Server](https://learn.chatgpt.com/docs/app-server)。完整平台交付范围见[详细需求](docs/grill_result/agent-collaboration/requirement.md)。

## 启动

在可访问 Linux Docker Engine 的 PowerShell 中执行。第一次打开项目时，从模板生成本地 `.env`，已有文件会保留。

```powershell
python -m pip install -r requirements.txt
docker build -t collaboration-sandbox:local -f docker/Dockerfile docker
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
```

打开项目根目录的 `.env`，把老师给的 Key 粘贴到 `COURSE_API_KEY=` 后面并保存。模板已填好 HKGAI 基址和模型 ID。更换课程 API 或模型时，可编辑对应两行并重启服务。`.env` 已由 Git 忽略；同名进程环境变量优先，新开终端可使用文件配置。

```powershell
python -m uvicorn collaboration_runtime.api:app --host 127.0.0.1 --port 8000 --workers 1
```

首次启动会在状态目录生成 `operator-token`；妥善保管该文件。访问 [主页](http://127.0.0.1:8000/) 可用令牌登录，API 使用 `Authorization: Bearer <token>`。建组、增员及旧双角色入口使用 operator 令牌。使用单进程访问同一个状态目录。缺少课程配置时启动报错。默认状态目录为 `.tmp/runtime`，可用 `COLLABORATION_STATE_DIR` 指定另一个目录。该目录应由宿主管理，包含会话历史与已发布材料。

## 导入角色并建组

把官方角色 Markdown 原文交给 `POST /v1/teams`。宿主用一次过渡模型决策识别 Lead 或创建一个通用 Lead，准备好每个成员的 Docker 工作区后返回成员清单和第 1 版分工契约。读取原始角色说明时保留提示正文，格式错误会返回文件行号。

```powershell
$operatorToken = (Get-Content -LiteralPath '.tmp/runtime/operator-token' -Raw).Trim()
$headers = @{ Authorization = "Bearer $operatorToken" }
$roster = Get-Content -LiteralPath 'test samples/scenario_1_software_engineering 2/01_role_initialization.md' -Raw -Encoding UTF8
$requestId = [guid]::NewGuid().ToString('N')
$body = @{ request_id = $requestId; roster = $roster } | ConvertTo-Json
$team = Invoke-RestMethod 'http://127.0.0.1:8000/v1/teams' -Method Post -Headers $headers -ContentType 'application/json' -Body ([Text.Encoding]::UTF8.GetBytes($body))
$team.members | Select-Object role_id, name, session_id, workspace_id
```

准备失败时重复发送同一请求体，宿主复用已经分配的标识。用 `GET /v1/teams/{team_id}` 查看当前成员。用 `POST /v1/teams/{team_id}/members` 提交 `request_id`、`expected_version`、`role_id`、`name` 和 `prompt` 增员，工作区就绪后才发布下一版契约。用 `GET /v1/teams/{team_id}/contracts/{version}` 可核对历史版本。operator 调用 `POST /v1/teams/{team_id}/members/{member_id}/credential` 获取成员 token；再次调用会轮换凭据，使旧 token 失效。使用该成员 token 和其 `session_id`，发送 `POST /v1/sessions/{session_id}/turns` 可用原角色提示在专属工作区执行命令。

## T002 团队协作

在[工作台](http://127.0.0.1:8000/)登录后选择团队，可发送群聊或双方私聊。点名用成员 ID 定向投递。真人发出的未点名群聊默认投递 Lead，未点名的进展消息只供读者查看。真人可用 `assignment` 记录原始布置，用 `supplement` 引用原始布置补充信息。Lead 可在原始布置上登记目标，首项目标进入活动状态，后续目标排队。

- `GET /v1/teams` 列出当前身份可访问的团队。`POST /v1/teams/{team_id}/messages` 接收 `request_id`、`text` 和可选的 `recipient_id`、`mentions`、`reply_to`、`kind`。重试同一条消息时复用原请求键与参数。
- `GET /v1/teams/{team_id}/snapshot` 返回可见消息、目标和游标。`GET /v1/teams/{team_id}/events?after=<cursor>` 接续后续事件，重连优先使用 `Last-Event-ID`。游标绑定团队和读者，可补读最近 512 个团队事件，收到 `stream.reset` 时重新取快照。
- `GET /v1/teams/{team_id}/deliveries` 返回该成员的待领取记录。Lead 用 `POST /v1/teams/{team_id}/goals` 提交 `request_id` 和 `source_message_id`。正式成员模型工具也可发送消息、查询可见消息和参与者，Lead 可用 `register_goal` 登记布置。

消息续读使用 FastAPI 0.139.0 的原生 SSE 和浏览器 `EventSource`。可运行 `python -m pytest -q tests/test_collaboration.py` 核对权限、并发重放、待办恢复和目标排队。 [FastAPI SSE](https://fastapi.tiangolo.com/tutorial/server-sent-events/) · [T002 验收用例](tests/test_collaboration.py)

## T003 真人澄清

正式成员可用 `ask_human` 提问，传入 `request_id`、已投递给本会话的 `source_message_id` 和 `text`。登记目标前也可询问。问题默认显示在群聊，引用双方私聊中的布置会保留原可见范围。

1. 在工作台「待答问题」点击「答复」，输入答案并发送。服务端按引用认定原提问者，即使消息同时点名其他成员。
2. 同一工作还有其他问题待答时继续阻塞。最后一条有效答复产生一条 `clarification.resume` 待办，包含原工作消息 ID 和相关答复 ID。已答问题如需变更，可引用原答复发送更正，提问者和引用了该决定的正式成员会收到通知。
3. 发起提问的模型回合以 `waiting_human` 释放会话执行槽。问题和待办保存在 SQLite，重启后可用原引用答复。T003 可通过 `GET /v1/teams/{team_id}/deliveries` 核对恢复投递，待办自动领取和启动模型回合由 T006 接入。

## T005 私有工作区与产物交接

对已导入团队的正式成员发起回合。成员可以调用 `create_file`、`read_file`、`update_file`、`delete_file` 操作自己的工作区，并通过 `run_command` 执行代码和测试。工作区文件使用相对路径，发布或文件写入单次上限为 1 MiB。

1. 请开发成员创建并自测一个文件，再调用 `list_workspace_readers` 取得测试成员的 `session_id`，通过 `publish_artifact` 指定该读者。工具返回 `artifact_id`、数字版本和 `version_id`。传回相同 `artifact_id` 可发布下一版。
2. 请测试成员用 `list_artifacts` 或 `read_artifact` 确认所验版本，在 `run_command` 的 `artifact_version_ids` 传入对应 ID。该命令可从 `/inputs/<version_id>/artifact` 读取指定只读文件。测试脚本仍写进自己的 `/workspace`。
3. 本机可信操作者也可用 `GET /v1/artifacts/{artifact_id}/versions/{version}` 下载指定数字版本，以成员 token 认证；无版本读权时返回 404。身份由 token 决定。模型工具的会话身份由运行时注入。

宿主在登记助手时指定独立工作区、允许的工具和授权版本。每次取文件和挂载都会检查该范围仍然有效，历史回合分支的入口由 T007 接入。

已获准的检索服务可在启动进程设置 `COURSE_SEARCH_URL` 为 SearXNG 的 `/search` JSON 端点，需要认证时用 `COURSE_SEARCH_API_KEY`。成员调用 `search_web` 得到来源和 `search_id`，可用 `get_search_result` 取回自己的记录。搜索由宿主发起，执行容器的外网继续隔离。真实检索来源的课程联调归 T013。 [SearXNG JSON 接口](https://docs.searxng.org/dev/search_api.html) · [T005 集成证据](docs/t005-validation.md)

## 开发与测试双角色

打开本机的 [接口交互页](http://127.0.0.1:8000/docs)，或在另一个 PowerShell 中使用：

```powershell
$operatorToken = (Get-Content -LiteralPath '.tmp/runtime/operator-token' -Raw).Trim()
$headers = @{ Authorization = "Bearer $operatorToken" }
$base = 'http://127.0.0.1:8000/v1/sessions'
$request = @{ text = '用 Python 做一个最小预订冲突检查，在私有区运行自测，然后发布 booking.py 给 tester。' } | ConvertTo-Json
$run = Invoke-RestMethod "$base/developer/turns" -Method Post -Headers $headers -ContentType 'application/json' -Body ([Text.Encoding]::UTF8.GetBytes($request))
$turn = "$base/developer/turns/$($run.turn_id)"
curl.exe -N -H "Authorization: Bearer $operatorToken" "$turn/events"
```

当回合仍在运行时，在接口交互页调用 `POST /v1/sessions/developer/turns/{turn_id}/steer`，提交 `{"text":"再检查紧邻的两个时段可以分别预订" }`。补充内容会在下一次模型决策前进入同一回合。GET 原回合地址可查看最后结果，事件流支持按 `Last-Event-ID` 接续。

看到 `publish_artifact` 成功后，给 `POST /v1/sessions/tester/turns` 发送 `{"text":"列出已发布产物，读取 booking.py 的发布版本，在自己的工作区编写并运行测试。" }`。测试角色用 `list_artifacts` 取得只读路径，在自己的 `/workspace` 写测试。发布快照以单个文件为单位，最多 1 MiB。已发布版本保留原内容，更新源文件后发布会获得新版本 ID。

同一角色一次只执行一个回合，两个角色可以并行。已经开始的工具可以结束并保存回执。收到新输入时，旧决策中尚未开始的工具会交给模型重新判断。服务重启后，已完成会话继续可用，未完成回合标记为中断，避免自动重做工具动作。

## 检查

```powershell
python -m pip install pytest
python -m pytest -q
$env:RUN_DOCKER_TESTS = '1'
python -m pytest -q
```

普通测试用明确注入的模型、工具及检索传输替身检查多步回合、授权、产物版本和检索来源记录。开启 Docker 测试后会使用真实容器核对角色隔离、文件工具、旧版只读复验、凭据隔离、默认无外网及超时清理。测试无需课程凭据。
