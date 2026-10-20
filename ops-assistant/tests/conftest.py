"""pytest 共享夹具：临时目录、演示数据库、知识库索引与运行时。

设计原则（工程规格第 6 节）：

1. **不写仓库生成物**：数据库、索引、导出、日志都落在 pytest 的临时目录里，
   跑完即丢弃，不会污染 ``ops-assistant/data/``。
2. **不 mock 数据库与计算**：真实建库、真实写数据、真实建索引，
   被验证的就是生产路径；唯一允许 fake 的是模型层（它依赖外部网络服务）。
3. **session 级数据库**：种子数据生成有固定耗时，同一测试会话共享一份
   ``ops.db``；需要独立库的测试（如建库可重复性）自己用函数级临时目录。

诚实声明：
    本文件在编写环境中**没有执行过**（该环境无法启动 Python 进程，
    ``pwsh`` 返回 0xC0000142）。所有断言都按源码静态推导。学习者机器上
    首次运行若失败，排查顺序见 ``docs/verification.md``。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

# 测试直接 import 源码包：pyproject 已声明 pythonpath = ["src"]，
# 这里再兜底一次，避免学习者用 `pytest tests/` 而没走项目配置时导入失败。
PROJECT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_DIR / "src"
SCRIPTS_DIR = PROJECT_DIR / "scripts"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ops_assistant.config import (  # noqa: E402  # 路径注入后再导入
    DEFAULT_EMBEDDING_DIM,
    Settings,
    ensure_dirs,
)
from ops_assistant.data.db import connect  # noqa: E402
from ops_assistant.data.generate import SEED, generate_database  # noqa: E402
from ops_assistant.knowledge.retrieve import KnowledgeBase  # noqa: E402

KNOWLEDGE_DIR = PROJECT_DIR / "data" / "knowledge"


# ---------------------------------------------------------------------------
# 设置构造
# ---------------------------------------------------------------------------


def build_settings(base_dir: Path) -> Settings:
    """在指定目录下构造一份"纯规则模式"的 Settings。

    参数：
        base_dir: 临时工作目录（数据库、索引、导出、日志都写在这里）。

    返回：
        Settings: ``llm_enabled=False``，两处 api_key 均为 None，
        因此测试永远不会访问真实模型服务。

    说明：
        知识文档目录指向仓库里的 ``ops-assistant/data/knowledge``（只读使用），
        索引文件写到临时目录，避免测试改动仓库中的索引。
    """
    data_dir = base_dir / "data"
    return Settings(
        base_dir=base_dir,
        data_dir=data_dir,
        db_path=data_dir / "ops.db",
        checkpoint_db_path=data_dir / "checkpoints.db",
        preference_db_path=data_dir / "preferences.db",
        knowledge_dir=KNOWLEDGE_DIR,
        index_path=data_dir / "knowledge_index.json",
        export_dir=data_dir / "exports",
        log_dir=base_dir / "logs",
        model=None,
        model_provider=None,
        api_key=None,
        base_url=None,
        embedding_model=None,
        embedding_base_url=None,
        embedding_api_key=None,
        embedding_dim=DEFAULT_EMBEDDING_DIM,
        llm_enabled=False,
        max_steps=12,
        max_tool_calls=8,
        request_timeout_s=60.0,
        log_level="WARNING",
        app_env="test",
    )


# ---------------------------------------------------------------------------
# session 级夹具：一份设置、一份演示库、一个连接、一个知识库
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def project_dir() -> Path:
    """仓库中 ``ops-assistant/`` 的绝对路径。"""
    return PROJECT_DIR


@pytest.fixture(scope="session")
def settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    """session 级设置：数据落在临时目录，模型关闭。"""
    base = tmp_path_factory.mktemp("ops-assistant")
    current = build_settings(base)
    ensure_dirs(current)
    return current


@pytest.fixture(scope="session")
def db_summary(settings: Settings) -> dict[str, Any]:
    """生成演示数据库，返回生成器给出的摘要（含各表行数与边界事实）。"""
    return generate_database(settings.db_path)


@pytest.fixture()
def conn(settings: Settings, db_summary: dict[str, Any]) -> Iterator[Any]:
    """只读用途的数据库连接（每个测试用例一个，用完关闭）。"""
    connection = connect(settings.db_path)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture(scope="session")
def kb(settings: Settings, db_summary: dict[str, Any]) -> KnowledgeBase:
    """session 级知识库：构建一次索引，供检索测试共享。"""
    return KnowledgeBase.build(settings)


@pytest.fixture(scope="session")
def ground_truth(settings: Settings, db_summary: dict[str, Any]) -> dict[str, Any]:
    """调用独立标准答案脚本，返回解析后的 JSON。

    说明：
        标准答案脚本按设计**不 import 项目查询层**（避免循环验证），
        这里通过文件路径动态导入，保持同样的独立性；
        输出写到临时目录，不覆盖仓库里的 ``data/ground_truth.json``。
    """
    module = _load_script("compute_ground_truth")
    out_path = settings.data_dir / "ground_truth.json"
    module.write_ground_truth(settings.db_path, out_path)
    return json.loads(out_path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def runtime(settings: Settings, db_summary: dict[str, Any], kb: KnowledgeBase) -> Iterator[Any]:
    """进程内运行时（内存检查点），供工作流与 API 测试共享。

    使用 ``use_memory_checkpointer=True`` 是为了让测试不需要 SQLite 检查点文件；
    重启恢复类测试会用独立的 SQLite 检查点运行时。
    """
    from ops_assistant.workflow.persistence import AppRuntime

    instance = AppRuntime.create(settings, use_memory_checkpointer=True)
    try:
        yield instance
    finally:
        instance.close()


# ---------------------------------------------------------------------------
# 辅助工具
# ---------------------------------------------------------------------------


def _load_script(name: str) -> ModuleType:
    """按文件路径加载 ``scripts/<name>.py``，返回模块对象。

    参数：
        name: 不带 ``.py`` 的脚本名，例如 ``"compute_ground_truth"``。

    返回：
        ModuleType: 已执行过模块级代码的模块。

    异常：
        FileNotFoundError: 脚本不存在。
    """
    path = SCRIPTS_DIR / f"{name}.py"
    if not path.is_file():
        raise FileNotFoundError(f"找不到脚本：{path}")
    spec = importlib.util.spec_from_file_location(f"tests_scripts_{name}", path)
    if spec is None or spec.loader is None:  # pragma: no cover - 环境异常兜底
        raise ImportError(f"无法加载脚本：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def settings_factory(tmp_path_factory: pytest.TempPathFactory) -> Callable[[str], Settings]:
    """工厂夹具：按名字创建互相隔离的设置（相同名字共享同一临时目录）。"""

    cache: dict[str, Settings] = {}

    def factory(name: str) -> Settings:
        if name not in cache:
            base = tmp_path_factory.mktemp(f"settings-{name}")
            current = build_settings(base)
            ensure_dirs(current)
            cache[name] = current
        return cache[name]

    return factory


@pytest.fixture(scope="session")
def seed() -> int:
    """演示数据固定随机种子，供测试断言引用。"""
    return SEED
