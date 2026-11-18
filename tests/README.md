# 测试说明

本目录包含调用真实模型 API 的功能验收，以及可重复构造异常的离线回归测试。
默认 `python -m pytest -q` 执行全部用例，包含真实 API 请求。

| 功能 | 测试文件 |
|---|---|
| 真实模型 API 功能验收 | `test_real_api.py` |
| 配置、CLI 与功能开关 | `test_config_cli.py`、`test_profiles.py` |
| 模型协议、流式响应与主执行循环 | `test_model_protocol.py`、`test_runtime.py` |
| 会话持久化与恢复 | `test_session_storage.py` |
| Todo、Skills 与子 Agent | `test_agents_skills.py` |
| 上下文、产物分页与历史预算 | `test_context.py`、`test_history_index_budget.py` |
| 跨会话记忆与证据来源 | `test_memory.py`、`test_memory_provenance.py` |
| 多模块协同 | `test_agent_workflow.py` |
| 任务、后台执行、Cron 与事件重试 | `test_tasks_events.py`、`test_event_retry_input.py` |
| 团队、计划审批与任务恢复 | `test_teams_worktrees.py`、`test_plan_review_recovery.py`、`test_team_task_recovery.py`、`test_team_event_retry.py` |
| 任务完成权限 | `test_task_completion_permissions.py` |
| 取消与进程、线程清理 | `test_runtime_entry_cancellation.py`、`test_teammate_startup_cancellation.py`、`test_shell_cleanup_failure.py` |
| Git 执行边界与 Worktree 恢复 | `test_git_execution_policy.py`、`test_worktree_verification_recovery.py` |
| MCP 接入、Schema、发现时限与连接清理 | `test_mcp.py`、`test_mcp_schema_limits.py`、`test_mcp_discovery_deadline.py`、`test_mcp_cleanup_confirmation.py` |
| 控制台输入、输出脱敏与文件类型边界 | `test_console_input.py`、`test_display_redaction.py`、`test_regular_file_reads.py` |

`test_real_api.py` 使用真实 `DeepSeekClient` 和生产 Runtime，覆盖非流式与流式响应、代码修复与会话恢复、子 Agent、跨会话记忆、上下文压缩、本地 MCP 调用以及双队友 Worktree 协作。
其中修复、子 Agent、记忆和团队场景复用 `examples/` 中的真实模型演示检查；所有响应均来自配置的模型 API。
用例检查工具结果、文件内容、固定 unittest 结果、检查点和资源回收，不只检查模型是否返回文本。

`conftest.py` 为离线回归提供公共模型替身、临时工作区和资源清理。带 `real_api` 标记的用例保留真实凭证并允许联网。
部分测试文件也提供其他用例复用的辅助函数，移动或删除时需要同步修改导入。
`examples/mcp_server.py` 是 MCP 测试使用的本地 stdio 服务，需要保留。

`test_agent_workflow.py` 验证 Todo、Skills、子 Agent、产物分页、压缩和跨会话记忆能共同工作，属于产品集成测试。
各类取消、恢复、超时和权限测试验证异常输入下的功能行为，属于长期回归测试。
这些测试并非仅用于某个开发阶段的临时验收脚本。

使用 Python 3.11 或更新版本，在仓库根目录执行：

```bash
python -m pip install -e '.[dev]'

# 配置仓库根目录 .env 中的 DEEPSEEK_API_KEY 后，执行完整测试
python -m pytest -q

# 只运行真实模型 API 功能验收
python -m pytest -q -m real_api

# 只运行离线回归，无需 API Key
python -m pytest -q -m "not real_api"
```

真实 API 测试读取应用 `.env` 或进程环境中的配置，会消耗 API 额度；未配置凭证时明确报错，不跳过或回退到模型替身。
真实服务的网络故障、限流、凭证错误和模型行为不符合断言都会使测试失败。测试仅向模型发送临时构造的项目数据。
原有离线用例保留网络拦截与测试凭证，用于精确验证重试、取消、存储损坏和权限边界。

真实 MCP stdio 测试需要允许启动本地子进程，以及通过本地 `socketpair` 唤醒 asyncio I/O 线程。
若沙箱禁止本地 socket 写入，连接和关闭操作可能超时，并进一步报告资源未确认停止。
遇到这种情况，应在允许本地进程通信的测试环境中复测。真实 API 用例还需要允许访问配置的模型服务。
