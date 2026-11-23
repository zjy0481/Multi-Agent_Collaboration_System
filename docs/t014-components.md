# T014 组件来源与验证索引

以集成基线 `fa88192` 的仓库声明和本机观测为准。`requirements.txt` 固定四个直接 Python 依赖。表中其余本机版本用于复现实验环境。

| 组件 | 版本与来源 | 许可证 | 使用与验证索引 |
|---|---|---|---|
| FastAPI | 0.139.0，`requirements.txt`；[源码](https://github.com/fastapi/fastapi) | [MIT](https://github.com/fastapi/fastapi/blob/master/LICENSE) | `collaboration_runtime/api.py`；`tests/test_collaboration.py` |
| HTTPX | 0.28.1，`requirements.txt`；[源码](https://github.com/encode/httpx) | [BSD-3-Clause](https://github.com/encode/httpx/blob/master/LICENSE.md) | `collaboration_runtime/model.py`、`search.py`；`tests/test_model_config.py`、`test_search.py` |
| python-dotenv | 1.2.4，`requirements.txt`；[源码](https://github.com/theskumar/python-dotenv) | [BSD-3-Clause](https://github.com/theskumar/python-dotenv/blob/main/LICENSE) | 配置加载；`tests/test_model_config.py` |
| Uvicorn | 0.51.0，`requirements.txt`；[源码](https://github.com/encode/uvicorn) | [BSD-3-Clause](https://github.com/encode/uvicorn/blob/master/LICENSE.md) | ASGI 服务启动，见 `README.md` |
| Pydantic / Starlette | 本机 2.13.4 / 1.3.1；[Pydantic 源码](https://github.com/pydantic/pydantic)、[Starlette 源码](https://github.com/encode/starlette) | [MIT](https://github.com/pydantic/pydantic/blob/main/LICENSE) / [BSD-3-Clause](https://github.com/encode/starlette/blob/main/LICENSE.md) | 参数和工具校验、请求中间件及 TestClient。`collaboration_runtime/api.py`、`tests/test_collaboration.py` |
| Python / SQLite | 当前 `.venv` Python 3.14.5，SQLite 3.50.4；[Python](https://github.com/python/cpython)、[SQLite](https://sqlite.org/src/) | [Python 许可证](https://docs.python.org/3/license.html)；[SQLite 公有领域声明](https://www.sqlite.org/copyright.html) | `collaboration_runtime/journal.py`；`tests/test_journal.py`、`test_scheduler.py` |
| Docker | 当前客户端和引擎均为 29.9.2；本机 `collaboration-sandbox:local` 镜像 ID 为 `sha256:bc9be8ba79a50b8ccb40ce04ff9b31329d8caeeca5979e141657503c13cd3d1e`。`docker/Dockerfile` 的基础镜像使用 `python:3.12-slim` 标签；[Moby 源码](https://github.com/moby/moby) | [Moby Apache-2.0](https://github.com/moby/moby/blob/master/LICENSE) | `collaboration_runtime/sandbox.py`、`tests/test_sandbox.py`、`docs/validation/t005-validation.md` |
| Git | 当前 2.54.0.windows.1；[源码](https://github.com/git/git) | [GPL-2.0](https://github.com/git/git/blob/master/COPYING) | `collaboration_runtime/git_executor.py`；`tests/test_git_executor.py`、`test_git_approvals.py` |
| SearXNG | `2026.11.13-8a53a6aa5`，本机检索容器使用 `ghcr.io/searxng/searxng` 镜像，摘要 `sha256:82b222dd1854c43241874874fbc985006556230804dc819ef2a639eeb574d0e2`；[源码](https://github.com/searxng/searxng) | [AGPL-3.0](https://github.com/searxng/searxng/blob/master/LICENSE) | 外部检索服务，`collaboration_runtime/search.py`、`tests/test_live_search.py`、[真实环境联调记录](live-validation.md#t013-真实端点与检索) |

## T013 实际联调

`README.md` 的 T013 记录报告 course 模式、`api1.hkgai.net`、`t2_hkgai-v3_fp8_1m_e7` 和 Chat Completions；真实模型建立缺省 Lead，并分步调用 `read_file`、`send_message`，按不同调用 ID 核对随机文件内容和群聊记录。`tests/test_course_integration.py` 是对应显式启用的可重复测试。README 还记录本机 SearXNG 环回 JSON 联调，真实模型依次检索、取回、发消息并核对来源；`tests/test_live_search.py` 是对应测试。以上是既有记录，本次未重新运行真实外部联调；认证失败使用测试传输验证。客户端和模型服务的版本不应混同。

## 机制参照与本组实现

下列六个项目用于设计参照。当前仓库的实现证据指向本组 Python、SQLite 和 Docker 接线，复用方式记录为机制参照。

| 上游 | 参照入口 | 本仓库实现与验证 |
|---|---|---|
| OpenClaw | [群聊唤醒](https://docs.openclaw.ai/channels/groups)、[队列源码](https://github.com/openclaw/openclaw/blob/main/src/process/command-queue.ts) | `collaboration_runtime/collaboration.py`、`scheduler.py`，`tests/test_collaboration.py`、`test_scheduler.py` |
| LangGraph | [动态分发 `Send` 源码](https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/types.py)、[检查点分支](https://docs.langchain.com/oss/python/langgraph/use-time-travel) | `delegation.py`、`forks.py`，`tests/test_delegation.py`、`test_forks.py` |
| DeepSeek Harness Team | [团队任务板及工具包源码入口](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/team/README.md) | `delegation.py`、`scheduler.py`，`tests/test_delegation.py` |
| Codex | [固定提交的分支协议](https://github.com/openai/codex/blob/1476a08f1b410e359e64becf2494bc248743e396/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L3250) | `forks.py`、`runtime.py`；`tests/test_forks.py`、`test_runtime.py` |
| Pi | [会话管理与分支源码](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/session-manager.ts) | `fork_history.py`，`tests/test_forks.py` |
| OpenHands | [Docker 工作区源码](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-workspace/openhands/workspace/docker/workspace.py) | `sandbox.py`，`tests/test_sandbox.py` |

DeepSeek 模型的[工具调用协议](https://api-docs.deepseek.com/guides/tool_calls/)也是接口参照；实际课程联调的服务和模型见上文。机制参照的移动分支不是固定版本，如日后复制或改编源码，须另记固定提交、文件、许可证、修改及验证。
