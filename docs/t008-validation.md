# T008 任务结果与审查

1. 负责人调用 `publish_artifact` 发布确切版本并赋予主审读取权，再用 `submit_result` 提交任务 ID、预期修订、`version_ids`、验证证据与遗留问题。提交自动提醒主审、Lead、派发者，同一收件人只收到一条通知；通知本身不授予版本读取权。
2. 主审用 `get_task` 查看当前提交和证据，再用 `review_result` 对确切提交填写逐项依据（验收条件从 1 编号）。通过要求全部达标；退回必须记录未达标项、预期和复验项。两个写入工具均要求 `request_id`，同参重放返回原回执。
3. 退回后向原任务重交新产物版本。固定引用被替换版本的下游显示为待复核，需调整输入绑定并重新提交、审查；其他任务保留原结论。旧提交、版本与主审决定继续按原权限可查。

对应 HTTP 路径为 `POST /v1/teams/{team_id}/tasks/{task_id}/submissions` 和 `POST /v1/teams/{team_id}/tasks/{task_id}/submissions/{submission_id}/review`。用有权成员令牌登录工作台，打开「任务交付」中的任务，展开历史或当前提交，即可查看固定版本链接、决定与依据。仅有任务权限、没有产物权限的成员可看到流程状态，不会获得对应提交的详情。

结案消费者应在自己的校验中调用 `delegation.results.current_acceptance(team_id, actor, goal_id, task_id)`，而不是从历史记录挑选通过决定。该方法只返回当前任务修订、未被旧输入阻塞、已经通过且所有固定版本实际可读的事实；目标验收约定、其他阻塞和目标关闭仍由结案流程负责。

运行 `python -m pytest -q tests/test_task_results.py` 复验提交、版本权限、通知、返工、并发审查、重启和当前验收；测试不调用外部模型。
