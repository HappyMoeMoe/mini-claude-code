# 可重复演示与检查点

从产品目录执行，先 `python -m pip install -e '.[dev]'`。完整测试命令 `python -m pytest -q` 包含真实模型 API 用例，需要配置 Key 并消耗额度；只运行真实 API 用例可用 `python -m pytest -q -m real_api`，只运行离线回归可用 `python -m pytest -q -m "not real_api"`。下列独立真实模型演示脚本只有显式 --real 才执行。演示不自动批准模型生成的 Shell 命令。

## A：修复、验证与恢复

```bash
python examples/demo_real.py --real --scenario A
```

脚本创建临时 arithmetic.py 和固定 unittest。宿主先执行固定测试确认失败；真实模型读取、修复、回读源文件；宿主再次执行同一测试确认通过，并校验测试内容未变、恢复后的历史配对有效。模型 Shell 非交互拒绝，不伪称模型自行运行了测试。

要手动体验完整 CLI：在自己的演示项目中运行 `mini-claude-code --workspace /absolute/demo chat --minimal`，要求修复并验证。每次 Shell 调用都在终端查看完整命令后逐次决定，不能通过脚本输入 y 泛批命令。退出后用 sessions list 查询并显式 resume。

## B：子 Agent、Skills、产物与压缩

```bash
python examples/demo_real.py --real --scenario B
python -m pytest -q tests/test_agent_workflow.py tests/test_context.py
```

真实演示让 lead 有界委派一次 analyze，子会话读取 project.txt 并报告，主会话收到有来源的结果。完整低预算组合在离线测试中完成：Todo → Skill 元信息及正文 → 独立子会话 → 大输出产物 → 分页 → 压缩 → 新会话相关记忆。检查实际辅助调用、checkpoint、完整请求预算、取消/失败时旧状态保留，不以模型措辞是否一致判定通过。

## C：跨会话记忆

```bash
python examples/demo_real.py --real --scenario C
python -m pytest -q tests/test_memory.py
```

真实演示保存明确长期 pytest 偏好，检查真实记忆记录，再用新会话召回并核对回答。离线用例补充临时限制不入库、无关查询不强塞记忆、off 无辅助调用、删除与同源抑制、整理冲突与失败。管理命令 memory list/show/delete 不需要 Key；consolidate 需要模型。

## D：任务、后台与 Cron

```bash
python -m pytest -q tests/test_tasks_events.py
```

使用临时 workspace 和固定命令审批替身：依赖未满足不能认领；后台初始结果只代表启动，完成后持久事件唤醒，自动轮不读取审批；运行中不能 complete，关闭先收尾再释放。假 UTC 时钟验证 Cron 槽去重、IANA 时区、DST、leader、held/retry、接受点崩溃修复与取消。无需等待真实一分钟，也不把假时钟结果标成真实定时演示。

手动启用普通 chat，要求安排一个 Cron 后保持 CLI 打开；退出就不继续计时。用 schedule list 查询实际 job_id 与未接受事件；不要据 occurrence 生成就声称实际工作已完成。

## E：队友与独立 Worktree

```bash
python examples/demo_teams.py --real
python -m pytest -q tests/test_teams_worktrees.py
```

真实演示创建全新临时 Git 仓库与有效 commit，由 lead 派发两名真实模型队友，各修改独立 checkout 中的一个函数。主目录保持原样；宿主分别运行固定 unittest，接受完成事件并确认全部队友线程停止。没有自动提交或合并。离线用例进一步覆盖 required plan → 拒绝修订 → 新版本批准、旧批准失效、非交互 Shell 拒绝、目录租用、部分 Git 创建保留和关闭未确认不释放。

## F：MCP 动态目录、权限、产物与退出

```bash
python -m pytest -q tests/test_mcp.py
```

其中真实 stdio 用例启动 examples/mcp_server.py，通过统一管线发现 query/write_note/controlled_failure，验证临时写入审批替身、isError、超时未知状态和所属进程/线程回收。其他用例覆盖假传输分页、碰撞、Schema、快照、角色、计划 gate、产物和恢复不自动连接。全部离线，不连接外部 MCP。

手动体验：复制 examples/mcp.example.json，替换 Python 与脚本绝对路径，mkdir 一个 /tmp 下的演示目录，再执行 `mini-claude-code --workspace /tmp/mini-cc-mcp-demo chat --mcp-config /absolute/mcp.json`。输入 `/mcp connect demo` 后要求查询；写入时逐次查看 MCP 审批。用 `/mcp disconnect demo` 撤下工具，最后退出。

## 记录与清理

真实脚本打印临时目录、工具名及 PASS 检查点，正常或异常退出通过 TemporaryDirectory 清理本脚本创建的全部演示数据。需要保留记录时先复制脱敏日志；不将真实 Key、认证头或全环境转储写进演示报告。pytest 临时目录由 pytest 管理。

在真实项目使用 CLI 时，记录保存在该项目 .mini-claude-code；不会因退出删除文件变更、历史、任务或 checkout。用户项目的 Worktree 应按 worktrees list/remove 检查后处理，不按进程名称全局杀进程，不删除他人 checkout。实际执行结果以本地运行输出为准。
