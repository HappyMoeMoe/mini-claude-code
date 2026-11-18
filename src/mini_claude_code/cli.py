"""Configuration checks, session management and the interactive coding CLI."""

import argparse
import json
import sys

from . import __version__
from .config import load_config, configure_profile
from .core.errors import ConfigurationError, ContractError, ModelError, StorageError
from .storage.memory_store import MemoryError
from .tasks.models import TaskError


def positive_integer(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须为正整数") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("必须为正整数")
    return number


def build_parser():
    parser = argparse.ArgumentParser(prog="mini-claude-code", description="DeepSeek 编程助手：流式聊天、单次审批和会话恢复。")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--workspace", help="已有项目目录（默认当前目录）")
    parser.add_argument("--model", help="覆盖 MODEL_ID，默认 deepseek-flash")
    parser.add_argument("--base-url", help="覆盖 DEEPSEEK_BASE_URL")
    parser.add_argument("--max-rounds", type=positive_integer, default=30)
    parser.add_argument("--max-output-tokens", type=positive_integer, default=8000)
    parser.add_argument("--max-recovery-output-tokens", type=positive_integer, default=16000)
    parser.add_argument('--memory', choices=('on','off'), default=None, help='workspace 记忆开关，默认 on')
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("check", help="只做本地配置检查")
    chat_parser = commands.add_parser("chat", help="开始或恢复保存的会话")
    chat_parser.add_argument("--resume", metavar="SESSION_ID")
    chat_parser.add_argument('--minimal',action='store_true',help='仅基础编程、权限、保存恢复与上下文管理')
    chat_parser.add_argument('--disable',default='',help='逗号分隔：skills,subagents,tasks,background,scheduler')
    chat_parser.add_argument("--no-stream", action="store_true")
    chat_parser.add_argument("--mcp-config", help="显式加载本次本地 MCP 配置的绝对路径；不会自动启动连接")
    chat_parser.add_argument("--teams", action="store_true", help="显式允许本次 CLI 创建至多 3 个持久队友")
    chat_parser.add_argument("--context-budget-chars", type=positive_integer, default=50_000)
    chat_parser.add_argument('--memory', choices=('on','off'), default=argparse.SUPPRESS)
    chat_parser.add_argument('--timezone', help='Cron 默认 IANA 时区；再取合法 TZ，最后 UTC')
    memory = commands.add_parser('memory', help='查看、删除或整理 workspace 记忆')
    memory.add_argument('--memory', choices=('on','off'), default=argparse.SUPPRESS)
    management = memory.add_subparsers(dest='memory_command', required=True)
    for name in ('list','show','delete','consolidate'):
        command = management.add_parser(name)
        command.add_argument('--memory', choices=('on','off'), default=argparse.SUPPRESS)
        if name in {'show','delete'}: command.add_argument('memory_id')
    sessions = commands.add_parser("sessions", help="查询已保存的会话")
    listing = sessions.add_subparsers(dest="sessions_command", required=True).add_parser("list")
    listing.add_argument("--all", action="store_true", help="包括子会话及父关联")
    tasks = commands.add_parser('tasks', help='查询持久任务，或检查后释放孤立认领')
    task_commands = tasks.add_subparsers(dest='tasks_command', required=True)
    task_commands.add_parser('list')
    for name in ('show','release'):
        task_commands.add_parser(name).add_argument('task_id')
    schedule = commands.add_parser('schedule',help='查询或取消 workspace 的持久 Cron，不调用模型')
    schedules = schedule.add_subparsers(dest='schedule_command',required=True)
    schedules.add_parser('list')
    schedules.add_parser('cancel').add_argument('job_id')
    worktrees = commands.add_parser('worktrees',help='列出或安全移除任务 checkout，不调用模型')
    operations = worktrees.add_subparsers(dest='worktrees_command',required=True)
    operations.add_parser('list')
    operations.add_parser('remove').add_argument('name')
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    try:
        config = load_config(workspace=args.workspace, model=args.model, base_url=args.base_url,
                             max_output_tokens=args.max_output_tokens,
                             max_recovery_output_tokens=args.max_recovery_output_tokens)
        if args.command == 'chat' and args.timezone is not None:
            from .scheduler.cron import timezone_name
            try: zone = timezone_name(args.timezone)
            except ValueError as exc: raise ConfigurationError(str(exc)) from exc
            from dataclasses import replace
            config = replace(config,timezone=zone)
        from dataclasses import replace
        config = replace(config, memory_enabled=args.memory!='off')
        if args.command == 'memory':
            return manage_memory(config, args)
        if args.command == 'tasks':
            return manage_tasks(config, args)
        if args.command == 'worktrees':
            from .worktrees.service import WorktreeService
            service=WorktreeService(config.workspace)
            rows=[service.remove(args.name)] if args.worktrees_command=='remove' else service.records()
            for row in rows:print(json.dumps(row,ensure_ascii=False))
            return 0
        if args.command == 'schedule':
            return manage_schedule(config,args)
        if args.command == "chat":
            from dataclasses import replace
            disabled=args.disable.split(',') if args.disable else []
            config=configure_profile(config,minimal=args.minimal,disabled=disabled,teams=args.teams,
                                     mcp_config=args.mcp_config,memory=args.memory)
            config = replace(config, context_budget_chars=args.context_budget_chars)
            return chat(config, args.max_rounds, resume=args.resume, stream=not args.no_stream, teams=args.teams, mcp_config=args.mcp_config)
        if args.command == "sessions":
            from .storage.session_store import list_sessions
            for summary in list_sessions(config.workspace, include_children=args.all):
                parent = f" parent={summary['parent_session_id']} call={summary['parent_call_id']}" if summary.get('parent_session_id') else ''
                if summary.get('team_id'):parent+=f" team={summary['team_id']} lead={summary['lead_session_id']}"
                print(f"{summary['session_id']}  {summary['identity']['role']}  {summary['status']}  {summary['message_count']} 条消息  {summary['model']}{parent}")
            return 0
        print(f"workspace: {config.workspace}")
        print(f"model: {config.model}")
        print(f"DEEPSEEK_API_KEY: {'已设置' if config.api_key else '未设置'}")
        print(f"API 地址: {config.safe_base_url}")
        print(f"数据目录: {config.data_dir}")
        errors = config.model_readiness_errors()
        for error in errors:
            print(error, file=sys.stderr)
        if errors:
            return 2
        print("本地配置检查通过；未验证网络、凭证有效性或模型能力。")
        return 0
    except ConfigurationError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except (StorageError, ContractError, MemoryError, ModelError, TaskError) as exc:
        print(f"无法继续：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def chat(config, max_rounds=30, *, resume=None, stream=True, teams=False, mcp_config=None):
    from .console import ConsoleBroker
    from .core.loop import TurnOutcome
    from .runtime import Runtime

    config.require_model_ready()
    console = ConsoleBroker()
    try:
        with Runtime(config, interactive=console.interactive, approve=console.approve,
                     display=console.display, max_rounds=max_rounds, resume=resume, stream=stream, teams=teams, mcp_config=mcp_config) as runtime:
            runtime.mcp_approve=console.approve_mcp
            print(f"workspace: {config.workspace}\nmodel: {config.model}\nsession: {runtime.state.session_id}")
            print("会话已保存；/exit 或 EOF 退出；执行中 Ctrl+C 取消本轮，空闲时 Ctrl+C 退出。")
            print('团队：'+('已启用；队友非交互，Shell 不会自动批准。/team 查看，/team stop NAME 请求停止。' if teams else '未启用；chat --teams 显式开启。'))
            print('Cron 已关闭。' if 'scheduler' in config.disabled_features else f'Cron 默认时区：{config.timezone}；退出 CLI 后不继续计时。')
            print(f"记忆：{'on（正常回合后自动提取）' if config.memory_enabled else 'off'}；"
                  f"目录：{config.data_dir / 'memory'}；/memory 查看管理方式。")
            while True:
                try:
                    prompt = console.poll()
                    if prompt is None and runtime.events is not None:
                        if runtime.events.pending():
                            try:runtime.events.dispatch()
                            except (ModelError,ContractError) as exc:
                                console.display('notice',f'自动事件处理失败，未接受事件已 held：{exc}')
                            continue
                        prompt = console.read(wakeup=runtime.events.wakeup)
                    elif prompt is None:
                        prompt = console.read()
                except EOFError:
                    return 0
                if prompt is None:continue
                if prompt.strip() == "/exit":
                    return 0
                if not prompt.strip():
                    continue
                if prompt.strip()=='/mcp' or prompt.strip().startswith('/mcp '):
                    if runtime.mcp is None:console.display('notice','本次未显式加载 --mcp-config，MCP 未启用。')
                    else:
                        from .mcp.config import MCPError
                        try:
                            runtime.tasks.context()
                            parts=prompt.strip().split()
                            if len(parts)==1:value=runtime.mcp.status()
                            elif len(parts)==3 and parts[1] in {'connect','refresh','disconnect'}:
                                action=getattr(runtime.mcp,parts[1]);value=action(parts[2])
                            else:raise MCPError('用法：/mcp [connect|refresh|disconnect NAME]')
                            console.display('notice',json.dumps(value,ensure_ascii=False,indent=2))
                        except (MCPError,PermissionError) as exc:console.display('notice',str(exc))
                        except KeyboardInterrupt:console.display('notice','MCP 管理操作已取消；未重放调用。')
                    continue
                if prompt.strip()=='/team'  or prompt.strip().startswith('/team stop '):
                    if runtime.team is None:console.display('notice','本会话未启用 --teams。')
                    else:
                        try:
                            if prompt.strip()=='/team':console.display('notice',json.dumps(runtime.team.store.read(),ensure_ascii=False,indent=2))
                            else:
                                result=runtime.team.request_shutdown({'teammate':prompt.strip()[len('/team stop '):]},runtime.context)
                                console.display('notice',result.output)
                        except (TaskError,ValueError) as exc:console.display('notice',str(exc))
                    continue
                if prompt.strip() == '/background' or prompt.strip().startswith('/background cancel '):
                    if runtime.events is None or 'background' in config.disabled_features:
                        console.display('notice','后台功能在本次配置中关闭。');continue
                    try:
                        runtime.events.boundary()
                        if prompt.strip() == '/background':
                            console.display('notice',json.dumps(runtime.events.background.records(),ensure_ascii=False,indent=2))
                        else:
                            value=runtime.events.background.cancel(prompt.strip().split()[-1])
                            console.display('notice',value)
                    except ValueError as exc:console.display('notice',str(exc))
                    continue
                if prompt.strip().split(maxsplit=1)[0] == '/events':
                    if runtime.events is None:
                        console.display('notice','本次没有事件服务。');continue
                    try:
                        parts=prompt.strip().split()
                        if len(parts)==1:
                            rows=runtime.events.store.list(runtime.state.session_id)
                            console.display('notice','\n'.join(f'{r["event"]["event_id"]}  {r["event"]["kind"]}  {r["status"]}  source={r["event"].get("source_id")}'
                                for r in rows) or '本会话没有通知事件。')
                        else:
                            if len(parts)!=3 or parts[1]!='retry':raise ValueError('用法：/events 或 /events retry ID。')
                            runtime.events.retry(parts[2]);console.display('notice','事件已交回非交互投递；不会重启原命令。')
                    except ValueError as exc:console.display('notice',str(exc))
                    continue
                if prompt.strip() == '/memory':
                    message = '记忆已关闭；不自动读取、召回、提取或整理。'
                    if config.memory_enabled:
                        try:
                            records = runtime.memory.store.snapshot()['records']
                            message = f'记忆已开启；共 {len(records)} 条，active {sum(r["status"]=="active" for r in records)} 条。'
                        except MemoryError:
                            message = '记忆库不可用，原文件保留。'
                    console.display('notice', message + ' CLI 管理：memory list / show ID / delete ID / consolidate；chat --memory off 关闭。')
                    continue
                if prompt.strip() == '/compact' or prompt.strip().startswith('/compact '):
                    focus = prompt.strip()[len('/compact'):].strip()
                    try:
                        view = runtime.compact(focus)
                        console.display('notice', f'压缩结束：视图 {view.chars} 字符，辅助摘要 {view.auxiliary_calls} 次；等待新的输入。')
                    except (ModelError, ContractError) as exc:
                        console.display('notice', f'压缩失败，原始历史和上一检查点保留：{exc}')
                    except KeyboardInterrupt:
                        runtime.client.cancel_active()
                        console.display('notice', '已取消压缩，原始历史保留。')
                    continue
                if prompt.strip() == "/todos":
                    if config.minimal:
                        console.display("notice","minimal 中 Todo 已关闭。");continue
                    items = runtime.todos.current()
                    console.display("notice", "\n".join(f"[{item.status}] {item.content}" for item in items) or "当前 Todo 为空。")
                    continue
                if prompt.strip() == "/skills":
                    if "skills" in config.disabled_features:
                        console.display("notice","Skills 已关闭。");continue
                    catalog = runtime.skills.refresh()
                    console.display("notice", "\n".join(f"{item.name}: {item.description}" for item in catalog.entries) or "未发现可用技能。")
                    for warning in catalog.warnings:
                        console.display("notice", warning)
                    continue
                try:
                    outcome = runtime.run(prompt)
                    if outcome == TurnOutcome.CANCELLED:
                        console.display("notice", "本轮已取消；已保留真实结果，可继续输入。")
                        if runtime.team is not None:
                            active=[m['name'] for m in runtime.team.store.read()['members'].values() if m['state'] not in {'STOPPED','FAILED','interrupted'}]
                            if active:console.display('notice','持久队友仍活动：'+', '.join(active)+'；用 /team stop NAME 停止。')
                    elif outcome == TurnOutcome.LIMIT_REACHED:
                        console.display("notice", "达到本轮模型请求上限；调用结果已配对，可继续输入。")
                except (ModelError, ContractError) as exc:
                    console.display("notice", f"本轮失败：{exc}；已有修改保留，可继续输入或恢复会话。")
    except KeyboardInterrupt:
        console.display("notice", "已退出并清理本次会话资源。")
        return 130


def manage_memory(config, args):
    from .storage.memory_store import MemoryStore
    store = MemoryStore(config.workspace)
    if args.memory_command == 'consolidate':
        if not config.memory_enabled:
            raise ConfigurationError('--memory off 时不能整理记忆。')
        config.require_model_ready()
        from .context.memory import MemoryService
        from .core.cancellation import CancellationToken
        from .providers.deepseek import DeepSeekClient
        client = DeepSeekClient(config)
        try:
            service = MemoryService(config, client, display=lambda kind,value:print(value))
            count = service.consolidate(CancellationToken())
            print(f'记忆整理完成：{count} 条提议已提交。')
        finally:
            client.close()
        return 0
    if args.memory_command == 'delete':
        store.delete(args.memory_id)
        print('记忆已删除；同源事实已抑制。旧会话、输出及备份不受影响。')
        return 0
    records = store.snapshot()['records']
    if args.memory_command == 'list':
        for record in records:
            print(f'{record["memory_id"]}  {record["status"]}  {record["type"]}  r{record["revision"]}  {record["name"]}: {record["description"]}')
        return 0
    record = next((r for r in records if r['memory_id']==args.memory_id),None)
    if record is None: raise MemoryError('没有此记忆。')
    print(json.dumps(record,ensure_ascii=False,indent=2))
    return 0


def manage_tasks(config, args):
    from .storage.task_store import TaskStore, view
    from .tasks.service import release_orphan
    store = TaskStore(config.workspace)
    if args.tasks_command == 'release':
        def confirm(record):
            print(f'任务：{record.task_id}\n原负责人：{json.dumps(record.owner,ensure_ascii=False)}\nrevision: {record.revision}')
            print('释放不会回滚修改；需确认旧工作、子进程及外部副作用已停止。')
            try:
                return input('确认已检查并释放本次认领？仅输入 y 确认 [y/N]: ').strip() == 'y'
            except EOFError:
                return False
        record = release_orphan(store, args.task_id, interactive=sys.stdin.isatty(), confirm=confirm)
        print(f'{record.task_id} 已释放为 pending；未自动认领或继续执行。')
        return 0
    graph = store.snapshot()
    if args.tasks_command == 'show':
        record = store._get(graph, args.task_id)
        print(json.dumps(view(record,graph),ensure_ascii=False,indent=2))
        return 0
    for record in sorted(graph.values(),key=lambda r:(r.created_at,r.task_id)):
        item = view(record,graph,brief=True)
        print(json.dumps(item,ensure_ascii=False))
    return 0


def manage_schedule(config,args):
    from .storage.scheduler_store import SchedulerStore
    from .storage.event_store import EventStore
    from .core.events import RuntimeEvent
    store=SchedulerStore(config.workspace)
    events=EventStore(config.workspace)
    if args.schedule_command=='cancel':
        try:job=store.cancel(args.job_id)
        except ValueError as exc:raise ConfigurationError(str(exc)) from exc
        data=store.snapshot()
        for row in data['occurrences'].values():
            if row['job_id']==args.job_id and row['status']!='accepted':
                event=RuntimeEvent.from_dict(row['event']);events.put(event,held=row['status']=='held')
                events.cancel(event.event_id,in_flight=row['attempt_id'] is not None and row['status']=='pending')
        if args.job_id in data['local_slots']:
            slot=data['local_slots'][args.job_id]
            for row in events.list():
                if row['event'].get('source_id')==args.job_id:
                    events.cancel(row['event']['event_id'],in_flight=slot['attempt_id'] is not None)
        print(json.dumps(job,ensure_ascii=False));return 0
    data=store.snapshot()
    for job in data['jobs'].values():
        pending=[{'event_id':r['event']['event_id'],'status':r['status']} for r in data['occurrences'].values()
                 if r['job_id']==job['job_id'] and r['status'] in {'pending','held'}]
        print(json.dumps({**job,'unaccepted':pending},ensure_ascii=False))
    for slot in data['local_slots'].values():
        pending=[{'event_id':r['event']['event_id'],'status':r['status']} for r in events.list()
                 if r['event'].get('source_id')==slot['job_id'] and r['status'] in {'pending','held'}]
        print(json.dumps({**slot,'local_timer_only':True,'unaccepted':pending},ensure_ascii=False))
    return 0
