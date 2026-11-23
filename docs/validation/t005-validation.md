# T005 集成与验收记录

[工单](../plans/agent-collaboration/revisions/r0003/tickets/T005.md) · [查看交付物](../../README.md#3-查看交付物)

## 实际组件

| 组件 | 本次版本 | 许可与用途 |
| --- | --- | --- |
| Docker Engine | 29.9.2，Linux 容器 | 直接复用执行、资源限制和只读挂载；开源 Engine 项目 Moby 采用 Apache-2.0。 [Moby 许可证](https://github.com/moby/moby/blob/master/LICENSE) |
| 沙箱镜像 | `python:3.12-slim` 构建为 `collaboration-sandbox:local`，本机镜像 ID `sha256:bc9be8ba79a50b8ccb40ce04ff9b31329d8caeeca5979e141657503c13cd3d1e` | 运行受控 Python 文件工具及测试。Python 本身适用 PSF 许可，镜像所含系统包各有许可。 [Python 许可证](https://docs.python.org/3.12/license.html) · [Dockerfile](../../docker/Dockerfile) |
| Python / SQLite | Python 3.14.5，SQLite 3.50.4 | SQLite 属公共领域，用于产物版本、读者范围和来源记录。 [SQLite 许可](https://sqlite.org/copyright.html) |
| FastAPI / Pydantic | 0.139.0 / 2.13.4 | MIT，提供版本下载接口与模型工具的参数校验。 [FastAPI 许可证](https://github.com/fastapi/fastapi/blob/master/LICENSE) · [Pydantic 许可证](https://github.com/pydantic/pydantic/blob/main/LICENSE) |
| HTTPX | 0.28.1 | BSD-3-Clause，提供固定端点搜索和测试传输。SearXNG 接入采用公开的 JSON 协议。 [HTTPX 许可证](https://github.com/encode/httpx/blob/master/LICENSE.md) · [搜索协议](https://docs.searxng.org/dev/search_api.html) |

## 验收证据

全套回归共 42 项通过，含 5 项真实 Docker 用例。另用实际 HKGAI 模型 `t2_hkgai-v3_fp8_1m_e7` 做正式成员冒烟。开发成员调用 `create_file` 和 `publish_artifact`，测试成员按版本 ID 挂载文件、运行命令并在回复中复述实际读取的临时标记。第三成员视角无法下载该版。该运行只验证工作区和产物工具，检索验收使用下列测试传输。

| 验收项 | 用例 |
| --- | --- |
| 正式成员文件读写、越界与符号链接拒绝；指定旧版独立执行；覆盖只读版本失败；第三成员不可读、不可挂载、不可见宿主凭据 | [Docker 端到端用例](../../tests/test_sandbox.py) 的 `test_registered_members_file_tools_and_version_handoff` |
| 禁网、超时终止和容器回收 | [Docker 超时用例](../../tests/test_sandbox.py) 的 `test_no_egress_and_timeout_removes_container` |
| 固定版本摘要、版本各自读者、写入或登记失败后清理 | [产物存储用例](../../tests/test_artifact_store.py) |
| 跨团队拒绝、助手版授权和撤销、搜索来源写入及重新读取 | [工作区应用层联调](../../tests/test_workspace.py) |
| 固定端点、查询格式、来源过滤、重定向和错误处理 | [搜索传输用例](../../tests/test_search.py) |

Docker 验证使用真实隔离环境。文件工具和挂载的第三角色负例使用运行时登记身份。HTTP 下载受当时的本机可信操作者边界约束，该测试核对所选会话视角的版本授权。T002 增加了成员身份绑定。当前可从工作台下载，或使用成员令牌调用 `GET /v1/artifacts/{artifact_id}/versions/{version}` 下载指定数字版本。检索使用 HTTPX 测试传输，实际课程检索端点及许可由 T013 验收。待办领取及执行恢复由 T006 接入，历史回合分支由 T007 接入。
