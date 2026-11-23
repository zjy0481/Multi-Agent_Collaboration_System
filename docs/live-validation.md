# 真实环境联调记录

以下为既有实机验收记录。运行环境与联调命令见[本地环境配置指南](environment-setup.md)。

## T004 委托协商

使用 HKGAI 模型 `t2_hkgai-v3_fp8_1m_e7`。Lead 读取活动目标、创建草案、登记任务并更新至修订 2，负责人通过模型确认草案，其余确认通过同一 HTTP 接口提交。四个模型回合均完成，五次工具回执均匹配原调用 ID。复验时设置 `RUN_LIVE_DELEGATION_TESTS=1` 后运行[委托联调用例](../tests/test_delegation_live.py)。

## T006 持久调度

使用相同的课程模型和沙箱替身。模型先调用 `list_participants`，再使用返回的成员 ID 调用 `send_message` 向群聊发出进展。两次回执保留原调用 ID，回合状态为 `completed`。真实容器隔离见 [T005 验收记录](validation/t005-validation.md)。

## T013 真实端点与检索

- 课程模型使用 `course` 模式，连接 `api1.hkgai.net` 的 Chat Completions 接口，模型为 `t2_hkgai-v3_fp8_1m_e7`。真实模型生成缺省 Lead，并在两个模型步骤分别调用 `read_file` 和 `send_message`，把真实 Docker 工作区文件中的运行时随机值写入群聊。验收核对了两次工具调用 ID、文件内容和群聊记录。认证失败用 HTTP 401 测试传输验证。对应[课程模型联调用例](../tests/test_course_integration.py)。
- 真实检索使用本机 SearXNG `2026.11.13-8a53a6aa5` 的环回 JSON 接口，取得 SQLite 文档来源。检索用例验证结果重启后仍可按 `search_id` 读取，并保留来源和会话读取权限。真实模型依次调用 `search_web`、`get_search_result` 和 `send_message`，按原调用 ID 核对检索编号、来源 URL 和群聊消息。对应[检索联调用例](../tests/test_live_search.py)。
