"""配置模块：集中定义路径、模型开关、执行预算、日志级别与运行环境。
路径一律以 ops-assistant 项目根（本文件所在包往上两级）为基准，并允许 OPS_* 环境变量覆盖。
本模块只读取环境变量与项目根 .env，不写文件、不发起网络请求、不打印任何密钥明文。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# 目录与文件名常量：脚本、测试、文档引用同一套名字，避免多处硬编码。
DATA_DIRNAME = "data"
KNOWLEDGE_DIRNAME = "knowledge"
EXPORT_DIRNAME = "exports"
LOG_DIRNAME = "logs"
DB_FILENAME = "ops.db"
CHECKPOINT_DB_FILENAME = "checkpoints.db"
PREFERENCE_DB_FILENAME = "preferences.db"
INDEX_FILENAME = "knowledge_index.json"
ENV_FILENAME = ".env"

# 默认值（工程规格 5.1）。
DEFAULT_EMBEDDING_DIM = 512
DEFAULT_MAX_STEPS = 12
DEFAULT_MAX_TOOL_CALLS = 8
DEFAULT_REQUEST_TIMEOUT_S = 60.0
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_APP_ENV = "dev"

# 合法取值与取值范围：越界即视为配置错误。
VALID_LOG_LEVELS: tuple[str, ...] = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")
VALID_APP_ENVS: tuple[str, ...] = ("dev", "test", "prod")
_MIN_EMBEDDING_DIM = 1
_MAX_EMBEDDING_DIM = 8192
_MAX_STEPS_LIMIT = 200
_MAX_TOOL_CALLS_LIMIT = 200
_MAX_TIMEOUT_S = 3600.0

# 项目根目录（示例：<仓库根>/ops-assistant/src/ops_assistant/config.py）
#   parents[0] = ops_assistant 包目录
#   parents[1] = src
#   parents[2] = ops-assistant 项目根  ← 本模块使用
#   parents[3] = 仓库根（例如 D:\MySpace\LangChain学习路线），不是项目根
PROJECT_DIR = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    """运行期设置（不可变）。

    路径字段由 load_settings 按项目根推导；模型相关字段全部可为 None。
    api_key 与 embedding_api_key 使用 repr=False，避免整对象被打印/记录时泄露密钥。
    """

    base_dir: Path
    data_dir: Path
    db_path: Path
    checkpoint_db_path: Path
    preference_db_path: Path
    knowledge_dir: Path
    index_path: Path
    export_dir: Path
    log_dir: Path
    model: str | None
    model_provider: str | None
    api_key: str | None = field(repr=False)
    base_url: str | None
    embedding_model: str | None
    embedding_base_url: str | None
    embedding_api_key: str | None = field(repr=False)
    embedding_dim: int
    llm_enabled: bool
    max_steps: int
    max_tool_calls: int
    request_timeout_s: float
    log_level: str
    app_env: str


def _read_env(name: str) -> str | None:
    """读取一个环境变量；空字符串与纯空白视为“未设置”。

    参数：
        name: 环境变量名，例如 "OPS_MODEL"。

    返回：
        str | None：去掉首尾空白后的值，或 None。
    """
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def _read_int(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    """读取整数环境变量并做范围校验。

    参数：
        name: 环境变量名。
        default: 变量缺失时使用的默认值。
        minimum: 允许的最小值（含）。
        maximum: 允许的最大值（含）。

    返回：
        int：合法整数。

    异常：
        ValueError: 变量不是整数或超出范围；消息中不包含任何密钥类变量的值。
    """
    raw = _read_env(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"环境变量 {name} 必须是整数，当前值为 {raw!r}；"
            f"请修改 .env，或删除该变量以使用默认值 {default}。"
        ) from exc
    if value < minimum or value > maximum:
        raise ValueError(
            f"环境变量 {name} 必须在 {minimum} 到 {maximum} 之间，当前值为 {value}。"
        )
    return value


def _read_float(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    """读取浮点数环境变量并做范围校验。

    参数：
        name: 环境变量名。
        default: 变量缺失时使用的默认值。
        minimum: 允许的最小值（含）。
        maximum: 允许的最大值（含）。

    返回：
        float：合法浮点数。

    异常：
        ValueError: 变量不是数字或超出范围。
    """
    raw = _read_env(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"环境变量 {name} 必须是数字，当前值为 {raw!r}；"
            f"请修改 .env，或删除该变量以使用默认值 {default}。"
        ) from exc
    if value < minimum or value > maximum:
        raise ValueError(
            f"环境变量 {name} 必须在 {minimum} 到 {maximum} 之间，当前值为 {value}。"
        )
    return value


def _resolve_log_level(raw: str | None) -> str:
    """把日志级别归一化；非法值只告警并回退 INFO（日志配置不应阻断启动）。

    参数：
        raw: 环境变量原始值。

    返回：
        str：大写日志级别，一定是 VALID_LOG_LEVELS 之一。
    """
    if raw is None:
        return DEFAULT_LOG_LEVEL
    candidate = raw.strip().upper()
    if candidate in VALID_LOG_LEVELS:
        return candidate
    logger.warning(
        "OPS_LOG_LEVEL=%r 不是合法级别，已回退 %s；可用值：%s。",
        raw,
        DEFAULT_LOG_LEVEL,
        "、".join(VALID_LOG_LEVELS),
    )
    return DEFAULT_LOG_LEVEL


def _resolve_app_env(raw: str | None) -> str:
    """把运行环境名归一化；非法值只告警并回退 dev。

    参数：
        raw: 环境变量原始值。

    返回：
        str：dev / test / prod 之一。
    """
    if raw is None:
        return DEFAULT_APP_ENV
    candidate = raw.strip().lower()
    if candidate in VALID_APP_ENVS:
        return candidate
    logger.warning(
        "OPS_APP_ENV=%r 不是合法环境，已回退 %s；可用值：%s。",
        raw,
        DEFAULT_APP_ENV,
        "、".join(VALID_APP_ENVS),
    )
    return DEFAULT_APP_ENV


def _load_dotenv(base_dir: Path) -> None:
    """读取项目根 .env；已存在的环境变量不会被覆盖。

    参数：
        base_dir: 项目根目录；其下没有 .env 时静默返回。
    """
    env_path = Path(base_dir) / ENV_FILENAME
    if not env_path.is_file():
        return
    load_dotenv(dotenv_path=env_path, override=False)


def _warn_about_model_config(settings: Settings) -> None:
    """输出模型配置告警/摘要；只提到变量名与模型名，绝不输出密钥。

    参数：
        settings: 已构造完成的设置对象。
    """
    if settings.model and not settings.api_key:
        logger.warning(
            "已设置 OPS_MODEL=%s，但缺少 OPS_API_KEY：llm_enabled=False，"
            "将以纯规则模式运行（不会调用模型）。",
            settings.model,
        )
        return
    if settings.api_key and not settings.model:
        logger.warning(
            "已设置 OPS_API_KEY，但缺少 OPS_MODEL：模型不会启用，"
            "将以纯规则模式运行。",
        )
        return
    if settings.llm_enabled:
        logger.info(
            "模型已启用：model=%s，provider=%s，base_url=%s，llm_enabled=True。",
            settings.model,
            settings.model_provider or "(未设置)",
            settings.base_url or "(官方默认)",
        )
        return
    logger.info("未配置模型与密钥：以纯规则模式运行（llm_enabled=False）。")


def load_settings(base_dir: Path | None = None) -> Settings:
    """读取设置：先加载项目根 .env（不覆盖已有变量），再合并 OPS_* 环境变量。

    参数：
        base_dir: 项目根目录。默认使用本文件推导出的 ops-assistant 项目根；
            测试可传 tmp_path，避免读写仓库内生成物。

    返回：
        Settings：不可变的设置对象。

    异常：
        ValueError: 数值变量非法、越界，或 base_dir 指向一个已存在的文件。
    """
    project_dir = Path(base_dir).expanduser().resolve() if base_dir is not None else PROJECT_DIR
    if project_dir.exists() and not project_dir.is_dir():
        raise ValueError(f"项目根必须是目录，当前路径是文件：{project_dir}")

    _load_dotenv(project_dir)

    data_dir = project_dir / DATA_DIRNAME
    knowledge_dir = data_dir / KNOWLEDGE_DIRNAME
    db_path = data_dir / DB_FILENAME
    checkpoint_db_path = data_dir / CHECKPOINT_DB_FILENAME
    preference_db_path = data_dir / PREFERENCE_DB_FILENAME
    index_path = data_dir / INDEX_FILENAME
    export_dir = data_dir / EXPORT_DIRNAME
    log_dir = project_dir / LOG_DIRNAME

    model = _read_env("OPS_MODEL")
    model_provider = _read_env("OPS_MODEL_PROVIDER")
    api_key = _read_env("OPS_API_KEY")
    base_url = _read_env("OPS_BASE_URL")

    embedding_model = _read_env("OPS_EMBEDDING_MODEL")
    # 规格 5.1：embedding 端点与密钥可复用 OPS_BASE_URL / OPS_API_KEY。
    embedding_base_url = _read_env("OPS_EMBEDDING_BASE_URL") or base_url
    embedding_api_key = _read_env("OPS_EMBEDDING_API_KEY") or api_key

    embedding_dim = _read_int(
        "OPS_EMBEDDING_DIM",
        DEFAULT_EMBEDDING_DIM,
        minimum=_MIN_EMBEDDING_DIM,
        maximum=_MAX_EMBEDDING_DIM,
    )
    max_steps = _read_int(
        "OPS_MAX_STEPS",
        DEFAULT_MAX_STEPS,
        minimum=1,
        maximum=_MAX_STEPS_LIMIT,
    )
    max_tool_calls = _read_int(
        "OPS_MAX_TOOL_CALLS",
        DEFAULT_MAX_TOOL_CALLS,
        minimum=0,
        maximum=_MAX_TOOL_CALLS_LIMIT,
    )
    request_timeout_s = _read_float(
        "OPS_REQUEST_TIMEOUT_S",
        DEFAULT_REQUEST_TIMEOUT_S,
        minimum=0.1,
        maximum=_MAX_TIMEOUT_S,
    )
    log_level = _resolve_log_level(_read_env("OPS_LOG_LEVEL"))
    app_env = _resolve_app_env(_read_env("OPS_APP_ENV"))

    settings = Settings(
        base_dir=project_dir,
        data_dir=data_dir,
        db_path=db_path,
        checkpoint_db_path=checkpoint_db_path,
        preference_db_path=preference_db_path,
        knowledge_dir=knowledge_dir,
        index_path=index_path,
        export_dir=export_dir,
        log_dir=log_dir,
        model=model,
        model_provider=model_provider,
        api_key=api_key,
        base_url=base_url,
        embedding_model=embedding_model,
        embedding_base_url=embedding_base_url,
        embedding_api_key=embedding_api_key,
        embedding_dim=embedding_dim,
        llm_enabled=bool(model and api_key),
        max_steps=max_steps,
        max_tool_calls=max_tool_calls,
        request_timeout_s=request_timeout_s,
        log_level=log_level,
        app_env=app_env,
    )

    _warn_about_model_config(settings)
    return settings


def ensure_dirs(settings: Settings) -> None:
    """创建运行所需目录（幂等，不创建也不覆盖文件）。

    创建 data、data/knowledge、data/exports、logs，以及数据库/索引文件的父目录。
    知识文档本身不由本函数生成：缺失时由 bootstrap 与知识层给出中文提示。

    参数：
        settings: 设置对象。

    异常：
        OSError: 目录创建失败（权限不足、路径被同名文件占用等）。
    """
    directories = (
        settings.base_dir,
        settings.data_dir,
        settings.knowledge_dir,
        settings.export_dir,
        settings.log_dir,
        settings.db_path.parent,
        settings.checkpoint_db_path.parent,
        settings.preference_db_path.parent,
        settings.index_path.parent,
    )
    for directory in directories:
        Path(directory).mkdir(parents=True, exist_ok=True)
