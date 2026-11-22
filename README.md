# Multi-Agent_Collaboration_System

当前本机入口支持 T001 导入角色名单、建组与增员，也可运行开发与测试的双角色联调。Python 直连课程模型的 Chat Completions 接口，SQLite 保存团队、契约、会话回合与事件，Docker 提供私有执行空间。回合与中途输入参考 [Codex App Server](https://learn.chatgpt.com/docs/app-server)。完整平台交付范围见[详细需求](docs/grill_result/agent-collaboration/requirement.md)。

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

API 服务面向本机可信操作者。使用单进程访问同一个状态目录。缺少课程配置时启动报错。默认状态目录为 `.tmp/runtime`，可用 `COLLABORATION_STATE_DIR` 指定另一个目录。该目录应由宿主管理，包含会话历史与已发布材料。

## 导入角色并建组

把官方角色 Markdown 原文交给 `POST /v1/teams`。宿主用一次过渡模型决策识别 Lead 或创建一个通用 Lead，准备好每个成员的 Docker 工作区后返回成员清单和第 1 版分工契约。读取原始角色说明时保留提示正文，格式错误会返回文件行号。

```powershell
$roster = Get-Content -LiteralPath 'test samples/scenario_1_software_engineering 2/01_role_initialization.md' -Raw -Encoding UTF8
$requestId = [guid]::NewGuid().ToString('N')
$body = @{ request_id = $requestId; roster = $roster } | ConvertTo-Json
$team = Invoke-RestMethod 'http://127.0.0.1:8000/v1/teams' -Method Post -ContentType 'application/json' -Body ([Text.Encoding]::UTF8.GetBytes($body))
$team.members | Select-Object role_id, name, session_id, workspace_id
```

准备失败时重复发送同一请求体，宿主复用已经分配的标识。用 `GET /v1/teams/{team_id}` 查看当前成员。用 `POST /v1/teams/{team_id}/members` 提交 `request_id`、`expected_version`、`role_id`、`name` 和 `prompt` 增员，工作区就绪后才发布下一版契约。用 `GET /v1/teams/{team_id}/contracts/{version}` 可核对历史版本。拿到成员的 `session_id` 后，发送 `POST /v1/sessions/{session_id}/turns` 可用原角色提示在专属工作区执行命令。正式团队的消息待办和产物授权分别由 T002 与 T005 接入。

## 开发与测试双角色

打开本机的 [接口交互页](http://127.0.0.1:8000/docs)，或在另一个 PowerShell 中使用：

```powershell
$base = 'http://127.0.0.1:8000/v1/sessions'
$request = @{ text = '用 Python 做一个最小预订冲突检查，在私有区运行自测，然后发布 booking.py 给 tester。' } | ConvertTo-Json
$run = Invoke-RestMethod "$base/developer/turns" -Method Post -ContentType 'application/json' -Body ([Text.Encoding]::UTF8.GetBytes($request))
$turn = "$base/developer/turns/$($run.turn_id)"
curl.exe -N "$turn/events"
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

普通测试用明确注入的模型与工具替身检查多步工具回合、轮中补充和事件。开启 Docker 测试后会使用真实容器核对角色隔离、产物只读、凭据隔离、默认无外网及超时清理。测试无需课程凭据。
