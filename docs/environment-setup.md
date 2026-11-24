# 本地环境配置指南

本指南适用于从 Windows PowerShell 启动工作台的开发和测试环境。以下命令均从项目根目录执行。安装 WSL 时使用管理员 PowerShell。

## Python 与 `.venv`

本机联调使用 Python 3.13.5。已有可用的 `.venv` 时直接复用。首次配置时安装 Python 3.13，确认 `python --version` 显示预期版本后创建虚拟环境。接下来的命令直接使用 `.venv` 中的解释器。 [Python `venv` 文档](https://docs.python.org/3.13/library/venv.html)

```powershell
if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
    python -m venv .venv
}

.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install pytest
.\.venv\Scripts\python.exe -m pip check
```

运行依赖锁定在 [requirements.txt](../requirements.txt)。普通回归使用测试替身，运行 `.\.venv\Scripts\python.exe -m pytest -q` 即可。真实容器、课程模型和检索测试按下文在当前 PowerShell 显式开启。

## HKGAI 课程模型

首次配置时从模板复制 `.env`。已有 `.env` 时保留其中的配置。

```powershell
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
```

在 `.env` 中保留课程端点和模型 ID，并在 `COURSE_API_KEY=` 后填写课程发放的密钥。课程提供不同的端点或模型时，以课程发放的信息为准。`.env` 已被 Git 忽略，请在本机保管密钥。同名进程环境变量优先于 `.env`，修改后重启工作台。 [配置模板](../.env.example)

```dotenv
COLLABORATION_MODEL_MODE=course
COURSE_API_BASE_URL=https://api1.hkgai.net/v1
COURSE_API_MODEL=t2_hkgai-v3_fp8_1m_e7
COURSE_API_KEY=
```

离线调试可显式设置 `COLLABORATION_MODEL_MODE=mock`。运行下文的真实模型联调时使用 `course`，并为测试保留课程模型额度。

课程模型请求的临时错误会自动重试。耗尽重试或达到回合步数上限后，本机操作者可以调用 `POST /v1/sessions/{session_id}/turns/{turn_id}/retry` 从检查点继续。每次续跑的步数有限，成功的工具回执会保留。

## WSL 2 与 Docker

在开启硬件虚拟化的 Windows 上，以管理员身份安装 WSL。安装完成后按系统提示重启。Docker Desktop 可在 Windows PowerShell 使用其 Linux Engine，安装时可跳过单独的 Linux 发行版。 [Microsoft WSL 命令](https://learn.microsoft.com/zh-cn/windows/wsl/basic-commands) · [Docker Desktop 的 WSL 2 后端](https://docs.docker.com/desktop/features/wsl/)

```powershell
wsl --install --no-distribution
```

安装并启动 Docker Desktop，选择 WSL 2 后端和 Linux 容器。若 `docker` 尚未进入当前终端的 `PATH`，重新打开 PowerShell。 [Docker Desktop 安装说明](https://docs.docker.com/desktop/setup/install/windows-install/)

```powershell
wsl --status
wsl --list --verbose
docker version
docker info --format '{{.OSType}} {{.KernelVersion}}'
```

Docker Desktop 启动后，`docker-desktop` 的 WSL 版本应为 `2`，Docker 服务端应显示 `linux`。构建项目的沙箱镜像，再运行真实容器测试。该组用例覆盖角色工作区、宿主凭据、断网、只读产物和超时清理。

```powershell
docker build -t collaboration-sandbox:local -f docker/Dockerfile docker

$env:RUN_DOCKER_TESTS = '1'
.\.venv\Scripts\python.exe -m pytest -q tests/test_sandbox.py
Remove-Item Env:RUN_DOCKER_TESTS
```

## 联网搜索与代理

检索由工作台宿主发起。宿主从进程环境读取 `EXA_API_KEY`，优先直连 Exa。若 Exa 超时、请求失败或未返回可用链接，并且配置了 `COURSE_SEARCH_URL`，则同一次工具调用回退到 SearXNG。只配置 SearXNG 时会直接使用它。备用容器可通过 `host.docker.internal:7897` 访问宿主代理。成员的执行容器使用 `--network none`。 [Exa Search API](https://exa.ai/docs/reference/search) · [Docker Desktop 网络说明](https://docs.docker.com/desktop/features/networking/networking-how-tos/) · [沙箱实现](../collaboration_runtime/sandbox.py)

`search_web` 和 `get_search_result` 返回实际提供者 `provider`、上游状态 `upstream_status` 和可能的回退原因 `fallback_reason`。回退或备用引擎部分不可用时，状态记为 `degraded`。`read_source` 可用结果编号或精确 URL 读取当前会话搜索记录中的公网 HTTPS 链接。宿主最多跟随五次重定向，每跳重新校验并固定公网地址。证据保存原始 URL、最终 URL、跳转链、获取时间及原始内容哈希，可用 `get_source` 取回。

原文读取支持 HTML、纯文本和带文本层的 PDF。PDF 由宿主虚拟环境中随 `requirements.txt` 安装的 pypdf 提取，下载限 5 MB、提取限 5 秒和前 20 页，返回文本最多保留 40,000 字符。扫描件或加密 PDF 会返回提取错误。若来源站拒绝访问，工具保留站点 HTTP 状态并提示选择其他公开原文。 [pypdf 文本提取说明](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)

### 验证 SearXNG 使用的 Windows 代理

先启动本机的代理客户端，确认 `7897` 是可用的 HTTP 代理端口。其他机器使用不同端口时，将本节示例中的 `7897` 替换为实际端口。命令返回 `Proxy CONNECT=200 HTTP=204` 时，说明代理可以建立 HTTPS 隧道并取得响应。

```powershell
curl.exe --proxy 'http://127.0.0.1:7897' --max-time 15 --silent --show-error `
  --output NUL --write-out 'Proxy CONNECT=%{http_connect} HTTP=%{http_code}\n' `
  'https://www.google.com/generate_204'
```

### 启动备用 SearXNG

首次部署时生成本机配置，启用 JSON 输出，并指定容器访问宿主代理的地址。以只监听 `127.0.0.1:8888` 的方式发布搜索服务。 [SearXNG 容器安装](https://docs.searxng.org/admin/installation-docker.html) · [SearXNG 出站代理](https://docs.searxng.org/admin/settings/settings_outgoing.html)

```powershell
$searxConfigDir = (New-Item -ItemType Directory -Force -Path '.tmp/searxng/config').FullName
$searxSettingsFile = Join-Path $searxConfigDir 'settings.yml'

if (-not (Test-Path -LiteralPath $searxSettingsFile)) {
    $searxSecret = & .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"
@"
use_default_settings: true
server:
  secret_key: "$searxSecret"
search:
  formats: [html, json]
outgoing:
  proxies:
    all://:
      - http://host.docker.internal:7897
"@ | Set-Content -LiteralPath $searxSettingsFile -Encoding ascii
}

docker run -d --name collaboration-searxng -p 127.0.0.1:8888:8080 `
  --mount "type=bind,source=$searxConfigDir,target=/etc/searxng" `
  --mount 'type=volume,source=collaboration-searxng-cache,target=/var/cache/searxng' `
  docker.io/searxng/searxng:latest
```

如果已有同名容器，使用 `docker start collaboration-searxng` 启动它。编辑 `.tmp/searxng/config/settings.yml` 后，运行 `docker restart collaboration-searxng` 加载配置。

### 接入项目并验证检索

如果 Windows 已设置 `EXA_API_KEY`，在继承该变量的 PowerShell 中启动工作台。对于设置变量之前就已打开的 PowerShell，可将已保存的值加载到当前进程。以下命令不会显示密钥。

```powershell
if (-not $env:EXA_API_KEY) {
    $env:EXA_API_KEY = [Environment]::GetEnvironmentVariable('EXA_API_KEY', 'User')
}
if (-not $env:EXA_API_KEY) {
    $env:EXA_API_KEY = [Environment]::GetEnvironmentVariable('EXA_API_KEY', 'Machine')
}
```

使用本机 SearXNG 作为备用时，在 `.env` 中填写端点。只有备用服务需要 Bearer 认证时才填写 `COURSE_SEARCH_API_KEY`。修改配置后重启工作台。

```dotenv
COURSE_SEARCH_URL=http://127.0.0.1:8888/search
COURSE_SEARCH_API_KEY=
```

先跑真实检索用例，确认已设置 Exa Key 时返回 `provider=exa`、重启后可读取原结果，并检查会话间的读权限。也可独立检查备用 `/search` 是否返回非空 `results`。 [SearXNG 搜索 API](https://docs.searxng.org/dev/search_api.html) · [项目检索测试](../tests/test_live_search.py)

```powershell
curl.exe --noproxy '*' --fail --get `
  --data-urlencode 'q=site:sqlite.org SQLite transaction documentation' `
  --data-urlencode 'format=json' 'http://127.0.0.1:8888/search'

$env:RUN_LIVE_SEARCH_TESTS = '1'
.\.venv\Scripts\python.exe -m pytest -q tests/test_live_search.py -k records_reopen
Remove-Item Env:RUN_LIVE_SEARCH_TESTS
```

| 现象 | 检查方法 |
| --- | --- |
| `/search` 返回 `403` | 检查 `search.formats` 是否包含 `json`，保存后重启 SearXNG。 |
| 返回 `200` 但 `results` 为空 | 用 `docker logs collaboration-searxng` 查看上游错误，确认容器能访问 `host.docker.internal:7897`。 |
| 某个引擎提示 `CAPTCHA` | 查看其他引擎是否仍返回有效来源。曾观察到 DuckDuckGo 触发验证码，而 Google CSE 仍返回 SQLite 官方文档。 |
| Docker 拉取镜像超时 | 在 Docker Desktop 的 **Settings → Resources → Proxies** 检查镜像拉取所用的代理。有需要时填写 `http://127.0.0.1:7897`。 [Docker Desktop 代理设置](https://docs.docker.com/desktop/settings-and-maintenance/settings/#proxies) |

项目的 HKGAI 和检索客户端都设置了 `trust_env=False`，会忽略 `HTTP_PROXY`、`HTTPS_PROXY` 等环境变量。SearXNG 的出站代理通过自己的 `outgoing.proxies` 控制。若 HKGAI 连接超时，从 Windows 宿主检查课程端点的直连条件。 [HTTPX 环境变量](https://www.python-httpx.org/environment_variables/)

## Git 与 GitHub 测试仓库

安装 Git for Windows，并用 `git --version` 检查命令是否可用。Git 自动化用例会自行创建临时裸仓库。手动验收工作台的提交、审批和推送时，创建持久的本地裸仓库，并在启动工作台的同一个 PowerShell 设置 `COLLABORATION_GIT_TARGETS_DIR`。 [Git for Windows](https://git-scm.com/install/windows)

```powershell
if (-not (Test-Path -LiteralPath '.tmp/git-remotes/demo.git')) {
    git init --bare --initial-branch=main .tmp/git-remotes/demo.git
}
$env:COLLABORATION_GIT_TARGETS_DIR = (Resolve-Path '.tmp/git-remotes').Path
git --git-dir=.tmp/git-remotes/demo.git rev-parse --is-bare-repository

.\.venv\Scripts\python.exe -m pytest -q tests/test_git_executor.py tests/test_git_approvals.py
```

本地手动联调使用仓库名 `demo` 和目标分支 `refs/heads/main`。批准后可用 `git --git-dir=.tmp/git-remotes/demo.git rev-parse refs/heads/main` 核对提交 SHA。Codex 命令沙箱中曾出现本地推送失败，同一组用例在 Windows 宿主 PowerShell 通过。

**本项目所有需要访问 GitHub 仓库的测试，统一使用 `zjy0481/MACS_testing`。** 访问这个私有仓库需要相应的 GitHub 权限，实际推送需要写权限。每轮独立远端验收使用新分支，例如 `test/git-delivery-20261123-001`，将 `main` 留作基线。同一场景中依赖前次推送状态的步骤共用当轮分支。验收时记录完整目标 ref 和提交 SHA。 [统一测试仓库](https://github.com/zjy0481/MACS_testing.git)

需要验证 GitHub 访问权限时，安装 GitHub CLI（`gh`）。首次使用时运行 `gh auth login --hostname github.com --git-protocol https`，并选择有测试仓库权限的账号。若 HTTPS Git 命令需要复用 `gh` 的登录凭据，运行 `gh auth setup-git --hostname github.com`。 [GitHub CLI 认证说明](https://cli.github.com/manual/gh_auth_setup-git)

```powershell
gh auth status --hostname github.com
gh api repos/zjy0481/MACS_testing --jq '.permissions.push'
git ls-remote --symref https://github.com/zjy0481/MACS_testing.git HEAD
```

## 启动工作台与真实模型联调

启动 Docker Desktop 和需要的 SearXNG 容器。手动 Git 联调时，在同一个 PowerShell 先设置上节的 Git 目标目录。每次使用单进程访问一个状态目录。默认目录为 `.tmp/runtime`，需要分隔测试和演示数据时可用 `COLLABORATION_STATE_DIR` 指定独立目录。 [工作台启动说明](../README.md#启动)

```powershell
$env:COLLABORATION_STATE_DIR = '.tmp/runtime-test'
.\.venv\Scripts\python.exe -m uvicorn collaboration_runtime.api:app --host 127.0.0.1 --port 8000 --workers 1
```

首次启动会自动创建 SQLite 状态和登录用的 `operator-token` 文件。使用所选状态目录内的令牌登录工作台，结束时在启动终端按 `Ctrl+C`。 [本地工作台](http://127.0.0.1:8000/)

在 PowerShell 中运行以下两项真实模型用例。第一项验证补建 Lead、读取真实 Docker 沙箱文件并将内容发到群聊。第二项验证模型发起搜索、按 `search_id` 取回结果并在群聊中引用实际来源。 [课程模型测试](../tests/test_course_integration.py) · [模型检索测试](../tests/test_live_search.py)

```powershell
$env:RUN_LIVE_COURSE_TESTS = '1'
.\.venv\Scripts\python.exe -m pytest -q -s tests/test_course_integration.py
Remove-Item Env:RUN_LIVE_COURSE_TESTS

$env:RUN_LIVE_COURSE_SEARCH_TESTS = '1'
.\.venv\Scripts\python.exe -m pytest -q -s tests/test_live_search.py -k course_model
Remove-Item Env:RUN_LIVE_COURSE_SEARCH_TESTS
```

## 模型端点暂停与恢复

课程模型返回 HTTP 402 时，当前进程暂停领取新的模型待办。已领取的可恢复工作保留原回合和工具回执。操作者可通过本地 OpenAPI 页面查看 `GET /v1/model/status`。处理余额或配额后，使用操作者凭据调用 `POST /v1/model/resume`，调度器继续领取队列中的工作。该接口解除本地暂停，后续真实模型请求负责验证端点是否恢复。目标截止时间继续按原设置计算。重启服务会重新检查端点，待办和检查点仍保存在 SQLite。 [本地 API](http://127.0.0.1:8000/docs)

模型工具可按需查询 `list_delegation` 的 `compact` 视图。搜索结果的 `result_id` 可用于读取本次搜索命中的原文，也可沿用精确 URL。文本读取工具支持用 `start_char` 和 `max_chars` 指定片段。排查沙箱延迟时，可在 `run_command` 中启用 `measure_timings`，回执会报告总用时和 Docker CLI 进程用时。

一次模型决策最多可发出 8 个工具调用。超出的批次整体丢弃并占用一个普通回合步骤，运行时在剩余预算内给模型一次缩小批次的机会。沙箱默认镜像中的 Python 可用 `urllib.request` 测试容器内运行的本地服务。公网检索使用 `search_web` 和 `read_source`。

## 已知问题与验收状态

- **Starlette 测试客户端弃用提示。** `fastapi.testclient.TestClient` 使用 `httpx` 时会出现 `StarletteDeprecationWarning`。本机测试仍能通过，可继续推进。启用将警告视为错误的测试设置时，这条警告可能影响测试收集。后续可以单独评估 `TestClient` 迁移到 `httpx2`，并复跑相关用例。 [Starlette TestClient](https://starlette.dev/testclient/)
- **工作台审批后推送至 GitHub 待 T016。** 工作台已通过本地裸仓库的提交、审批和推送联调。通过 Git CLI 向统一测试仓库的新分支推送也已验证，远端 ref 与提交 SHA 一致。工作台当前的 Git 执行器仅接收本地裸仓库，审批后向 GitHub 推送的端到端验收随 T016 推进。 [T016 验收要求](plans/agent-collaboration/revisions/r0003/tickets/T016.md)
