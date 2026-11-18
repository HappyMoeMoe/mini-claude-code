# 使用与排错

完整参数、额度和机制介绍见 [README](../README.md)。根参数放在子命令之前；chat 专属参数放在 chat 后。--memory 可放在根或 chat 中，后者优先。

```bash
mini-claude-code --workspace /path/to/project check
mini-claude-code --workspace /path/to/project --max-rounds 12 chat --minimal
mini-claude-code --workspace /path/to/project chat --disable skills,subagents,scheduler --memory off
mini-claude-code --workspace /path/to/project chat --teams
mini-claude-code --workspace /path/to/project chat --mcp-config /absolute/path/mcp.json
```

## 配置档

默认启用 Todo、Skills、一次性子 Agent、记忆、任务图、后台和 Cron；团队须 --teams，MCP 须显式配置。--minimal 保留五个基础编程工具、权限/Hooks、保存恢复及基本上下文管理（compact 和 read_tool_output），其余能力关闭，不启动事件或调度服务。

--disable 只接受 skills、subagents、tasks、background、scheduler，逗号分隔，不接受空项或其他名称。它们从工具池和相应提示中移除；直接伪造调用也不能执行。memory 使用 --memory off；团队默认关闭；MCP 不传配置即关闭。关闭 tasks 仍保留目录占用和资源安全检查，不删除任务记录或转移已有认领。

minimal 与显式 --teams、--mcp-config、--memory on 冲突；默认 memory=on 会由 minimal 关闭。teams 依赖 tasks，不能同时禁用。禁用 scheduler 不启动计时线程，也不获取 leader 锁。已保存的数据仍保留。

## 配置文件位置

源码安装固定读取产品根 .env。独立 wheel 安装读取 `~/.config/mini-claude-code/.env`，不读取 site-packages 父目录或目标项目 .env。创建该目录和文件后填入 DEEPSEEK_API_KEY、MODEL_ID、DEEPSEEK_BASE_URL。进程环境优先于文件，CLI 模型/地址优先级更高。help/version 不读取文件；check 仅本地检查，不证明凭证有效。

MCP env_from 只引用当前进程环境，不会自动导出应用 .env 的值。配置样例中的 command 和脚本路径必须替换成自己的绝对路径。

## 会话与控制台

MCP 的 timeout_seconds.list 是整次目录发现预算（默认 15 秒），分页获取、所有输入/输出 Schema 定义校验和目录发布共用。单个定义校验最多使用剩余总预算与 2 秒单次上限的较小值；总超时会中止发现、回收校验进程并撤下目录。预算较小时也可能拒绝简单合法 Schema，因为进程启动计入预算。后续工具参数/结果校验各自使用单次上限，不沿用发现期限。

/exit 或 EOF 正常退出；执行中 Ctrl+C 取消本轮，空闲 Ctrl+C 退出。Shell 展示完整命令、cwd 和超时后，只有小写 y 批准本次，非交互 ask 拒绝。MCP ask 展示 Server、原名、副作用和脱敏参数，v 可查看完整脱敏参数，再决定是否批准。没有长期批准命令。MCP 本地 Schema 校验单次限 2 秒、校验子进程地址空间限 256 MiB；超限明确拒绝或报告输出校验失败，不忽略约束、不重跑远端调用。

| 控制台命令 | 用途 |
|---|---|
| /compact [focus] | 空闲时压缩工作视图，原始历史保留 |
| /todos、/skills、/memory | 查看当前轻量计划、技能目录或记忆管理说明 |
| /background、/background cancel ID | 查询或取消本 Runtime 后台执行 |
| /events、/events retry ID | 查看通知及显式重试 held 通知，不重启原命令 |
| /team、/team stop NAME | 查询团队、请求停止并等待真实回收 |
| /mcp、/mcp connect/refresh/disconnect NAME | 查看、连接、刷新或关闭显式配置的 Server |

压缩后的历史原文索引仅展示预算内的近期摘录，并标明省略数量及原会话日志引用。摘录最多占 4,000 个序列化字符，且不超过上下文预算的 10%；完整原始历史仍保留。旧检查点可继续恢复，恢复本身不请求模型；没有新增 CLI 配置参数。

关闭的机制会返回关闭说明。大输出使用结果中的 artifact_id 调用 read_tool_output；offset 是从 0 开始的行号，超长单行可结合 char_offset 分段读取。产物读取有会话归属与目录检查。

/events retry 的 ID 不存在、格式错误或参数缺失/多余时，只提示错误并继续等待输入；请从 /events 复制当前会话的完整 ID。其他会话的事件不能重试，只有未撤销的 held 事件可以重新投递。已有事件文件损坏或不可读取时，仍按存储故障停止处理。

重试还要求事件来源在当前会话中可投递。恢复后旧团队的通知，以及团队未启用或身份不匹配的通知，会明确拒绝重试并保留原记录，不会显示重试成功。旧团队 held 通知不会自动迁移到新团队，也不会重新激活旧计划或控制协议；当前团队的合法通知、Cron 和后台结果仍按原规则处理。

## 管理与恢复

```bash
mini-claude-code --workspace /path/to/project sessions list --all
mini-claude-code --workspace /path/to/project chat --resume session_<uuid>
mini-claude-code --workspace /path/to/project tasks list
mini-claude-code --workspace /path/to/project tasks show task_<uuid>
mini-claude-code --workspace /path/to/project memory list
mini-claude-code --workspace /path/to/project memory show memory_<uuid>
mini-claude-code --workspace /path/to/project schedule list
mini-claude-code --workspace /path/to/project worktrees list
```

这些查询不需要 Key、不调用模型、不启动服务。memory delete ID 删除并抑制同源事实；consolidate 需要模型。tasks release ID 要求用户检查旧执行链并交互确认；不会回滚变更。worktrees remove NAME 仅允许任务已完成、无活动租用且 Git 完全干净的 checkout，保留分支和记录。示例 ID 须替换为实际查询所得值。

complete_task 仅允许真实 owner 完成任务；已完成任务的重复确认仍限原完成者的同一 agent_id/session_id，现存 assignment 也须匹配原任务和 claim。队友不能通过重复确认读取其他团队或未分组任务，合法重复不会覆盖简报、重复发送完成事件或断开 MCP 连接。

宿主内部 Worktree/Git 操作按次禁用仓库 Hook、fsmonitor 和自动维护，不修改用户配置或 Hook 文件。若有效配置含非空的 clean/smudge/process 外部过滤器（包括 Git LFS），自动操作会明确拒绝，不执行过滤器，也不忽略过滤器生成另一种 checkout 内容。确需运行这些项目程序时，在主会话中通过逐次审批的 Shell 执行；已批准的用户 Git 命令保留原本配置行为。

恢复时若完整 Worktree 因 Git 超时、不可用或策略拒绝而无法校验，会提示原因并保留原 ready 记录，认领与执行仍被校验规则约束。解决原因后可重新认领，无需修改任务/操作日志，已有文件保持原样。这里的 ready 表示创建事务已完整提交，不代表本次校验通过；已有 partial/failed 或未完成事务不会被自动提升。

取消尚未确认的新队友启动会停止该次新队友，已有队友继续运行。启动中止后最多等待 3 秒确认收尾；超时则报告资源未确认、保留认领并停止新工作，需检查遗留状态。

resume 读取消息、Todo、上下文 checkpoint 等数据，重新按当前配置计算权限，等待新输入；不自动重放工具、重启进程、复活队友或连接 MCP。事件未接受时 held。计划旧批准不能作用于新任务。任务 completed 是执行者报告，仍须检查文件和真实验证记录。

同一会话以 --resume 和 --teams 恢复后，可以让 lead 显式调用 spawn_teammate(task_id=旧任务ID) 重新派发自己的旧团队任务。仅支持 pending、无人认领且 ready 的任务；旧成员须停止、会话锁可验证且旧团队没有遗留认领。若仍有认领，先检查旧执行链，退出聊天后使用 tasks release 显式释放，再恢复并派发。不会自动迁移或继承旧计划批准，Worktree 和已有修改保留。

review_plan 引用旧团队、其他团队或不存在的请求时返回普通工具错误，当前轮可继续纠正请求。请使用当前队友提交的真实 request_id；若当前团队仍引用某请求而文件丢失，或已有协议文件损坏、不可读取，则停止执行并保留原记录。

## 常见失败

取消发生在任务校验或技能发现等执行轮入口时，也会按本轮取消处理，清理所属后台工作并在安全时释放认领，随后可以继续输入。预先取消的输入不会写入历史；无法确认清理完成时仍保留认领并阻止新工作。lead 单轮取消不停止已有持久队友，子 Agent 的取消不释放父任务认领。

Shell 命令已启动后，若进程回收或管道清理失败，会中止本批剩余工具并保留任务认领，不能继续 complete_task 或写入。即使之后进程收尾成功，也需检查实际状态后再恢复；不会把回收权限错误当作普通的执行前拒绝。

MCP 只有在进程组与传输资源全部回收后才报告已停止。回收超时或失败时，会保留任务认领并阻止新工作；即使后台稍后收尾完成，也不会自动恢复执行，应先检查资源和任务状态。

配置错误退出 2；关键存储或协议无法继续退出 1；空闲中断 130；普通退出 0。普通工具错误是发回模型的结果，不等于会话崩溃。模型请求失败可继续输入，已完成文件变更保留。

遇到 execution_unknown，请按 session、call_id、task/claim 或 MCP generation 检查实际状态，不直接重放。未确认资源停止时宿主保留认领。关键日志损坏或配额耗尽时保留数据并停止新副作用，先检查本地记录和备份；不要靠清空 .mini-claude-code 假装修复。

Shell 与本地 MCP 程序没有 OS 沙箱；自行逃离所属进程组的进程无法由该组回收保证覆盖。队友 Shell 默认拒绝，需由 lead 向用户请求命令审批。CLI 退出后不调度；Worktree 不自动合并；记忆可能过期；压缩可能丢失细节；MCP 只支持已声明协议与工具范围。
