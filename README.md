# 多智能体协作平台

## 项目简介

在本机工作台向一个由模型驱动的团队布置任务。成员可以协商分工、向用户提问、在各自的 Docker 工作区执行任务，并提交带版本的交付物。用户通过网页查看对话、回复问题和下载有权读取的成果。Python 后端调用 HKGAI 课程模型，SQLite 保存团队、会话和任务状态。

## 环境配置

以下命令在 Windows PowerShell 的仓库根目录执行。准备 Python 3.13、运行 Linux 容器的 Docker Desktop，以及课程发放的 HKGAI API Key。首次安装或需要配置可选的联网搜索时，参见[本地环境配置指南](docs/environment-setup.md)。

```powershell
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    python -m venv .venv
}
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
docker build -t collaboration-sandbox:local -f docker/Dockerfile docker
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
```

在项目根目录的 `.env` 中填好 `COURSE_API_KEY`。模板已配置课程端点和模型 ID。把 API Key 保留在本机。

## 启动

```powershell
.\.venv\Scripts\python.exe -m uvicorn collaboration_runtime.api:app --host 127.0.0.1 --port 8000 --workers 1
```

保持该终端运行。首次启动会在状态目录创建 `operator-token`，用于网页登录和本机管理 API。默认状态目录为 `.tmp/runtime`。如果启动时设置了 `COLLABORATION_STATE_DIR`，请从所选目录读取令牌。

## 使用教程

### 1. 导入角色并建组

新状态目录中的工作台会显示「暂无团队」。当前需要先通过本机 API 创建团队。服务启动后，另开一个 PowerShell，在仓库根目录执行以下命令。示例导入软件开发团队，也可以将 `$rosterPath` 改为其他官方样例的 `01_role_initialization.md`。

```powershell
$operatorToken = (Get-Content -LiteralPath '.tmp/runtime/operator-token' -Raw).Trim()
$headers = @{ Authorization = "Bearer $operatorToken" }
$rosterPath = 'test samples/scenario_1_software_engineering 2/01_role_initialization.md'
$roster = Get-Content -LiteralPath $rosterPath -Raw -Encoding UTF8
$body = @{
    request_id = [guid]::NewGuid().ToString('N')
    roster = $roster
} | ConvertTo-Json
$team = Invoke-RestMethod 'http://127.0.0.1:8000/v1/teams' -Method Post `
    -Headers $headers -ContentType 'application/json' -Body ([Text.Encoding]::UTF8.GetBytes($body))
$team.members | Select-Object role_id, name
```

返回成员列表后即可在网页选中团队。若建组失败，先排查错误，再在同一终端用已有的 `$body` 重跑 `Invoke-RestMethod` 请求，保留原请求 ID。

### 2. 登录并布置任务

1. 打开[本机工作台](http://127.0.0.1:8000/)，用同一个 `operator-token` 登录。若已登录且仍显示「暂无团队」，点击右上角「刷新」，再从「团队」下拉框选中刚创建的团队。
2. 在「群聊」将消息类型选为「布置」，输入任务并发送。可用所选场景的 `02_task_1.md` 作为第一题。默认由 Lead 接收，目标登记后可在「活动与排队目标」查看状态。三组角色和题目见[官方样例](<test samples/README.md>)。
3. 团队提出问题时，在「待答问题」点击对应的「答复」，然后输入并发送答案。第一题结束后，可在同一团队布置 `03_task_2.md`，延续既有协作记录。

### 3. 查看交付物

在「任务交付」展开有权读取的任务，即可查看当前与历史提交、验证证据和审查决定。点击具体版本链接下载文件。

课程模型临时故障会有限重试。对于重试耗尽或达到步数上限的回合，本机操作者可通过 API 的 `POST /v1/sessions/{session_id}/turns/{turn_id}/retry` 续跑。

检索支持上游状态和受控原文读取，具体接入条件见[本地环境配置指南](docs/environment-setup.md)。

## 参考

- [本机 API 文档](http://127.0.0.1:8000/docs)
- [项目详细需求](docs/grill_result/agent-collaboration/requirement.md)
- [真实环境联调记录](docs/live-validation.md)
- [T015 官方样例验收记录](docs/validation/t015-second-fix-rerun.md)
