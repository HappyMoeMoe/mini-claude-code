# Mini Claude Code

Mini Claude Code 是一个模仿 Claude Code 的轻量级终端编程助手，使用 Python 构建，当前通过 DeepSeek 的 Anthropic 兼容接口调用模型。

我们希望用一个小型项目，完整实现编程 Agent 的核心能力：理解需求、读取代码、修改文件、执行命令，并在多轮交互中完成任务。项目采用模块化设计，方便阅读源码、扩展工具和探索多 Agent 协作。

## 核心功能

- **终端编程与工具执行**：通过自然语言提出需求，Agent 在多轮对话中读取代码、查找文件、修改内容并执行 Shell 命令，流式展示回答和工具执行结果。
- **会话恢复与上下文管理**：在本地保存对话和执行记录，支持退出后恢复会话；通过历史压缩、上下文预算和大输出分页，处理较长的编程任务。
- **Skills 与长期记忆**：按需加载任务相关的技能说明，提取和整理项目约定与用户偏好，并在后续会话中召回相关信息。
- **任务规划与进度管理**：用 Todo 跟踪当前计划，用持久任务管理复杂工作的依赖、认领和完成状态，为单 Agent 和团队协作提供任务基础。
- **多 Agent 协作**：主 Agent 可委派独立子任务，也可创建持久队友并行工作，支持消息通信和计划审批；结合 Git Worktree，让不同任务在独立工作目录中修改代码。
- **后台执行与定时调度**：将耗时命令放到后台运行，完成后向会话发送结果；支持按 Cron 表达式和时区调度，并统一处理后台、定时及团队通知。
- **工具扩展与 Hooks**：通过本地 stdio MCP 服务接入外部工具，统一校验参数和处理结果；通过生命周期钩子，在需求提交、工具执行和每轮结束时加入自定义检查。
- **权限控制与异常恢复**：检查文件访问范围和 Agent 角色权限，对 Shell 等操作逐次审批；支持模型请求的有限重试、当前轮取消，以及退出时的进程和线程清理。

## 快速开始

环境要求：**Linux、Python 3.11+、DeepSeek API Key**。

在仓库根目录创建虚拟环境并安装：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

编辑仓库根目录的 `.env`，填入自己的 Key：

```dotenv
DEEPSEEK_API_KEY=你的_API_Key
MODEL_ID=deepseek-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com/anthropic
```

检查配置并启动：

```bash
mini-claude-code check
mini-claude-code --workspace /path/to/your/project chat
```

`--workspace` 指定要操作的项目目录，默认使用当前目录。进入会话后，直接输入需求即可，例如：

> 阅读这个项目，找到导致测试失败的问题，修复后运行相关测试。

Shell 命令会在执行前请求确认，输入 `y` 批准本次调用。输入 `/exit` 退出；会话数据保存在目标项目的 `.mini-claude-code/` 中。建议将该目录加入目标项目的 `.gitignore`。

## 常用操作

```bash
# 仅启用基础编程能力
mini-claude-code --workspace /path/to/project chat --minimal

# 查看并恢复历史会话
mini-claude-code --workspace /path/to/project sessions list
mini-claude-code --workspace /path/to/project chat --resume <session_id>

# 启用多 Agent 团队
mini-claude-code --workspace /path/to/project chat --teams

# 加载本地 MCP 配置
mini-claude-code --workspace /path/to/project chat --mcp-config /absolute/path/to/mcp.json
```

MCP 配置样例见 [examples/mcp.example.json](examples/mcp.example.json)。

## 真实模型验证

配置好 `.env` 后，在仓库根目录运行真实 API 测试，验证流式对话、代码修复、子 Agent、记忆、上下文压缩、MCP 和团队协作：

```bash
python -m pip install -e '.[dev]'
python -m pytest -q -m real_api
```

测试会调用配置的模型 API 并消耗额度，检查实际响应、文件变更和执行结果。默认执行 `python -m pytest -q` 也包含这些真实 API 用例。完整说明见 [测试说明](tests/README.md)。
