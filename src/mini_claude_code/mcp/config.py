"""Strict host-owned configuration. Server text never grants authority."""
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re

from ..core.errors import ConfigurationError


class MCPError(ValueError):
    """An ordinary connection/business failure, independent of session storage."""


def exact(data,required,optional=()):
    if type(data) is not dict or not set(required)<=data.keys() or data.keys()-set(required)-set(optional):
        raise ConfigurationError('MCP 配置缺少字段或包含未知字段。')


def no_duplicates(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ConfigurationError('MCP JSON 包含重复字段。')
        result[key]=value
    return result


@dataclass(frozen=True)
class Grant:
    policy: str = 'ask'
    roles: tuple[str,...] = ('lead',)
    effect: str = 'unknown'


@dataclass(frozen=True)
class ServerConfig:
    name: str
    command: str
    args: tuple[str,...]
    env_from: dict[str,str]
    timeouts: dict[str,float]
    tools: dict[str,Grant]

    def environment(self):
        env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8','PYTHONIOENCODING':'utf-8'}
        for target,source in self.env_from.items():
            if source not in os.environ:raise MCPError(f'MCP 环境引用未设置：{source}。')
            env[target]=os.environ[source]
        return env


@dataclass(frozen=True)
class MCPConfig:
    path: str
    fingerprint: str
    servers: dict[str,ServerConfig]


def load_mcp_config(path):
    path=Path(path)
    if not path.is_absolute():raise ConfigurationError('--mcp-config 必须为绝对路径。')
    try:
        if path.stat().st_size>1024*1024:raise ConfigurationError('MCP 配置超过 1 MiB。')
        data=json.loads(path.read_text(encoding='utf-8'),object_pairs_hook=no_duplicates,
            parse_constant=lambda value:(_ for _ in ()).throw(ConfigurationError('配置不接受非有限数字。')))
        exact(data,{'schema_version','servers'})
        if type(data['schema_version']) is not int or data['schema_version']!=1:raise ConfigurationError('不支持 MCP 配置版本。')
        if type(data['servers']) is not dict:raise ConfigurationError('servers 必须为对象。')
        servers={}
        for name,row in data['servers'].items():
            if re.fullmatch(r'[A-Za-z0-9_-]{1,24}',name) is None:raise ConfigurationError('MCP Server 名必须为 1–24 位安全字符。')
            exact(row,{'command'},{'args','env_from','timeout_seconds','tools'})
            command=row['command']
            if type(command) is not str or not Path(command).is_absolute() or not Path(command).is_file() or not os.access(command,os.X_OK):
                raise ConfigurationError('MCP command 必须为已有可执行文件的绝对路径。')
            if Path(command).name in {'npx','uvx'}:raise ConfigurationError('本期不支持 npx/uvx 包安装启动器。')
            args=row.get('args',[])
            if type(args) is not list or len(args)>64 or any(type(a) is not str or '\0' in a or len(a)>8000 for a in args):
                raise ConfigurationError('MCP args 必须为最多 64 项的有限字符串数组。')
            env=row.get('env_from',{})
            if type(env) is not dict or any(type(v) is not str or re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',k) is None or re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',v) is None for k,v in env.items()):
                raise ConfigurationError('env_from 必须为环境变量名称映射。')
            limits=row.get('timeout_seconds',{})
            exact(limits,set(),{'connect','list','call'})
            timeouts={'connect':15,'list':15,'call':60,**limits}
            if any(type(v) not in {int,float} or not math.isfinite(v) or not 0<v<=120 for v in timeouts.values()):
                raise ConfigurationError('MCP 超时必须大于 0 且至多 120 秒。')
            tools=row.get('tools',{})
            if type(tools) is not dict:raise ConfigurationError('MCP tools 必须按远端原名配置。')
            grants={}
            for raw,g in tools.items():
                if not raw or len(raw)>1000:raise ConfigurationError('远端工具原名无效。')
                exact(g,set(),{'policy','roles','effect'})
                grant=Grant(g.get('policy','ask'),tuple(g.get('roles',['lead'])),g.get('effect','unknown'))
                if grant.policy not in {'allow','ask','deny'} or grant.effect not in {'read','write','unknown'}:
                    raise ConfigurationError('MCP policy/effect 无效。')
                if type(g.get('roles',[])) is not list or not grant.roles or len(set(grant.roles))!=len(grant.roles) or not set(grant.roles)<={'lead','subagent','teammate'}:
                    raise ConfigurationError('MCP roles 必须为非重复角色数组。')
                grants[raw]=grant
            servers[name]=ServerConfig(name,str(Path(command).resolve()),tuple(args),dict(env),timeouts,grants)
        fingerprint=hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()
        return MCPConfig(str(path.resolve()),fingerprint,servers)
    except ConfigurationError:raise
    except (OSError,ValueError,TypeError,RecursionError) as exc:raise ConfigurationError('MCP 配置不可读取或结构无效。') from exc
