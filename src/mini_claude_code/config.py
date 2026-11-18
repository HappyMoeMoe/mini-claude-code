"""Application .env, environment and CLI configuration; no directory creation."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .core.errors import ConfigurationError

DEFAULT_BASE_URL = "https://api.deepseek.com/anthropic"
DEFAULT_MODEL = "deepseek-flash"
# Editable source installation: never discover .env in the target workspace.
_SOURCE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = (_SOURCE_ROOT / ".env" if (_SOURCE_ROOT / "src/mini_claude_code/config.py").is_file()
                    else Path.home() / ".config/mini-claude-code/.env")


@dataclass(frozen=True)
class Config:
    workspace: Path
    model: str | None
    api_key: str | None = field(repr=False)
    base_url: str = DEFAULT_BASE_URL
    fallback_model: str | None = None
    max_output_tokens: int = 8000
    max_recovery_output_tokens: int = 16000
    context_budget_chars: int = 50_000
    memory_enabled: bool = True
    timezone: str = 'UTC'
    minimal: bool = False
    disabled_features: frozenset[str] = frozenset()

    @property
    def data_dir(self) -> Path:
        return self.workspace / ".mini-claude-code"

    @property
    def safe_base_url(self) -> str:
        """Remove userinfo, query parameters and fragments from displayed URLs."""
        parsed = urlsplit(self.base_url)
        return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", ""))

    def model_readiness_errors(self) -> list[str]:
        errors = []
        if not self.model:
            errors.append("未配置模型：请设置 MODEL_ID 或 --model。")
        if not self.api_key:
            errors.append("未配置 API Key：请在应用 .env 或环境变量中设置 DEEPSEEK_API_KEY。")
        return errors

    def require_model_ready(self) -> None:
        errors = self.model_readiness_errors()
        if errors:
            raise ConfigurationError(" ".join(errors))


def load_config(
    *,
    workspace: str | Path | None = None,
    model: str | None = None,
    base_url: str | None = None,
    environ: Mapping[str, str] | None = None,
    env_file: str | Path = DEFAULT_ENV_FILE,
    max_output_tokens: int = 8000,
    max_recovery_output_tokens: int = 16000,
    timezone: str | None = None,
) -> Config:
    from dotenv import dotenv_values

    try:
        # No shell evaluation or variable interpolation; do not mutate os.environ.
        values = dotenv_values(env_file, encoding="utf-8", interpolate=False)
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError("无法读取应用 .env 配置文件。") from exc
    env = {key: value for key, value in values.items() if value is not None}
    env.update(os.environ if environ is None else environ)
    try:
        root = (Path.cwd() if workspace is None else Path(workspace).expanduser()).resolve(strict=True)
        if not root.is_dir():
            raise ConfigurationError("workspace 必须是已存在的目录。")
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigurationError("workspace 无法解析为已存在的目录。") from exc

    selected_model = model if model is not None else env.get("MODEL_ID", DEFAULT_MODEL)
    if selected_model is not None:
        selected_model = selected_model.strip()
        if not selected_model or any(char.isspace() for char in selected_model):
            raise ConfigurationError("模型名称必须为非空且不含空白的字符串。")

    address = base_url if base_url is not None else env.get("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL)
    try:
        parsed = urlsplit(address)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError("Invalid URL")
        if any(char.isspace() or ord(char) < 32 for char in address):
            raise ValueError("Invalid URL")
        _ = parsed.port
    except ValueError as exc:
        # Never include the supplied URL, which may contain credentials.
        raise ConfigurationError("API 地址必须是有效的 HTTP(S) URL。") from exc

    if (type(max_output_tokens) is not int or type(max_recovery_output_tokens) is not int
            or max_output_tokens < 1 or max_recovery_output_tokens < max_output_tokens):
        raise ConfigurationError("输出预算必须为正整数，恢复上限不能小于初始值。")
    fallback = env.get("FALLBACK_MODEL_ID", "").strip() or None
    if fallback and any(char.isspace() for char in fallback):
        raise ConfigurationError("备用模型名称不能包含空白。")
    key = env.get("DEEPSEEK_API_KEY", "").strip() or None
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    zone = timezone if timezone is not None else env.get('TZ','UTC')
    try: ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        if timezone is not None: raise ConfigurationError('timezone 必须为合法 IANA 时区。') from exc
        zone = 'UTC'
    return Config(workspace=root, model=selected_model, api_key=key, base_url=address, fallback_model=fallback,
                  max_output_tokens=max_output_tokens, max_recovery_output_tokens=max_recovery_output_tokens, timezone=zone)



FEATURES = frozenset({'skills','subagents','tasks','background','scheduler'})


def configure_profile(config, *, minimal=False, disabled=(), teams=False, mcp_config=None, memory=None):
    """Host-selected profile, applied before any Runtime resources are created."""
    from dataclasses import replace
    disabled=frozenset(disabled)
    if disabled-FEATURES:raise ConfigurationError('disable 只接受 skills,subagents,tasks,background,scheduler。')
    if minimal and (teams or mcp_config is not None or memory=='on'):
        raise ConfigurationError('--minimal 与 --teams、--mcp-config 或显式 --memory on 冲突。')
    if isinstance(disabled,str) or set(disabled)-FEATURES:
        raise ConfigurationError('--disable 仅接受 skills,subagents,tasks,background,scheduler。')
    if teams and 'tasks' in disabled:raise ConfigurationError('--teams 依赖 tasks，不能同时禁用。')
    return replace(config,minimal=minimal,disabled_features=FEATURES if minimal else disabled,
                   memory_enabled=False if minimal else config.memory_enabled if memory is None else memory=='on')
