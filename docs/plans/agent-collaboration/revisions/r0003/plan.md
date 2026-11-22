<!-- project-planner: PP-1 plan_id=pp-ba8ebd7d-1a02-4703-9797-328c57f1d5ff revision=r0003 -->
# 项目计划 pp-ba8ebd7d-1a02-4703-9797-328c57f1d5ff / r0003

basis_digest: `607f2dbeec7445e7366a0a993dab73b1ece378477ca7761834c522331e4a3914`

相对区间是整票保守规划，不代表运行门槛、完成或验收事实。
审阅与确认的当前结论请读取 [revision.json](revision.json) 和 [review.md](review.md)。
修订来源、影响及证据适用性见 [revision.json](revision.json)。

## 规格目录

- [S001: 团队、消息与任务协作](specs/S001.md)（对象修订 2）
- [S002: 模型运行、历史检查点与助手分支](specs/S002.md)（对象修订 1）
- [S003: 私有执行、产物版本与审查](specs/S003.md)（对象修订 1）
- [S004: 个人经验、评测与课程场景](specs/S004.md)（对象修订 2）

## 票据摘要

| 编号 | 票据 / 正文 | 工作量 | 完成输入 | 规格 | 交付路线 |
| --- | --- | --- | --- | --- | --- |
| 1 | [T001: 导入角色并建立可用团队](tickets/T001.md) | 3 | 未完成 | S001, S002, S003 | unspecified |
| 2 | [T002: 发送群聊与私聊并恢复可见消息](tickets/T002.md) | 3 | 未完成 | S001 | unspecified |
| 3 | [T003: 处理真人澄清与重启接续](tickets/T003.md) | 2 | 未完成 | S001, S002 | unspecified |
| 4 | [T004: 协商委托并登记任务修订](tickets/T004.md) | 3 | 未完成 | S001 | unspecified |
| 5 | [T005: 在私有工作区执行并交接版本](tickets/T005.md) | 3 | 未完成 | S003, S001 | unspecified |
| 6 | [T006: 用可重复的工具回合驱动持久待办](tickets/T006.md) | 3 | 未完成 | S002, S001 | unspecified |
| 7 | [T007: 从历史回合派助手完成一次受限协作](tickets/T007.md) | 3 | 未完成 | S001, S002, S003 | unspecified |
| 8 | [T008: 提交版本并完成局部返工](tickets/T008.md) | 2 | 未完成 | S001, S003 | unspecified |
| 9 | [T009: 关闭或终止目标并激活下一项](tickets/T009.md) | 2 | 未完成 | S001, S004 | unspecified |
| 10 | [T010: 从业务事实生成可重算报告](tickets/T010.md) | 2 | 未完成 | S004 | unspecified |
| 11 | [T011: 按真人批准执行 Git 推送](tickets/T011.md) | 2 | 未完成 | S001, S002, S003 | unspecified |
| 12 | [T012: 记录个人经验并跨题验证采用](tickets/T012.md) | 2 | 未完成 | S002, S003, S004 | unspecified |
| 13 | [T013: 联调课程端点与真实工具](tickets/T013.md) | 2 | 未完成 | S002, S003 | unspecified |
| 14 | [T014: 贯通协作、权限与故障恢复验收](tickets/T014.md) | 3 | 未完成 | S001, S002, S003, S004 | unspecified |
| 15 | [T015: 运行三组官方样例各两题](tickets/T015.md) | 3 | 未完成 | S001, S002, S003, S004 | unspecified |
| 16 | [T016: 验收一次获准的 GitHub 交付](tickets/T016.md) | 1 | 未完成 | S003 | unspecified |
| 17 | [T017: 运行中期与期末闭集评测](tickets/T017.md) | 3 | 未完成 | S004 | unspecified |
| 18 | [T018: 汇总课程展示与正式交付材料](tickets/T018.md) | 2 | 未完成 | S004 | unspecified |

## 覆盖索引

| 需求 / 来源位置 | 处置 | 规格 | 票据 | 验证归属 | 缺口 / 决定 |
| --- | --- | --- | --- | --- | --- |
| R001 / REQ: 建组和增员 | arranged | S001, S003 | T001, T004, T005, T014 | T001#acceptance, T004#acceptance, T014#acceptance | — |
| R002 / REQ: 自主拆解、确认与交付 | arranged | S001, S003 | T004, T008, T014 | T004#acceptance, T008#acceptance, T014#acceptance | — |
| R003 / REQ: 群聊、私聊与人工决定／多问题与重启 | arranged | S001, S002 | T002, T003, T014 | T002#acceptance, T003#acceptance, T014#acceptance | — |
| R004 / REQ: 指定旧回合建分支／助手参与首次委托协商 | arranged | S001, S002, S003 | T004, T005, T006, T007, T014 | T007#acceptance, T014#acceptance | — |
| R005 / REQ: 跨参与者隔离 | arranged | S001, S002, S003, S004 | T002, T005, T007, T010, T012, T014 | T014#acceptance | — |
| R006 / REQ: 草案过期／非阻塞答疑 | arranged | S001, S002 | T004, T006, T007, T014 | T007#acceptance, T014#acceptance | — |
| R007 / REQ: 终止时仍有在途工作／必需交付与下一目标 | arranged | S001, S004 | T009, T010, T014 | T009#acceptance, T014#acceptance | — |
| R008 / REQ: 局部返工 | arranged | S001, S003 | T008, T009, T014 | T008#acceptance, T014#acceptance | — |
| R009 / REQ: 工具执行与外部授权／真实模型与外部工具 | arranged | S002, S003 | T005, T006, T011, T013, T014, T016 | T013#acceptance, T014#acceptance, T016#acceptance | — |
| R010 / REQ: 授权绑定与过期 | arranged | S001, S002, S003 | T011, T014, T016 | T011#acceptance, T014#acceptance, T016#acceptance | — |
| R011 / REQ: 个人经验与跨题延续／记忆与两题连续 | arranged | S002, S003, S004 | T012, T014, T015 | T012#acceptance, T014#acceptance, T015#acceptance | — |
| R012 / REQ: 指标可重算／登记前等待纳入评测 | arranged | S001, S004 | T009, T010, T014 | T010#acceptance, T014#acceptance | — |
| R013 / REQ: 开源复用与本组实现边界 | arranged | S001, S002, S003, S004 | T001, T002, T005, T006, T007, T011, T012, T013, T014 | T013#acceptance, T014#acceptance | — |
| R014 / COURSE: 2.4 File Format Constraints / 2.5 User Interface | arranged | S001, S004 | T002, T003, T008, T014, T015, T018 | T002#acceptance, T003#acceptance, T008#acceptance, T014#acceptance, T015#acceptance, T018#acceptance | — |
| R015 / COURSE: 2.6 Standardized LLM Backbone | arranged | S002 | T006, T013 | T006#acceptance, T013#acceptance | — |
| R016 / SAMPLES: Three Multi-Agent Collaboration Scenarios | arranged | S001, S002, S003, S004 | T015 | T015#acceptance | — |
| R017 / COURSE: Evaluation & Metrics / The Final Evaluation Dataset | arranged | S004 | T017 | T017#acceptance | — |
| R018 / COURSE: Deliverables and Submission Requirements | arranged | S004 | T018 | T018#acceptance | — |
| R019 / REQ: U-001 基金第二题正式提交格式 | arranged | S004 | T018 | T018#acceptance | — |
| R020 / REQ: U-002 正式评测输入与端点条件 | arranged | S002, S004 | T013, T017 | T013#acceptance, T017#acceptance | — |

## 依赖与条件

后置门槛保留原用途；完整区间放在全部必要交付之后，不禁止已有确认的提前工作。

| 依赖 | 消费者 | 提供者 | 用途 | 判断 | 所需交付 / 门槛 | 依据 / 提前决定 |
| --- | --- | --- | --- | --- | --- | --- |
| D001 | T002 | ticket:T001 | start | planned_delivery | 团队、参与者和主会话 / 三组角色导入验证通过，可查询稳定的团队、参与者与会话 ID，并已进入本票可用的共同版本 | tickets/T001.md#acceptance / None |
| D002 | T003 | ticket:T002 | start | planned_delivery | 消息写入与引用投递 / 群聊和私聊的消息 ID、引用入口和投递持久化可联合运行，并已进入本票可用的共同版本 | tickets/T002.md#acceptance / None |
| D003 | T004 | ticket:T002 | start | planned_delivery | 已登记活动目标、原始布置消息和事件通知 / Lead 可按原消息幂等登记活动目标，并能在共同版本按权限投影后续通知 | tickets/T002.md#acceptance / None |
| D004 | T005 | ticket:T001 | start | planned_delivery | 角色工作区的登记、准备和查找 / 初始及新增成员有通过隔离检查的工作区映射，并已进入本票可用的共同版本 | tickets/T001.md#acceptance / None |
| D005 | T006 | ticket:T002 | start | planned_delivery | 可领取的消息待办及会话身份 / 重启后仍可读取可领取的定向消息待办，稳定识别其执行会话，并已进入共同版本 | tickets/T002.md#acceptance / None |
| D006 | T007 | ticket:T004 | start | planned_delivery | 首次协商和既有任务修订 / 同版确认、草案范围和过期处理已通过拟定用例，并已进入本票可用的共同版本 | tickets/T004.md#acceptance / None |
| D007 | T007 | ticket:T005 | start | planned_delivery | 授权材料版本和助手工作区 / 部署支持隔离工作区，可按版本授权和导入，并已进入本票可用的共同版本 | tickets/T005.md#acceptance / None |
| D008 | T007 | ticket:T006 | start | planned_delivery | 成组回合、领取、代次和独立模型推进 / 完成回合、工具配对、可恢复回执与并行执行已经验证，并已进入本票可用的共同版本 | tickets/T006.md#acceptance / None |
| D009 | T008 | ticket:T004 | start | planned_delivery | 任务 ID、有效修订、提交人和审查人关系 / 委托登记和修订历史可按授权读取，并已进入本票可用的共同版本 | tickets/T004.md#acceptance / None |
| D010 | T008 | ticket:T005 | start | planned_delivery | 不可变产物的发布与获准读取 / 可用固定版本作审查证据并复现，并已进入本票可用的共同版本 | tickets/T005.md#acceptance / None |
| D011 | T009 | ticket:T003 | start | planned_delivery | 人类问题的阻塞和撤销关联 / 重启及引用答复验证通过，可查有效阻塞与工作范围，并已进入本票可用的共同版本 | tickets/T003.md#acceptance / None |
| D012 | T009 | ticket:T008 | start | planned_delivery | 当前通过证据和下游待复核 / 可对当前任务修订读取稳定验收事实，并已进入本票可用的共同版本 | tickets/T008.md#acceptance / None |
| D013 | T009 | ticket:T007 | start | planned_delivery | 分支撤权和助手终止 / 可关闭指定工作范围并阻止旧代次继续生效，并已进入本票可用的共同版本 | tickets/T007.md#acceptance / None |
| D014 | T010 | ticket:T009 | start | planned_delivery | 目标终态、冻结事实及登记前终止记录 / 可稳定读取已终结目标的事实，并从消息查询登记前终止，并已进入本票可用的共同版本 | tickets/T009.md#acceptance / None |
| D015 | T011 | ticket:T003 | start | planned_delivery | 真人交互入口和受阻工作 / 浏览器可接收针对特定事件的人工输入，引用及等待记录可持久化，并已进入本票可用的共同版本 | tickets/T003.md#acceptance / None |
| D016 | T011 | ticket:T005 | start | planned_delivery | 可运行受控命令的隔离工作区 / 授权身份可定位隔离工作区并运行受控命令，工作区操作已在共同版本验证 | tickets/T005.md#acceptance / None |
| D017 | T011 | ticket:T006 | start | planned_delivery | 模型调用 ID 和可恢复的待办 / 待办能投递到原会话，主流程有重放和旧代次检查，并已进入本票可用的共同版本 | tickets/T006.md#acceptance / None |
| D018 | T012 | ticket:T007 | start | planned_delivery | 显式交接范围与受限助手读取 / 可对作者、授权助手和第三方使用同一来源投影，并已进入本票可用的共同版本 | tickets/T007.md#acceptance / None |
| D019 | T013 | ticket:T005 | start | planned_delivery | 实际检索所用的工具适配及来源记录 / 受控工具可提交检索结果及来源，并已进入本票可用的共同版本 | tickets/T005.md#acceptance / None |
| D020 | T013 | ticket:T006 | start | planned_delivery | 模型适配器和可重放工具闭环 / 离线双调用和错误分类通过，能够配置课程端点，并已进入本票可用的共同版本 | tickets/T006.md#acceptance / None |
| D021 | T014 | ticket:T007 | acceptance | planned_delivery | 历史分支、协商与权限正反例 / 可在共同基线证明中途分支及父子并行，并已进入本票可用的共同版本 | tickets/T007.md#acceptance / None |
| D022 | T014 | ticket:T010 | integration | planned_delivery | 结案冻结、队列及指标输出 / 报告能从可定位的业务记录重算且权限投影可用，并已进入本票可用的共同版本 | tickets/T010.md#acceptance / None |
| D023 | T014 | ticket:T011 | integration | planned_delivery | 审批状态及 Git 核对 / 本地可控远端上有拒绝、重放及未知结果的记录，并已进入本票可用的共同版本 | tickets/T011.md#acceptance / None |
| D024 | T014 | ticket:T012 | integration | planned_delivery | 跨题经验读写及采用记录 / 能用已授权与未授权操作者复现实例，并已进入本票可用的共同版本 | tickets/T012.md#acceptance / None |
| D025 | T015 | ticket:T013 | start | planned_delivery | 真实课程模型和工具配置 / 两步真实工具已由实际端点完成并保全证据，并已进入本票可用的共同版本 | tickets/T013.md#acceptance / None |
| D026 | T015 | ticket:T014 | start | planned_delivery | 通过离线联验的完整系统基线 / 组合行为已经在共同版本有实际证据，并已进入本票可用的共同版本 | tickets/T014.md#acceptance / None |
| D027 | T016 | ticket:T011 | acceptance | planned_delivery | 宿主的 GitHub 绑定批准及回执 / 本地夹具验证了审批绑定、幂等和结果核对，并已进入本票可用的共同版本 | tickets/T011.md#acceptance / None |
| D028 | T017 | ticket:T013 | start | planned_delivery | 实际端点、模型和工具传输 / 真实工具闭环与课程授权的配置可用，并已进入本票可用的共同版本 | tickets/T013.md#acceptance / None |
| D029 | T017 | ticket:T014 | start | planned_delivery | 可报告且通过协作联验的完整基线 / 能使用同一版本重放主要功能并生成可重算报告，并已进入本票可用的共同版本 | tickets/T014.md#acceptance / None |
| D030 | T018 | ticket:T015 | integration | planned_delivery | 三组样例、连题和经验采用案例 / 六题成功与失败的证据可定位，并已进入本票可用的共同版本 | tickets/T015.md#acceptance / None |
| D031 | T018 | ticket:T016 | acceptance | planned_delivery | GitHub 真实交付的证据 / 实际远端 ref 与获准 SHA 的匹配及回执可用，并已进入本票可用的共同版本 | tickets/T016.md#acceptance / None |
| D032 | T018 | ticket:T017 | integration | planned_delivery | 两次评测和正式性能分析 / 实际闭集运行及模型、数据、代码版本均可追溯，并已进入本票可用的共同版本 | tickets/T017.md#acceptance / None |
| D033 | T013 | external:E001 | acceptance | unknown | 课程认可端点、模型、凭据与联调配额可用 / 课程认可端点、模型、凭据与联调配额可用 | review.md#external-conditions / None |
| D034 | T013 | external:E002 | acceptance | unknown | 获准实际检索来源和调用权限可用 / 获准实际检索来源和调用权限可用 | review.md#external-conditions / None |
| D035 | T016 | external:E003 | acceptance | unknown | 真实 GitHub 验收仓库、目标 ref 及真人批准可用 / 真实 GitHub 验收仓库、目标 ref 及真人批准可用 | review.md#external-conditions / None |
| D036 | T017 | external:E004 | start | unknown | 中期及期末闭集输入、发放和评测安排已明确 / 中期及期末闭集输入、发放和评测安排已明确 | review.md#external-conditions / None |
| D037 | T018 | external:E005 | integration | unknown | 基金第二题正式提交格式和提交安排已明确 / 基金第二题正式提交格式和提交安排已明确 | review.md#external-conditions / None |
- 外部 E001: 课程认可端点、模型、凭据与联调配额可用；偏移 None；依据 review.md#external-conditions
- 外部 E002: 获准实际检索来源和调用权限可用；偏移 None；依据 review.md#external-conditions
- 外部 E003: 真实 GitHub 验收仓库、目标 ref 及真人批准可用；偏移 None；依据 review.md#external-conditions
- 外部 E004: 中期及期末闭集输入、发放和评测安排已明确；偏移 None；依据 review.md#external-conditions
- 外部 E005: 基金第二题正式提交格式和提交安排已明确；偏移 None；依据 review.md#external-conditions

## 资源、容量与限制

容量：2；依据：用户在本轮明确同时推进两张票。
参照票：{"ticket_id": "T016", "reason": "固定仓库和提交的一次真实回执验证作为相对单位。估算包含验证和整理证据。"}
规划假设：["相对工量单位，外部等待单独体现在就绪条件。", "共同版本经过上游验收后视为相应共享交付可用。", "当前首版受理的组织路线未指定，计划按逻辑交付和验收归属编排。"]

## 整票区间

| 票据 | 半开区间 | 必要依赖 |
| --- | --- | --- |
| T001 | [0, 3) |  |
| T002 | [3, 6) | D001 |
| T003 | [6, 8) | D002 |
| T004 | [6, 9) | D003 |
| T005 | [3, 6) | D004 |
| T006 | [8, 11) | D005 |
| T007 | [11, 14) | D006, D007, D008 |
| T008 | [9, 11) | D009, D010 |
| T009 | [14, 16) | D011, D012, D013 |
| T010 | [16, 18) | D014 |
| T011 | [11, 13) | D015, D016, D017 |
| T012 | [14, 16) | D018 |
| T014 | [18, 21) | D021, D022, D023, D024 |
- T013 区间未定：gate_unknown；dependency:D033, dependency:D034
- T015 区间未定：gate_unknown, upstream_undated；dependency:D025, dependency:D033, dependency:D034, ticket:T013
- T016 区间未定：gate_unknown；dependency:D035
- T017 区间未定：gate_unknown, upstream_undated；dependency:D028, dependency:D033, dependency:D034, dependency:D036, ticket:T013
- T018 区间未定：gate_unknown, upstream_undated；dependency:D025, dependency:D028, dependency:D030, dependency:D031, dependency:D032, dependency:D033, dependency:D034, dependency:D035, dependency:D036, dependency:D037, ticket:T013, ticket:T015, ticket:T016, ticket:T017

## 实际重叠组

- [3, 6): T002, T005
- [6, 8): T003, T004
- [8, 9): T004, T006
- [9, 11): T006, T008
- [11, 13): T007, T011
- [14, 16): T009, T012

## 潜在并行候选

以下仅为完整依赖层与资源允许的候选；所列条件满足后仍须受容量约束，不能据此声称当前可启动。

- T002, T005；条件：D001, D004。
- T003, T004, T006；条件：D002, D003, D005。
- T007, T008, T011, T013；条件：D006, D007, D008, D009, D010, D015, D016, D017, D019, D020, D033, D034。
- T009, T012, T016；条件：D011, D012, D013, D018, D027, D035。
- T015, T017；条件：D025, D026, D028, D029, D036。

结构检查不能判断规格充分性、实际权限、共享基线验证、提前方案接受或验收充分性；这些须由调用者及独立审阅核对。
