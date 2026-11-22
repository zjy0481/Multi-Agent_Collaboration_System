# AGENTS.md

项目是一个课程用多智能体协作平台。

## 阅读指引

- [课程要求](docs/raw/basic_requirement.md)列出必备能力、评测指标和提交材料。
- [平台初步设计](docs/preliminary-proposal.md)介绍技术路线、团队分工与协作流程。
- [详细需求](docs/grill_result/agent-collaboration/requirement.md)定义产品行为、权限和验收条件。
- [领域模型](docs/grill_result/agent-collaboration/domain-model.md)描述业务对象、关系与状态变化。
- [运行时对接设计](docs/grill_result/agent-collaboration/runtime-contract.md)说明接口、数据归属与故障恢复。
- [官方样例](<test samples/README.md>)收录三组角色配置和六项连续任务。

## Git 提交

创建提交前读取[Agent 提交规范](.codex/policies/AGENT_COMMIT_GUIDELINES.md)。每个提交对应一项逻辑变更，并执行与该变更相关的检查。

- 标题格式为 `type(scope): 中文摘要 / english summary`。范围可省略，填写时用简短的小写英文名词。两段摘要均用祈使语气、含义一致、末尾无标点，英文用小写。标题尽量控制在 100 字符内，最长 120 字符。
- 类型从 `feat`、`fix`、`docs`、`style`、`refactor`、`perf`、`test`、`build`、`ci`、`chore`、`revert` 中选择。
- 复杂变更、缺陷修复及破坏性变更须在正文分别用 `Chinese:` 和 `English:` 说明改动、原因及影响。破坏性变更还须在标题冒号前加 `!`，并附双语 `BREAKING CHANGE:` 页脚。关联实际 issue 时按规范使用 `Refs:` 或 `Closes:`。

文档提交示例为 `docs(model): 明确任务验收条件 / clarify task acceptance criteria`。
