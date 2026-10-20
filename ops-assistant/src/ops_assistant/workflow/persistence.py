"""运行时装配与持久化：SQLite 检查点、偏好存储、AppRuntime 生命周期。

本模块负责把"配置 → 连接 → 知识库 → 模型 → 检查点 → 编译好的图"装配在一起，
并对外提供三个动作：``invoke_turn`` / ``resume_turn`` / ``read_thread``。

关于 ``InMemorySaver``：
    它只用于测试与教学演示（``AppRuntime.create(use_memory_checkpointer=True)``），
    与 ``SqliteSaver`` 的差异（写在这里，避免误用于生产）：
    1) 进程退出即丢：重启后所有 thread 的检查点与 interrupt 都没有了；
    2) 无法跨进程/跨 worker 共享：多 worker 部署时各自持有不同历史；
    3) 不支持在中断后隔一段时间再恢复（进程还在内存里才行）；
    4) 没有 ``setup()`` 建表动作，也没有并发写入的文件锁语义。
"""

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command, Overwrite

from ops_assistant.config import Settings, ensure_dirs, load_settings
from ops_assistant.observability import (
    MetricsCollector,
    elapsed_ms_since,
    get_logger,
    log_structured,
    new_request_id,
)
from ops_assistant.workflow.graph import build_graph
from ops_assistant.workflow.nodes import _edited_report_or_draft
from ops_assistant.workflow.state import (
    DEFAULT_USER_ID,
    PREFERENCE_KEYS,
    AssistantContext,
    initial_state,
    jsonable,
)

logger = get_logger("workflow.persistence")

# 偏好/线程两个接口都用这张表存"用户级键值偏好"
PREFERENCES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS preferences (
    user_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, namespace, key)
)
"""

# 允许的审批结论（与 workflow.review 的 options 保持一致）
VALID_DECISIONS = ("approve", "reject", "edit")


class ThreadNotFoundError(LookupError):
    """会话不存在，或该 thread 还没有任何检查点。"""


def _project_root() -> Path:
    """返回 ops-assistant 项目根目录（本文件位于 src/ops_assistant/workflow/）。"""
    return Path(__file__).resolve().parents[3]


def _settings_path(settings: Any, *names: str, default: Path) -> Path:
    """按候选属性名依次读取配置里的路径，都取不到时用相对默认值。"""
    for name in names:
        value = getattr(settings, name, None)
        if value:
            return Path(value)
    return default


def _call_ensure_dirs(settings: Any) -> None:
    """调用配置模块的 ``ensure_dirs``（签名可能是 ensure_dirs(settings) 或 ensure_dirs()）。"""
    for args in ((settings,), ()):
        try:
            ensure_dirs(*args)
            return
        except TypeError:
            continue
        except Exception as exc:  # 建目录失败不致命，后续写文件时会给出更具体的错误
            logger.warning("ensure_dirs 失败：%s: %s", type(exc).__name__, exc)
            return


def _open_data_connection(db_path: str | Path) -> sqlite3.Connection:
    """打开业务库连接（显式允许跨线程使用）。

    说明：``data/db.py`` 的 ``connect`` 走 ``sqlite3.connect`` 默认参数，连接只能
    在创建它的线程里使用；而 API 的同步处理器跑在 Starlette 线程池中，调用线程
    与 lifespan 建连线程不同，直接复用会抛 ``ProgrammingError``。这里按
    ``data/db.py`` 的同一口径（``row_factory=sqlite3.Row``、外键 PRAGMA、30 秒
    busy timeout）重开一个跨线程连接，因此 CLI（单线程）与 API（多线程）可以共用
    同一个 ``AppRuntime`` 装配逻辑。
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _build_knowledge_base(settings: Any) -> Any:
    """构建知识库（失败返回 None，知识节点会降级为"无知识依据"）。"""
    try:
        from ops_assistant.knowledge.retrieve import KnowledgeBase
    except Exception as exc:  # 知识库模块缺失时不影响数据类流程
        logger.warning("knowledge 模块不可用：%s: %s", type(exc).__name__, exc)
        return None
    for args in ((settings,), ()):
        try:
            return KnowledgeBase.load_or_build(*args)
        except TypeError:
            continue
        except Exception as exc:
            logger.warning("知识库构建失败：%s: %s", type(exc).__name__, exc)
            return None
    return None


def _build_llm(settings: Any) -> Any:
    """构建可选模型客户端（未配置/构建失败都返回 None，纯规则流程照常跑）。"""
    try:
        from ops_assistant.agent.llm import build_llm
    except Exception as exc:
        logger.warning("llm 模块不可用：%s: %s", type(exc).__name__, exc)
        return None
    for args in ((settings,), ()):
        try:
            return build_llm(*args)
        except TypeError:
            continue
        except Exception as exc:
            logger.warning("模型客户端构建失败：%s: %s", type(exc).__name__, exc)
            return None
    return None


def _now_iso() -> str:
    """返回 UTC 的 ISO8601 时间字符串（秒精度）。"""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _unwrap_preference_value(value: Any) -> Any:
    """把存储层的 ``{"value": X}`` 包装还原成标量 ``X``。

    仅当 value 是**只含 ``value`` 一个键**且内层不是 dict/list 的 dict 时才解包；
    真正的 dict 偏好值原样返回。这样 ``all_for_user`` 的返回值既能覆盖
    ``set_many`` 写入的标量偏好，也不会误伤未来可能出现的 dict 偏好。
    """
    if (
        isinstance(value, dict)
        and len(value) == 1
        and "value" in value
        and not isinstance(value["value"], (dict, list))
    ):
        return value["value"]
    return value


# ---------------------------------------------------------------------------
# 偏好存储（教学版 Store 适配器）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreferenceItem:
    """一条偏好（模拟官方 Store 的 Item：``value`` 是 dict）。"""

    namespace: tuple[str, ...]
    key: str
    value: dict[str, Any]
    created_at: str | None = None
    updated_at: str | None = None


class PreferenceStore:
    """会话级偏好的 SQLite 持久化存储（教学用 Store 适配器）。

    实现 ``get / put / search / all_for_user / set_many`` 五个方法，因此既能直接给
    节点当"偏好读写器"用，也能作为 Store 适配器注入 ``builder.compile(store=...)``。

    说明（教学适配器，可替换为官方持久化后端）：
        官方 ``BaseStore`` 的持久化实现（Postgres / Redis 等）自带命名空间、
        向量检索与 TTL 语义；本项目为了"零外部依赖 + 可离线跑"用一个 SQLite
        小表实现同样的读写轮廓。二者差异：
        1) 没有 ``batch`` 与 ``list_namespaces``；
        2) ``search`` 只做前缀匹配 + 关键字过滤，没有向量相似度；
        3) 没有 TTL；删除语义由 ``delete`` 提供。
        替换方式：把 ``AppRuntime.create`` 里的 PreferenceStore 换成官方后端实例即可，
        节点的调用签名（all_for_user / set_many）保持不变。
    """

    def __init__(self, path: str | Path) -> None:
        """打开（必要时创建）偏好数据库。

        参数：
            path: SQLite 文件路径；父目录会自动创建。
        """
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # check_same_thread=False：FastAPI/Streamlit 侧可能在别的线程里调用，
        # 所有读写都走 self._lock，避免并发写冲突
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(PREFERENCES_TABLE_SQL)
            self._conn.commit()

    # -- 内部工具 ---------------------------------------------------------
    @staticmethod
    def namespace_for(user_id: str) -> tuple[str, ...]:
        """用户偏好所在的命名空间。"""
        return ("users", str(user_id or DEFAULT_USER_ID), "preferences")

    @staticmethod
    def _flat(namespace: Any) -> str:
        """把元组/列表命名空间拍平成 ``a/b/c`` 字符串。"""
        parts = namespace if isinstance(namespace, (tuple, list)) else (namespace,)
        return "/".join(str(part) for part in parts if str(part))

    def _row_to_item(self, row: sqlite3.Row) -> PreferenceItem:
        """把一行记录转换成 PreferenceItem。"""
        try:
            payload = json.loads(row["value"])
        except (TypeError, ValueError):
            payload = {}
        value = payload.get("value") if isinstance(payload, dict) else payload
        return PreferenceItem(
            namespace=tuple(str(row["namespace"]).split("/")),
            key=str(row["key"]),
            value=value if isinstance(value, dict) else {"value": value},
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
        )

    # -- Store 轮廓 -------------------------------------------------------
    def get(self, namespace: Any, key: str) -> PreferenceItem | None:
        """读取单条偏好；不存在返回 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM preferences WHERE namespace = ? AND key = ?",
                (self._flat(namespace), str(key)),
            ).fetchone()
        return None if row is None else self._row_to_item(row)

    def put(self, namespace: Any, key: str, value: dict[str, Any]) -> None:
        """写入/覆盖单条偏好（value 会先做 JSON 校验）。"""
        namespace_key = self._flat(namespace)
        parts = namespace_key.split("/")
        user_id = parts[1] if len(parts) >= 3 else DEFAULT_USER_ID
        payload = jsonable(value if isinstance(value, dict) else {"value": value})
        stamp = _now_iso()
        with self._lock:
            self._conn.execute(
                "INSERT INTO preferences (user_id, namespace, key, value, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, namespace, key) DO UPDATE SET "
                "value = excluded.value, updated_at = excluded.updated_at",
                (
                    str(user_id),
                    namespace_key,
                    str(key),
                    json.dumps({"value": payload}, ensure_ascii=False, sort_keys=True),
                    stamp,
                    stamp,
                ),
            )
            self._conn.commit()

    def search(
        self,
        namespace_prefix: Any,
        *,
        limit: int = 100,
        offset: int = 0,
        filter: dict[str, Any] | None = None,
    ) -> list[PreferenceItem]:
        """按命名空间前缀搜索偏好（前缀匹配 + 可选等值过滤）。"""
        prefix = self._flat(namespace_prefix)
        pattern = f"{prefix}%" if prefix else "%"
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM preferences WHERE namespace LIKE ? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (pattern, int(limit), int(offset)),
            ).fetchall()
        items = [self._row_to_item(row) for row in rows]
        if filter:
            items = [
                item
                for item in items
                if all(str(item.value.get(str(key))) == str(value) for key, value in dict(filter).items())
            ]
        return items

    def all_for_user(self, user_id: str) -> dict[str, Any]:
        """返回该用户的全部偏好（节点读取用）。

        参数：
            user_id: 用户标识。

        返回：
            dict：``{key: 标量值}``；没有记录时返回空 dict。

        说明：
            ``put`` / ``set_many`` 落盘时统一写成 ``{"value": X}``（与官方 Store 的
            ``Item.value`` 是 dict 对齐）；这里必须解包回标量 ``X``（例如 ``"万元"``），
            否则 ``effective_preferences`` 做取值校验时会拿到 dict 而抛
            ``TypeError: unhashable type: 'dict'``。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM preferences WHERE namespace = ?",
                (self._flat(self.namespace_for(user_id)),),
            ).fetchall()
        values: dict[str, Any] = {}
        for row in rows:
            values[str(row["key"])] = _unwrap_preference_value(self._row_to_item(row).value)
        return values

    def set_many(self, user_id: str, values: dict[str, Any]) -> None:
        """批量写入用户偏好（一次事务，失败整体回滚）。

        参数：
            user_id: 用户标识。
            values: 待写入的偏好键值；``None`` 表示删除该键。
        """
        namespace_key = self._flat(self.namespace_for(user_id))
        stamp = _now_iso()
        with self._lock:
            try:
                for key, value in dict(values or {}).items():
                    if value is None:
                        self._conn.execute(
                            "DELETE FROM preferences WHERE namespace = ? AND key = ?",
                            (namespace_key, str(key)),
                        )
                        continue
                    payload = jsonable(value)
                    self._conn.execute(
                        "INSERT INTO preferences (user_id, namespace, key, value, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(user_id, namespace, key) DO UPDATE SET "
                        "value = excluded.value, updated_at = excluded.updated_at",
                        (
                            str(user_id or DEFAULT_USER_ID),
                            namespace_key,
                            str(key),
                            json.dumps({"value": payload}, ensure_ascii=False, sort_keys=True),
                            stamp,
                            stamp,
                        ),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def delete(self, namespace: Any, key: str) -> None:
        """删除单条偏好（不存在时静默返回）。"""
        with self._lock:
            self._conn.execute(
                "DELETE FROM preferences WHERE namespace = ? AND key = ?",
                (self._flat(namespace), str(key)),
            )
            self._conn.commit()

    def close(self) -> None:
        """关闭底层连接（重复调用安全）。"""
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def defined_keys(self) -> tuple[str, ...]:
        """返回支持的偏好键（供 API/UI 做校验提示）。"""
        return tuple(PREFERENCE_KEYS)


# ---------------------------------------------------------------------------
# AppRuntime
# ---------------------------------------------------------------------------


def _locked_turn(method):
    """一次业务操作的读/验证/推进/结果快照在同一临界区内完成。"""

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)

    return wrapped


@dataclass
class AppRuntime:
    """一个进程内的运行时：连接、知识库、模型、检查点、编译好的图与锁。

    使用方式（FastAPI 的 lifespan / Streamlit 的进程内单例 / CLI）::

        runtime = AppRuntime.create(settings)
        try:
            turn = runtime.invoke_turn("2026 年九月销售额比八月变化多少？")
            turn = runtime.resume_turn(turn["thread_id"], "approve")
        finally:
            runtime.close()
    """

    settings: Any
    conn: sqlite3.Connection
    kb: Any
    llm: Any
    prefs: PreferenceStore
    checkpointer: Any
    graph: Any
    metrics: MetricsCollector = field(default_factory=MetricsCollector)
    lock: Any = field(default_factory=threading.RLock)
    checkpoint_conn: sqlite3.Connection | None = None
    closed: bool = False

    # -- 装配 -------------------------------------------------------------
    @classmethod
    def create(
        cls,
        settings: Settings | None = None,
        *,
        checkpointer: Any | None = None,
        use_memory_checkpointer: bool = False,
    ) -> "AppRuntime":
        """装配运行时。

        参数：
            settings: 配置对象；缺省时调用 ``load_settings()``。
            checkpointer: 外部注入的检查点实现（测试用），注入后不会再建 SQLite 连接。
            use_memory_checkpointer: 是否使用 ``InMemorySaver``（仅测试/实验路径）。

        返回：
            AppRuntime：已编译好工作流图的运行时实例。
        """
        settings = settings or load_settings()
        _call_ensure_dirs(settings)
        root = _project_root()
        data_dir = _settings_path(settings, "data_dir", default=root / "data")
        db_path = _settings_path(settings, "db_path", "sales_db_path", default=data_dir / "ops.db")
        checkpoint_path = _settings_path(
            settings,
            "checkpoint_db_path",
            "checkpoint_path",
            "checkpoint_db",
            default=data_dir / "checkpoints.db",
        )
        prefs_path = _settings_path(
            settings,
            "preference_db_path",
            "prefs_path",
            "preferences_path",
            default=data_dir / "preferences.db",
        )

        conn = _open_data_connection(db_path)
        kb = _build_knowledge_base(settings)
        llm = _build_llm(settings)
        prefs = PreferenceStore(prefs_path)

        checkpoint_conn: sqlite3.Connection | None = None
        if checkpointer is None:
            if use_memory_checkpointer:
                checkpointer = InMemorySaver()
            else:
                checkpoint_conn = sqlite3.connect(str(checkpoint_path), check_same_thread=False)
                checkpointer = SqliteSaver(checkpoint_conn)
                checkpointer.setup()

        graph = build_graph(settings=settings, checkpointer=checkpointer, store=prefs)
        return cls(
            settings=settings,
            conn=conn,
            kb=kb,
            llm=llm,
            prefs=prefs,
            checkpointer=checkpointer,
            graph=graph,
            metrics=MetricsCollector(),
            lock=threading.RLock(),
            checkpoint_conn=checkpoint_conn,
        )

    # -- 关闭 -------------------------------------------------------------
    def close(self) -> None:
        """释放所有连接（幂等；关闭后再调用其它方法会抛 RuntimeError）。"""
        if self.closed:
            return
        self.closed = True
        with self.lock:
            for name, closer in (
                ("checkpoint", self._close_checkpointer),
                ("preferences", self.prefs.close),
                ("db", self.conn.close),
            ):
                try:
                    closer()
                except Exception as exc:  # 关闭失败只记日志，不掩盖主流程结果
                    logger.warning("关闭 %s 连接失败：%s: %s", name, type(exc).__name__, exc)

    def _close_checkpointer(self) -> None:
        """关闭检查点后端（SQLite 连接自己持有，InMemorySaver 没有连接）。"""
        if self.checkpoint_conn is not None:
            self.checkpoint_conn.close()
            self.checkpoint_conn = None

    def _ensure_open(self) -> None:
        """确保运行时仍可用。"""
        if self.closed:
            raise RuntimeError("AppRuntime 已关闭，请重新创建运行时后再调用。")

    # -- 上下文与配置 -----------------------------------------------------
    @property
    def store(self) -> PreferenceStore:
        """偏好存储；同时作为 ``builder.compile(store=...)`` 注入的 Store。"""
        return self.prefs

    @property
    def max_steps(self) -> int:
        """单轮最大步数（来自配置，默认 12）。"""
        return int(getattr(self.settings, "max_steps", 12) or 12)

    def _context(self, user_id: str) -> AssistantContext:
        """构造节点依赖上下文（一次 invoke 复用同一份）。"""
        return {
            "settings": self.settings,
            "conn": self.conn,
            "kb": self.kb,
            "llm": self.llm,
            "prefs": self.prefs,
            "metrics": self.metrics,
        }

    def _config(self, thread_id: str, user_id: str, request_id: str) -> dict[str, Any]:
        """构造 invoke 配置：thread_id 隔离 + recursion_limit（框架保险丝）。

        关于 ``recursion_limit`` 的取值：
            它只做"兜底保险丝"，真正的预算控制由节点的 ``BudgetTracker`` 完成。
            最坏路径长度 ≈ ``max_steps`` 个消耗预算的节点 + 1 个 skip 节点 +
            ``clarify``/``fail`` 两个不消耗预算的节点，再加 1 的框架计数余量，
            即 ``max_steps + 4``；同时 LangGraph 在执行完第 ``recursion_limit``
            个超级步后仍需要一次调度判断，所以下限取 8 才能覆盖 ``max_steps``
            很小的实验场景（例如测试里 ``max_steps=2`` 时路径为
            understand → knowledge → data(skip) → fail，共 4 步 + 1）。
            早期版本写成 ``max(4, max_steps * 2)``，在 ``max_steps <= 2`` 时会先于
            BudgetTracker 触发 ``GraphRecursionError``，被 ``invoke_turn`` 收敛成
            ``status="failed"`` 而不是设计要求的 ``budget_exceeded``。
        """
        return {
            "configurable": {"thread_id": thread_id, "user_id": user_id},
            "recursion_limit": max(8, self.max_steps * 2),
            "metadata": {
                "thread_id": thread_id,
                "user_id": user_id,
                "request_id": request_id,
            },
        }

    # -- 对外动作 ---------------------------------------------------------
    @_locked_turn
    def invoke_turn(
        self,
        question: str,
        *,
        thread_id: str | None = None,
        user_id: str = DEFAULT_USER_ID,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """开启/继续一轮问答，跑到"需要人工审批"或终态为止。

        参数：
            question: 用户问题。
            thread_id: 会话 id；缺省生成新的。
            user_id: 用户 id（决定偏好与线程可见性）。
            request_id: 请求追踪 id；缺省生成。

        返回：
            dict：``TurnResult``（thread_id/status/answer/report/interrupt/trace/...）。
        """
        self._ensure_open()
        thread_id = str(thread_id or new_request_id())
        request_id = request_id or new_request_id()
        user_id = str(user_id or DEFAULT_USER_ID)
        started = time.perf_counter()
        existing = self._get_snapshot(thread_id, user_id)
        if _extract_interrupt(existing) is not None:
            raise ValueError("PENDING_APPROVAL: 请先批准、编辑或拒绝当前报告，再开始新一轮。")
        state = initial_state(question, thread_id, user_id, self.settings)
        config = self._config(thread_id, user_id, request_id)
        context = self._context(user_id)
        try:
            with self.lock:  # 同一时刻只推进一张图，避免共享连接/检查点被并发写
                values = self.graph.invoke(state, config=config, context=context)
        except Exception as exc:
            self.metrics.record_error(
                code="GRAPH_INVOKE_ERROR",
                message=f"{type(exc).__name__}: {exc}",
                thread_id=thread_id,
                node="runtime",
                request_id=request_id,
            )
            log_structured(
                event="turn.failed",
                request_id=request_id,
                thread_id=thread_id,
                ok=False,
                error_code=type(exc).__name__,
                duration_ms=elapsed_ms_since(started),
            )
            return self._failed_result(
                thread_id,
                request_id,
                started,
                f"GRAPH_ERROR: 工作流执行失败（{type(exc).__name__}: {exc}）。",
            )
        log_structured(
            event="turn.finished",
            request_id=request_id,
            thread_id=thread_id,
            ok=True,
            duration_ms=elapsed_ms_since(started),
            detail={"status": str((values or {}).get("status") or "")},
        )
        return self._turn_result(thread_id, values or {}, user_id=user_id, request_id=request_id, started=started)

    @_locked_turn
    def resume_turn(
        self,
        thread_id: str,
        decision: str,
        *,
        interrupt_id: str | None = None,
        report_id: str | None = None,
        content_hash: str | None = None,
        comment: str = "",
        edited_report: str = "",
        user_id: str = DEFAULT_USER_ID,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """恢复一个停在审批 interrupt 上的会话。

        参数：
            thread_id: 会话 id。
            decision: ``approve`` / ``reject`` / ``edit``。
            comment: 审批备注（可选）。
            edited_report: ``edit`` 时的原稿及末尾无数字备注，正文不可改。
            user_id: 用户 id。
            request_id: 请求追踪 id。

        返回：
            dict：``TurnResult``。

        异常：
            ValueError: decision 非法，或该会话没有待审批报告。
            ThreadNotFoundError: 会话不存在。
        """
        self._ensure_open()
        decision = str(decision or "").strip().lower()
        if decision not in VALID_DECISIONS:
            raise ValueError(f"decision 必须是 {VALID_DECISIONS} 之一，收到：{decision or '（空）'}")
        thread_id = str(thread_id or "")
        request_id = request_id or new_request_id()
        user_id = str(user_id or DEFAULT_USER_ID)
        started = time.perf_counter()

        snapshot = self._get_snapshot(thread_id, user_id)
        pending = _extract_interrupt(snapshot)
        values = dict(getattr(snapshot, "values", None) or {})
        if report_id is not None and report_id != values.get("report_id"):
            raise ValueError("STALE_APPROVAL: 报告版本已经变化，请刷新后确认。")
        current_id = pending["id"] if pending else (values.get("review") or {}).get("interrupt_id")
        if interrupt_id is not None and interrupt_id != current_id:
            raise ValueError("STALE_APPROVAL: 审批中断已变化，请刷新后确认。")
        from ops_assistant.report.export import report_digest

        if content_hash is not None and content_hash != report_digest(str(values.get("draft_report") or "")):
            raise ValueError("STALE_APPROVAL: 草稿内容已变化。")
        if decision == "edit":
            _, edit_errors = _edited_report_or_draft(edited_report, str(values.get("draft_report") or ""))
            if edit_errors:
                raise ValueError(edit_errors[0])
        if pending is None:
            # 已经没有待审批的中断：如果是"已批准并导出过"，直接返回同一结果（幂等）
            if (
                decision == "approve"
                and values.get("status") == "failed"
                and (values.get("review") or {}).get("decision") in {"approve", "edit"}
                and any(str(e).startswith("EXPORT_ERROR:") for e in values.get("errors", []))
            ):
                config = self.graph.update_state(
                    self._config(thread_id, user_id, request_id),
                    {"status": "approved", "errors": Overwrite([])},
                    as_node="review",
                )
                values = self.graph.invoke(None, config=config, context=self._context(user_id))
                return self._turn_result(thread_id, values, user_id=user_id, request_id=request_id, started=started)
            if decision == "approve" and values.get("status") == "approved" and values.get("export_path"):
                return self._turn_result(thread_id, values, user_id=user_id, request_id=request_id, started=started)
            raise ValueError("该会话没有待审批的报告：请先调用 /api/chat 生成报告草稿。")

        payload = {
            "decision": decision,
            "_interrupt_id": pending["id"],
            "comment": str(comment or ""),
            "edited_report": str(edited_report or ""),
        }
        config = self._config(thread_id, user_id, request_id)
        context = self._context(user_id)
        try:
            with self.lock:
                values = self.graph.invoke(Command(resume=payload), config=config, context=context)
        except Exception as exc:
            self.metrics.record_error(
                code="GRAPH_RESUME_ERROR",
                message=f"{type(exc).__name__}: {exc}",
                thread_id=thread_id,
                node="runtime",
                request_id=request_id,
            )
            return self._failed_result(
                thread_id,
                request_id,
                started,
                f"GRAPH_ERROR: 审批恢复失败（{type(exc).__name__}: {exc}）。",
            )
        log_structured(
            event="turn.resumed",
            request_id=request_id,
            thread_id=thread_id,
            ok=True,
            duration_ms=elapsed_ms_since(started),
            detail={"decision": decision},
        )
        return self._turn_result(thread_id, values or {}, user_id=user_id, request_id=request_id, started=started)

    def read_thread(
        self,
        thread_id: str,
        *,
        user_id: str = DEFAULT_USER_ID,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """从检查点读出一个会话的当前状态（UI 刷新时用，不推进图）。"""
        self._ensure_open()
        thread_id = str(thread_id or "")
        request_id = request_id or new_request_id()
        started = time.perf_counter()
        snapshot = self._get_snapshot(thread_id, user_id)
        values = dict(getattr(snapshot, "values", None) or {})
        if not values:
            raise ThreadNotFoundError(f"会话不存在或还没有历史：{thread_id}")
        return self._turn_result(
            thread_id,
            values,
            user_id=user_id,
            request_id=request_id,
            started=started,
            snapshot=snapshot,
        )

    def list_threads(self, *, user_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """列出会话（按最近更新时间倒序），供 UI 的"会话列表"使用。"""
        self._ensure_open()
        limit = max(1, int(limit or 50))
        with self.lock:
            threads = self._collect_threads(limit=limit)
        if user_id:
            filtered = [item for item in threads if not item.get("user_id") or str(item.get("user_id")) == str(user_id)]
            threads = filtered
        return threads[:limit]

    @_locked_turn
    def list_reports(self, thread_id: str, user_id: str = DEFAULT_USER_ID) -> list[dict[str, Any]]:
        """列出该会话已经导出的各版本；thread 归属仍由当前状态验证。"""
        self._get_snapshot(thread_id, user_id)
        seen = {}
        for snapshot in self.graph.get_state_history(self._config(thread_id, user_id, new_request_id())):
            values = snapshot.values
            report_id = values.get("report_id")
            if report_id and values.get("status") == "approved" and values.get("export_path"):
                seen.setdefault(
                    report_id,
                    {
                        "report_id": report_id,
                        "export_path": values["export_path"],
                        "question": values.get("question", ""),
                    },
                )
        return list(seen.values())

    @_locked_turn
    def report_path(self, thread_id: str, report_id: str, user_id: str = DEFAULT_USER_ID) -> str:
        for item in self.list_reports(thread_id, user_id):
            if item["report_id"] == report_id:
                return str(item["export_path"])
        raise ThreadNotFoundError("报告不存在或尚未批准。")

    def health(self) -> dict[str, Any]:
        """健康检查信息（``/api/health`` 与 CLI 共用）。

        返回：
            dict：``status / model_enabled / kb_chunks / db_ok / max_steps /
            max_tool_calls / checkpointer / closed``。
        """
        db_ok = True
        try:
            self.conn.execute("SELECT 1").fetchone()
        except Exception:
            db_ok = False
        kb_chunks = 0
        stats = getattr(self.kb, "stats", None)
        if callable(stats):
            try:
                data = stats()
                if isinstance(data, dict):
                    kb_chunks = int(data.get("chunk_count") or data.get("chunks") or data.get("total_chunks") or 0)
            except Exception:
                kb_chunks = 0
        return {
            "status": "ok" if db_ok and not self.closed else "degraded",
            "model_enabled": bool(getattr(self.llm, "enabled", False)),
            "kb_chunks": kb_chunks,
            "db_ok": db_ok,
            "max_steps": self.max_steps,
            "max_tool_calls": int(getattr(self.settings, "max_tool_calls", 8) or 8),
            "checkpointer": type(self.checkpointer).__name__,
            "closed": self.closed,
        }

    def usage_stats(self) -> dict[str, Any]:
        """用量统计（``/api/usage`` 用）。

        返回：
            dict：``{"usage": 本轮/最近一次的模型与耗时快照, "metrics": 进程级累计指标}``。
        """
        return {
            "usage": self._usage_snapshot(time.perf_counter()),
            "metrics": self.metrics.snapshot(),
        }

    # -- 内部实现 ---------------------------------------------------------
    def _collect_threads(self, *, limit: int) -> list[dict[str, Any]]:
        """遍历检查点，按 thread 去重后返回最新一条状态摘要。"""
        try:
            iterator: Iterator[Any] = self.checkpointer.list(None, limit=max(limit * 4, 100))
        except TypeError:
            iterator = self.checkpointer.list(None)
        except Exception as exc:
            logger.warning("列出会话失败：%s: %s", type(exc).__name__, exc)
            return []
        seen: dict[str, dict[str, Any]] = {}
        count = 0
        for item in iterator:
            count += 1
            if count > max(limit * 8, 400):
                break
            config = getattr(item, "config", None) or {}
            configurable = (config or {}).get("configurable") or {}
            thread_id = str(configurable.get("thread_id") or "")
            if not thread_id or thread_id in seen:
                continue
            metadata = getattr(item, "metadata", None) or {}
            checkpoint = getattr(item, "checkpoint", None) or {}
            channel_values = (checkpoint.get("channel_values") or {}) if isinstance(checkpoint, dict) else {}
            ts = str(checkpoint.get("ts") or "") if isinstance(checkpoint, dict) else ""
            seen[thread_id] = {
                "thread_id": thread_id,
                "user_id": str(metadata.get("user_id") or ""),
                "updated_at": ts,
                "question": str(channel_values.get("question") or ""),
                "status": str(channel_values.get("status") or ""),
            }
        return sorted(seen.values(), key=lambda item: item.get("updated_at") or "", reverse=True)

    def _get_snapshot(self, thread_id: str, user_id: str) -> Any:
        """读取状态快照（内部统一加锁，避免与正在跑的 invoke 交错）。"""
        if not thread_id:
            raise ThreadNotFoundError("thread_id 不能为空")
        config = self._config(thread_id, user_id, new_request_id())
        try:
            with self.lock:
                snapshot = self.graph.get_state(config)
                owner = (snapshot.values or {}).get("user_id")
                if owner and owner != user_id:
                    raise ThreadNotFoundError("会话不存在或不属于该用户。")
                return snapshot
        except ThreadNotFoundError:
            raise
        except Exception as exc:
            raise ThreadNotFoundError(f"读取会话状态失败（{type(exc).__name__}: {exc}）：{thread_id}") from exc

    def _failed_result(
        self,
        thread_id: str,
        request_id: str,
        started: float,
        message: str,
    ) -> dict[str, Any]:
        """构造失败形态的 TurnResult（不抛异常给调用方，便于 API/UI 统一展示）。"""
        usage = self._usage_snapshot(started)
        metrics_snapshot = self.metrics.snapshot()
        return {
            "thread_id": thread_id,
            "status": "failed",
            "answer": message,
            "report": "",
            "clarification": "",
            "interrupt": None,
            "sources": [],
            "tables": [],
            "chart": None,
            "trace": [],
            "usage": usage,
            "metrics": metrics_snapshot,
            "errors": [message],
            "export_path": None,
            "request_id": request_id,
        }

    def _usage_snapshot(self, started: float, values: dict[str, Any] | None = None) -> dict[str, Any]:
        """返回用量快照：模型侧累计 token + 本轮图状态计数 + 请求耗时。

        参数：
            started: 请求起点（time.perf_counter 值）。
            values: 图状态字典；提供时按规格 5.10 把 ``budget`` 里的本轮
                步数/工具调用计数合并进来（``steps`` / ``tool_calls`` /
                ``max_steps`` / ``max_tool_calls``）。
        """
        raw: dict[str, Any] = {}
        usage = getattr(self.llm, "usage", None)
        if callable(usage):
            try:
                candidate = usage()
                if isinstance(candidate, dict):
                    raw = dict(candidate)
            except Exception:
                raw = {}
        prompt_tokens = int(raw.get("prompt_tokens") or 0)
        completion_tokens = int(raw.get("completion_tokens") or 0)
        snapshot: dict[str, Any] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": int(raw.get("total_tokens") or (prompt_tokens + completion_tokens)),
            "model_calls": int(raw.get("model_calls") or 0),
            "elapsed_ms": elapsed_ms_since(started),
        }
        if values is not None:
            budget = values.get("budget") if isinstance(values.get("budget"), dict) else {}
            snapshot["steps"] = int(budget.get("steps") or 0)
            snapshot["max_steps"] = int(budget.get("max_steps") or self.max_steps)
            snapshot["tool_calls"] = int(budget.get("tool_calls") or 0)
            snapshot["max_tool_calls"] = int(budget.get("max_tool_calls", self.settings.max_tool_calls))
        return snapshot

    def _turn_result(
        self,
        thread_id: str,
        values: dict[str, Any],
        *,
        user_id: str,
        request_id: str,
        started: float,
        snapshot: Any | None = None,
    ) -> dict[str, Any]:
        """把图状态转换成对外的 TurnResult（JSON 友好）。

        参数：
            thread_id: 会话 id。
            values: 图状态字典。
            user_id: 用户 id（读取待审批 interrupt 时需要）。
            request_id: 请求追踪 id。
            started: 请求起点（算 elapsed_ms）。
            snapshot: 已有的状态快照；为 None 时按需重新读取。
        """
        status = str(values.get("status") or "unknown")
        pending: dict[str, Any] | None = None
        if status in {"awaiting_approval", "running", "approved"}:
            try:
                source = snapshot if snapshot is not None else self._get_snapshot(thread_id, user_id)
                pending = _extract_interrupt(source)
            except Exception:  # 读快照失败不影响返回已有状态
                pending = None
        # 报告字段的取值规则：
        #   approved → final_report（export 节点保证与落盘内容一致）；
        #   awaiting_approval → final_report 为空时回退 draft_report（UI 要预览草稿）；
        #   rejected → 只认 final_review 置空的 final_report，绝不回退草稿，
        #             否则"拒绝后仍能看到报告"会被误读成"其实导出了"。
        report = str(values.get("final_report") or "")
        if not report and status != "rejected":
            report = str(values.get("draft_report") or "")
        return {
            "thread_id": thread_id,
            "status": status,
            "report_id": str(values.get("report_id") or ""),
            "approval_interrupt_id": str((values.get("review") or {}).get("interrupt_id") or ""),
            "answer": _answer_text(values),
            "report": report,
            "clarification": str(values.get("clarification") or ""),
            "interrupt": pending,
            "sources": [item for item in (values.get("sources") or []) if isinstance(item, dict)],
            "tables": [item for item in (values.get("tables") or []) if isinstance(item, dict)],
            "chart": values.get("chart") if isinstance(values.get("chart"), dict) else None,
            "trace": [item for item in (values.get("trace") or []) if isinstance(item, dict)][-200:],
            "usage": self._usage_snapshot(started, values=values),
            # metrics 是进程级累计值（model_calls/tool_calls/errors 跨请求累加），
            # 单轮的步数与工具调用计数看 usage.steps / usage.tool_calls。
            "metrics": self.metrics.snapshot(started_at=started),
            "errors": [str(item) for item in (values.get("errors") or [])],
            "export_path": str(values.get("export_path") or "") or None,
            "request_id": request_id,
            "messages": _messages(values.get("messages") or []),
            "spec": values.get("spec") if isinstance(values.get("spec"), dict) else {},
            "calculation": values.get("calculation") if isinstance(values.get("calculation"), dict) else {},
            "updated_at": _now_iso(),
        }


# ---------------------------------------------------------------------------
# 状态快照小工具
# ---------------------------------------------------------------------------


def _extract_interrupt(snapshot: Any) -> dict[str, Any] | None:
    """从状态快照里取出待处理的中断（reject 之后返回 None）。

    参数：
        snapshot: ``graph.get_state(config)`` 返回的 StateSnapshot。

    返回：
        dict：``{"id": ..., "payload": ...}``；没有待处理中断时返回 None。
    """
    if snapshot is None:
        return None
    candidates: list[Any] = []
    raw = getattr(snapshot, "interrupts", None)
    if raw:
        candidates.extend(list(raw))
    if not candidates:
        for task in getattr(snapshot, "tasks", ()) or ():
            for item in getattr(task, "interrupts", ()) or ():
                candidates.append(item)
    if not candidates:
        return None
    first = candidates[0]
    if isinstance(first, dict):
        value = first.get("value")
        interrupt_id = first.get("id")
    else:
        value = getattr(first, "value", None)
        interrupt_id = getattr(first, "id", None)
    return {"id": str(interrupt_id or ""), "payload": jsonable(value)}


def _messages(raw: Any) -> list[dict[str, str]]:
    """把 LangChain 消息列表转成 ``[{"role": ..., "content": ...}]``（JSON 友好）。"""
    result: list[dict[str, str]] = []
    for message in raw or []:
        role = "assistant" if isinstance(message, AIMessage) else "user"
        content = str(getattr(message, "content", "") or "")
        if not content.strip():
            continue
        result.append({"role": role, "content": content})
    return result


def _answer_text(values: dict[str, Any]) -> str:
    """给出一句话的"当前答复"，供 UI 聊天区直接显示。"""
    status = str(values.get("status") or "")
    export_path = str(values.get("export_path") or "")
    if status == "approved" and export_path:
        return f"报告已批准并导出到：{export_path}"
    if status == "awaiting_approval":
        return "报告草稿已生成，等待人工审批（approve 导出 / edit 修改后导出 / reject 不导出）。"
    if status == "clarification":
        return str(values.get("clarification") or "请补充更多信息后再试。")
    if status in {"failed", "budget_exceeded", "rejected"}:
        errors = [str(item) for item in (values.get("errors") or []) if str(item).strip()]
        if errors:
            return errors[-1]
    for message in reversed(list(values.get("messages") or [])):
        if isinstance(message, AIMessage):
            content = str(getattr(message, "content", "") or "").strip()
            if content:
                return content
    return "本轮没有产生可展示的答复。"
