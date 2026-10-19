"""日志工具：统一控制台/文件日志格式，并对疑似密钥字段做脱敏。
本模块不读取 .env、不依赖项目内其他模块，可被脚本、API、UI 复用。
默认输出到标准错误；传入 log_dir 时额外写入 UTF-8 追加日志文件。
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# 文件日志默认名称（相对 log_dir）。
DEFAULT_LOG_FILENAME = "ops-assistant.log"

# 日志格式：时间 级别 logger 名 - 消息。
LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s - %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 字段名命中这些子串时，其值一律替换为掩码。
_SECRET_FIELD_HINTS: tuple[str, ...] = (
    "api_key",
    "apikey",
    "access_key",
    "token",
    "secret",
    "password",
    "authorization",
    "credential",
)

# 掩码文本：不保留任何密钥片段（连末四位也不保留）。
MASK_TEXT = "***"

# 用于识别“本模块自己添加的 handler”，保证重复配置不会叠加输出。
_HANDLER_MARKER = "_ops_assistant_handler"


def mask_secret(value: str | None) -> str:
    """把密钥值转换成可安全展示的文本。

    参数：
        value: 密钥原值；None 或空字符串表示未配置。

    返回：
        str："(未设置)" 或掩码 "***"。
    """
    if value is None or not str(value).strip():
        return "(未设置)"
    return MASK_TEXT


def is_secret_field(name: str) -> bool:
    """判断字段名是否属于敏感字段。

    参数：
        name: 字段名，例如 "api_key"。

    返回：
        bool：字段名包含任一敏感子串时为 True。
    """
    lowered = name.lower()
    return any(hint in lowered for hint in _SECRET_FIELD_HINTS)


def sanitize_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """返回脱敏后的字段副本，不修改入参。

    参数：
        fields: 待记录的键值对。

    返回：
        dict[str, Any]：敏感键的值被替换为 "***"，其他键保持不变。
    """
    return {key: MASK_TEXT if is_secret_field(key) else value for key, value in fields.items()}


def get_logger(name: str) -> logging.Logger:
    """获取 logger。

    参数：
        name: logger 名称，通常传 __name__。

    返回：
        logging.Logger：标准库 logger（不在此处添加 handler）。
    """
    return logging.getLogger(name)


def _resolve_level(level: str | int) -> int:
    """把字符串或整数级别解析成 logging 常量。

    参数：
        level: 例如 "INFO"、"debug" 或 logging.WARNING。

    返回：
        int：合法日志级别；无法识别时告警并回退 INFO。
    """
    if isinstance(level, int):
        return level
    candidate = level.strip().upper()
    resolved = logging.getLevelName(candidate)
    if isinstance(resolved, int):
        return resolved
    logging.getLogger(__name__).warning(
        "未知日志级别 %r，已回退 INFO；可用值：DEBUG、INFO、WARNING、ERROR、CRITICAL。",
        level,
    )
    return logging.INFO


def setup_logging(
    level: str | int = "INFO",
    *,
    log_dir: Path | None = None,
    filename: str = DEFAULT_LOG_FILENAME,
    console: bool = True,
) -> Path | None:
    """配置根 logger，幂等：重复调用会先移除本模块添加的 handler 再安装新的。

    参数：
        level: 日志级别（字符串或 int）。
        log_dir: 提供时把日志同时写入 <log_dir>/<filename>（UTF-8，追加模式）。
        filename: 日志文件名。
        console: 是否输出到标准错误。

    返回：
        Path | None：启用的日志文件路径；未启用文件日志时返回 None。

    异常：
        OSError: 日志目录无法创建或日志文件无法打开。
    """
    resolved_level = _resolve_level(level)
    root = logging.getLogger()
    root.setLevel(resolved_level)

    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            root.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    if console:
        console_handler = logging.StreamHandler(stream=sys.stderr)
        console_handler.setFormatter(formatter)
        setattr(console_handler, _HANDLER_MARKER, True)
        root.addHandler(console_handler)

    log_path: Path | None = None
    if log_dir is not None:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        log_path = directory / filename
        file_handler = logging.FileHandler(log_path, encoding="utf-8", delay=True)
        file_handler.setFormatter(formatter)
        setattr(file_handler, _HANDLER_MARKER, True)
        root.addHandler(file_handler)

    return log_path


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """输出一行结构化事件日志，敏感字段自动脱敏。

    参数：
        logger: 目标 logger。
        event: 事件名，例如 "llm_call"、"index_build"。
        level: 日志级别，默认 INFO。
        **fields: 事件字段；字段名含 key/token/secret 等时值会被替换为 "***"。

    示例：
        log_event(logger, "llm_call", model="openai:gpt-4.1-mini", api_key=实际密钥, ok=True)
        输出形如：llm_call | model=openai:gpt-4.1-mini api_key=*** ok=True
    """
    safe_fields = sanitize_fields(fields)
    if not safe_fields:
        logger.log(level, "%s", event)
        return
    detail = " ".join(f"{key}={value}" for key, value in safe_fields.items())
    logger.log(level, "%s | %s", event, detail)
