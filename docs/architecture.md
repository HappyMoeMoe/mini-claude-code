# 架构与来源对照

本产品面向 Linux，是一个模块化 Python CLI；模型通过 DeepSeek 的 Anthropic Messages 兼容接口工作。它不承诺与官方 Claude Code 兼容。源码位于 src/mini_claude_code，不在运行时导入教程代码。

## 一次执行的数据流

CLI 加载宿主配置，Runtime 组装一个 Session、模型客户端、AgentLoop、ToolRegistry、ToolExecutor、ContextManager 和本地存储。Runtime 创建于哪个线程，就由该线程提交模型消息、执行意图和结果。一次性子 Agent 与持久队友使用相同组装入口，分别有独立身份、客户端、历史、取消令牌及上下文视图。

AgentLoop 先组装实际可见工具及系统提示，计算完整请求预算，再请求模型。流式文本可显示，但必须收到完整且有效的响应后才执行工具。ToolExecutor 是统一入口：参数校验、角色和目录检查、PreToolUse、权限及单次审批、重新检查认领和 binding、保存意图、执行、产物转换、保存唯一结果、PostToolUse。Hook 不能扩大权限。关键存储失败会停止新的副作用。

工具调用展示先经过 Runtime.display：递归遮蔽 password/passwd/passphrase、API/私钥/访问/签名/加密密钥、token/secret/authorization/credential 及中文密码/口令/私钥/密钥/凭证字段，兼容大小写及常见分隔符；普通查询 key 与 public_key 保留。并替换当前模型 API key 与显式 MCP env_from 注入值（至少四字符）。主会话、子会话和队友队列均只收到展示副本；MCP 审批共用同一脱敏规则。ConsoleBroker 另对敏感字段做防御性遮蔽。执行器、模型历史与持久会话仍保留原始参数，脱敏不等于会话文件加密，也不保证识别任意自由文本中的未知凭证。

模型请求的有限重试只重试请求，不重放已执行工具。历史保留全部原始消息；ContextManager 使用独立 checkpoint 和工作视图进行有损压缩，失败保留旧状态。大型输出由 ArtifactStore 限额保存，模型通过 read_tool_output 分页取回。

压缩背景的历史用户摘录只保留预算内最近的一段，按序列化后的 JSON 字符数计费，避免引号、反斜线或控制字符低估开销。摘录预算为 ContextConfig.history_index_chars（默认 4,000）与总预算 10% 的较小值；统计说明与会话/journal 引用另有固定开销，仍计入最终请求预算。索引按原顺序显示、说明展示/省略数量，完整记录留在 messages.jsonl。摘要、当前输入和完整工具协议保护沿用原规则；不删原历史、不放宽最终预算，也不改变 checkpoint schema_version，旧 budget_config 没有新字段仍可恢复。

ConsoleBroker 的 poll 与 read 共用行队列、UTF-8 增量解码器及尾部缓冲，minimal 与带事件唤醒的交互均保留分片输入。无事件时 read 等待完整行或 EOF；有事件时可返回控制权处理唤醒。

## 根目录、身份与持久记录

canonical workspace 保存 sessions、tasks、memory、background、events、scheduler、teams 和 worktrees 元数据。普通会话 effective root 是 workspace；有 assignment 时由宿主解析 claim 和 Worktree 绑定，effective root/cwd 切换到任务 checkout。文件工具、Skills、Shell 与 MCP 的 cwd 使用此有效根。

TaskService 即使关闭模型侧任务工具也保留目录协调检查。owner session、agent、claim_id 和 assignment generation 由宿主生成；DirectoryLease 防止同一应用内两个执行者同时占用同一有效根。它与文件工具路径检查都不是操作系统沙箱。Shell 和本地 MCP 程序仍以当前用户权限执行。

会话 JSONL 保存消息与事件，关键意图先落盘再执行。结果已存在就保留；有意图无结果的恢复标记 execution_unknown，无意图的悬空调用补 cancelled；都不会自动重放。持久格式包含版本，未知版本或中段损坏拒绝恢复；末尾未完成行可备份后修复。

文件工具与技能正文读取共用 open_regular_read，打开前检查普通文件，并通过非阻塞打开和 fstat 复核拒绝 FIFO 等特殊文件；避免类型检查后的替换导致等待写入端。符号链接仍先由路径策略解析和授权。

Shell 的 _stop_owned 将回收异常或主进程仍存活转成 StorageError，并设置 unconfirmed_work。统一 _cleanup 在回收失败后仍尝试关闭 stdout/stderr 和 selector，只有本次清理成功才移除进程追踪；close 尝试全部追踪进程后再汇总失败。关键资源异常绕过普通工具拒绝分支，中止同批后续操作。TaskService.context 还检查仍被追踪的前台进程，防止仅凭标记遗漏放行 complete_task。Runtime 继续保留不安全的任务认领；迟到收尾成功不自动解除已有不确定标记。后台 Shell 复用同一清理入口并沿用后台未确认资源保护。

Runtime.run 从安装取消令牌开始，将服务启动、任务校验、技能刷新和子任务计数初始化纳入与 AgentLoop 相同的 try/finally。入口 Cancelled/KeyboardInterrupt 返回 CANCELLED 并停止本 Runtime 所属工作；其他准备错误保留原异常，也走资源收尾。预取消在接受历史或启动服务前检查，刷新后进入 Loop 前再复核。idle_boundary 即使失败，嵌套 finally 也清空两个 cancellation 字段。只有确认清理安全才释放当前认领；已有持久队友不因 lead 单轮取消停止，子 Agent 不能释放借用的父认领。

## 辅助模型与角色能力

Todo 是当前会话轻量状态，持久任务是跨会话依赖和认领，一次性 task 是同步委派，三者独立。Skills 先公开元信息目录，再按需读取正文。记忆使用小规模 JSON 原子存储与锁，带来源证据；选择、提取和整理通过有界无工具模型请求完成。记忆可能过期，不能授予权限。分页工具只是读取通道：记忆采纳 read_tool_output 证据前，校验产物完整性、当前会话/Agent 归属、原始 read_file 调用及已提交的成功且非 execution_unknown 结果。MCP、Shell、子会话或来源不完整的产物可继续按原权限查看，但不能经分页升级为本会话的项目/参考记忆证据；同一会话恢复后的合法文件产物仍可使用。

complete_task 在清理执行资源前先校验目标团队、执行者与 assignment，TaskStore 在锁内再次应用同一授权规则。completed 的幂等分支也必须验证原完成者的 agent_id/session_id，以及仍存在时的原 claim；不能成为跨团队读取详情的入口。合法重复仅返回已记录结果，不修改任务或再次触发资源清理。

analyze 子 Agent 只读；edit 仍受宿主权限限制，不能递归派生。队友线程只有合法 assignment 后才能操作项目文件，非交互 Shell ask 直接拒绝；require_plan 的版本化批准只是工作协调，不是命令批准。计划、消息、关闭均保存真实 request ID 和来源。收到关闭请求后先停止模型和所属进程、配对结果、确认回收，再释放 claim。新队友就绪后等待主线程复核取消并确认启动，确认前不运行模型。启动取消/超时/异常只停止本次新队友；初始认领使用独立启动取消令牌，已取得的认领在安全收尾后释放并阻止自动重试。启动失败等待收尾最多 3 秒，若未确认退出或存储/资源收尾失败，则主会话锁定新工作并保留认领，不把线程存活当成成功取消。

恢复后显式 spawn_teammate(task_id=...) 可迁移同一 lead 会话/Agent 创建的旧团队 pending、无人认领且 ready 的任务。旧团队成员须全部终止或 interrupted，成员会话来源和生命周期锁须可核验，旧团队不能留有任何认领。先持有旧会话锁，再按任务图锁 → 旧团队状态锁顺序复核并保持授权，归属更新与新 claim 通过同一次任务文件写入提交；Git 验证在任务锁外进行。自动扫描和普通 claim_task 不授予迁移权限，Worktree/依赖/创建来源保留，新计划状态不沿用旧批准；子会话保存 task_team_adopted 事件。

review_plan 的外部请求查找允许当前团队未引用的 ID 不存在，并返回 TaskError；它不会跨团队搜索并恢复旧批准。若 ID 仍被成员的 latest_plan 或 shutdown_request 引用，则保持严格读取，缺失也是存储故障。TeamStore.request 默认严格，只有审批外部 ID 时按上述条件允许缺失；已有文件的 JSON、基础身份/状态结构及读取失败保持 StorageError，不捕获全部存储异常降级为普通错误。

## 事件与服务

后台 Shell 的初始 ToolResult 仅代表成功启动，最终退出通过独立持久事件通知。worker 只捕获受限输出和发唤醒信号，所属会话线程在安全边界提交结果。Cron 将 UTC 槽与明确 IANA 时区匹配，持久 occurrence 不等同于模型已接受；按至少一次投递处理，并以事件 ID 区分重复。CLI 退出后不继续调度。

事件可唤醒空闲主会话，ConsoleBroker 主线程独占输入，保留未提交文本。自动轮不是新的人工授权，使用工具白名单和非交互上下文；失败、取消或恢复的未接受事件 held，须显式重试。恢复的是数据，线程、旧进程和队友不会从原位置续跑。

外部 /events retry 先检查命令参数及事件 ID，再允许查找不存在的 ID 并返回普通 ValueError。EventStore.get 默认仍严格读取，仅显式重试查找允许初次检查为不存在；权限错误、已有记录无效和检查后删除都保持 QueueError。事件归属、来源机制启用状态与 held/撤销状态沿用既有检查，不降级关键存储故障。

RuntimeEvents.retry 必须确认 _source(..., 'retry') 返回允许，才能将事件从 held 改为 pending 并唤醒投递。来源拒绝时返回普通 ValueError，事件、绑定和旧协议记录不改动。团队事件只信任当前 TeamManager 的来源校验；旧团队、团队未启用或身份不匹配均明确拒绝，不绕过校验导入历史控制权限。恢复提示说明 held 中只有当前可投递来源支持重试，当前团队/Cron/后台的既有投递规则不变。

## 宿主 Git 的执行边界

WorktreeService 的内部 Git 入口使用按次配置覆盖禁用仓库 Hook、fsmonitor 与自动维护。先检查有效 clean/smudge/process 过滤器配置，发现非空外部命令时拒绝自动 Git 操作，不删改仓库配置，不让过滤器成为 Hook 的替代执行路径。需要回调的项目可由用户在前台审批 Shell 后执行，Shell 本身的授权规则保持不变。按次禁用 Hook 的方式参见 [Git 官方配置说明](https://git-scm.com/docs/git-config#Documentation/git-config.txt-corehooksPath)。

Worktree 的 GitVerificationUnavailable 表示宿主策略拒绝、命令无法启动、超时、失败退出或输出无法解析，不能据此认定绑定损坏。恢复检查只对已完整提交且日志/任务均 ready 的绑定保留原记录并返回警告，Runtime 展示当前校验不可用；认领和每次执行仍重新 validate，不因历史 ready 放行。成功观察到路径/注册/分支或绑定不匹配时，继续按原规则降级；preparing/partial/failed 不自动提升，未完成创建事务不能借此恢复为 ready。

## MCP

显式宿主配置 → MCPManager → stdio 连接/完整分页发现 → 版本化 ToolRegistry → 请求快照 → 同一 ToolExecutor → 官方 SDK tools/call → 结果/产物。

锁定 mcp 1.30.0，实际要求协商 2025-11-25。每个会话/Server/assignment 使用独立进程，共享 Runtime 树的 8 个连接槽和惰性 I/O loop。I/O 线程不调用模型、不读控制台、不写历史。目录通知只标记 dirty；安全边界原子更新目录，执行前重新核对 generation/schema/cwd，单次批准不能跨绑定沿用。

授权来自本次配置的原始工具名、policy、roles 和 effect，Server 描述及注解不能授权。Schema 定义、输入与结构化输出使用标准 JSON Schema，在独立 Python 隔离子进程中校验：单次包含启动在内限 2 秒、子进程地址空间限 256 MiB；超时、取消、Ctrl+C 或异常均终止并回收校验进程。输入校验失败不发送 tools/call，输出校验失败保留已返回事实且不重跑。每次校验包含进程启动开销，不通过删除 Schema 约束降级。输出保留外部来源、isError 与已发送事实。调用超时/取消/断连不重试；未确认副作用保留 claim。关闭退出 SDK 会话、回收所属进程组，再停止 I/O loop。首期只支持本地 stdio tools，不支持远程传输和资源下载等扩展。

目录 refresh 创建一次单调时钟 deadline，贯穿分页、schema_validator 的本地遍历和输入/输出定义检查，以及注册发布。校验工作进程等待取发现剩余时间与单次 2 秒期限的较小值，返回成功后也复核 deadline。DiscoveryTimeout 和取消直接中止整次发现，不混入单工具 excluded；只有发现预算仍充足的单次 Schema 失败才沿用排除行为。发布中途失败会撤销已注册的新项，刷新失败也撤下旧目录。后续调用的 SchemaValidator 不保存发现 deadline。期限耗尽后须完成必要终止与回收，实际返回还包含清理耗时。

MCP 连接占槽并启动后，confirmed 保持 False；只有进程组退出、读写/stderr 任务结束、内存流关闭且 actor 收尾完成，才确认停止并释放容量。SDK 上下文仍在原 actor 中进出；transport 的进程/I/O 清理由独立 shield 任务完成，actor 的重复取消不能打断它。关闭等待维持 4 秒正常等待、取消 actor 后再等 3 秒、同步入口最多等待 8 秒；未完成或回收异常保持未确认、占槽和任务认领。_reap 只返回进程组事实，不提前确认整条连接；清理错误也会尝试关闭剩余传输资源。未确认后的 Runtime 不自动恢复新工作。

## s15 机制对照

| 机制 | 产品实现 | 稳定验证 |
|---|---|---|
| Loop / 分发 / 权限 / Hooks | core、tools、permissions、hooks | M1；M4 快照与 MCP 审批 |
| Todo / 子 Agent / Skills / Prompt | tasks.todo、agents、context.skills / prompt | M2；M4 开关与真实子 Agent |
| 压缩 / 错误恢复 / 产物 | context.manager、core.recovery、storage.artifacts | M1/M2；M4 MCP 大输出 |
| Memory | context.memory、storage.memory_store | M2；M4 真实跨会话召回 |
| 持久任务 | tasks.service / models、storage.task_store | M3 原子认领、依赖与恢复 |
| Background / Cron | background、scheduler、runtime_events | M3 后台与假时钟、接受修复 |
| Teams / Protocols | teams、storage.event_store | M3 计划修订、消息与关闭；M4 MCP gate |
| Worktree | worktrees.service、目录租用 | M3 部分创建与目录切换；M4 真实双队友 |
| MCP 动态池 | mcp 模块、registry 快照 | M4 假传输及真实 stdio |

相对教学版本，本产品拆分模块与强类型消息契约；记忆采用 JSON 原子存储；压缩保留原会话日志；队友默认没有交互 Shell 批准；自动认领须显式开启；Worktree 不自动同步或合并；MCP 从进程内演示补成声明范围内的真实协议接入。没有新增 s16 Workflow、s17 Goal Loop 或 Web 界面。

参考来源与许可见 [THIRD_PARTY_NOTICES](../THIRD_PARTY_NOTICES.md)。新代码许可证尚未决定，第三方 MIT 许可不代表新代码已选择 MIT。
