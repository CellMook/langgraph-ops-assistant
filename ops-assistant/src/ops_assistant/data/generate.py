"""固定种子数据生成器（工程规格 3.2）。

模块职责：

1. 用 ``random.Random(SEED)`` 生成 5 个地区、12 个商品、``2025-01`` 至 ``2026-09`` 的订单与退款；
   不依赖系统时间、不依赖 ``hash()`` 随机化，同一 Python 小版本下结果完全可复现。
2. 显式构造工程规格 3.2 要求的 8 类边界事实（见 ``_construct_*`` 函数与 ``_build_orders``）。
3. ``generate_database`` 是唯一的副作用入口（删旧库、建表、写数据、自检）；
   **import 本模块不会创建或修改任何文件**。

金额一律用整数分：
- 订单金额 = ``quantity * unit_price_cents``。
- 订单成交价 = 商品目录价 × (0.90～1.10)，用整数千分比实现“四舍五入到分”，避免浮点误差。
"""

from __future__ import annotations

import argparse
import calendar
import json
import random
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .db import connect, init_schema

#: 固定随机种子（工程规格 3.2）。
SEED: int = 20260901

#: 数据声明的月份范围（闭区间）。
DATA_FIRST_MONTH: str = "2025-01"
DATA_LAST_MONTH: str = "2026-09"

#: 每个地区每月订单数范围（含端点）。
ORDERS_PER_REGION_MONTH: tuple[int, int] = (6, 14)
#: 单笔订单数量范围（含端点）。
QUANTITY_RANGE: tuple[int, int] = (1, 8)
#: 成交价相对目录价的千分比范围（900 = 0.90，1100 = 1.10）。
PRICE_PERMILLE_RANGE: tuple[int, int] = (900, 1100)
#: 每笔订单产生退款记录的概率。
REFUND_PROBABILITY: float = 0.08
#: 退款日期相对下单日期最多延后天数（0～40 天，可能跨月）。
REFUND_MAX_DELAY_DAYS: int = 40
#: 退款状态权重（合计 1.0）。
REFUND_STATUS_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("approved", 0.7),
    ("pending", 0.2),
    ("rejected", 0.1),
)

#: 渠道取值：数据里存在，但分析工具不暴露该维度（规格 3.2 边界 8 / 第 4 节第 11 条）。
CHANNELS: tuple[str, ...] = ("线下", "天猫", "京东", "抖音")
#: 退款原因候选。
REFUND_REASONS: tuple[str, ...] = ("质量问题", "七天无理由", "发错货", "价格保护", "尺寸不合适")

#: 5 个地区：``(region_id, region_name, manager)``。
REGIONS: tuple[tuple[str, str, str], ...] = (
    ("R_EAST", "华东", "王磊"),
    ("R_NORTH", "华北", "李娜"),
    ("R_SOUTH", "华南", "陈强"),
    ("R_SW", "西南", "赵敏"),
    ("R_NE", "东北", "周涛"),
)

#: 12 个商品、4 个品类：``(product_id, product_name, category, 目录价分)``。
#: 目录价全部落在规格要求的 1999～19999 分之间。
PRODUCTS: tuple[tuple[str, str, str, int], ...] = (
    ("P_001", "无线降噪耳机", "数码", 19999),
    ("P_002", "智能手表", "数码", 16000),
    ("P_003", "机械键盘", "数码", 4999),
    ("P_004", "记忆棉枕头", "家居", 3999),
    ("P_005", "落地阅读灯", "家居", 6999),
    ("P_006", "五层收纳柜", "家居", 9999),
    ("P_007", "三合一冲锋衣", "服饰", 15999),
    ("P_008", "轻量跑步鞋", "服饰", 8999),
    ("P_009", "纯棉衬衫", "服饰", 3499),
    ("P_010", "坚果礼盒", "食品", 5999),
    ("P_011", "精品咖啡豆", "食品", 4599),
    ("P_012", "有机蜂蜜", "食品", 1999),
)

#: 商品目录价索引：``product_id -> unit_price_cents``。
PRODUCT_PRICE_BY_ID: dict[str, int] = {
    product_id: list_price_cents
    for product_id, _product_name, _category, list_price_cents in PRODUCTS
}

# ---------------------------------------------------------------------------
# 8 类边界事实的固定参数（改这些常量会改变标准答案，必须同步通知 Lead）
# ---------------------------------------------------------------------------

#: 边界 1：整月无订单（无记录，不是销售额 0）。
EMPTY_MONTH: str = "2025-01"
#: 边界 2：这些「地区 + 月份」组合刻意不生成任何订单。
EMPTY_REGION_MONTHS: tuple[tuple[str, str], ...] = (
    ("R_NE", "2025-09"),
    ("R_NE", "2026-08"),
)
#: 边界 7：该商品在该月不生成任何订单（商品维度零记录）。
PRODUCT_ZERO_ID: str = "P_012"
PRODUCT_ZERO_MONTH: str = "2026-05"

#: 边界 3：真零——该地区该月的 approved 退款合计恰好等于当月毛销售额。
TRUE_ZERO_REGION_ID: str = "R_SW"
TRUE_ZERO_MONTH: str = "2026-09"

#: 边界 4：该地区该月只有 pending 退款（金额不计入净销售额）。
PENDING_ONLY_REGION_ID: str = "R_NORTH"
PENDING_ONLY_MONTH: str = "2026-07"

#: 边界 6：该地区该月退款额大于毛销售额，净额恰为 ``-NEGATIVE_BUFFER_CENTS``。
NEGATIVE_REGION_ID: str = "R_SOUTH"
NEGATIVE_MONTH: str = "2026-03"
NEGATIVE_BUFFER_CENTS: int = 50_000

#: 边界 5：跨月退款（订单在 8 月、退款在 9 月且 approved），至少 3 笔。
CROSS_MONTH_ORDER_MONTH: str = "2026-08"
CROSS_MONTH_REFUND_MONTH: str = "2026-09"
CROSS_MONTH_REGION_ID: str = "R_EAST"
CROSS_MONTH_TARGET_COUNT: int = 3
CROSS_MONTH_REFUND_DATES: tuple[str, ...] = ("2026-09-08", "2026-09-12", "2026-09-18")

#: 固定值跨月退款：退款号与金额写死，便于标准答案与测试直接断言。
#: 构造方式：把一笔 2026-08 华东订单改写为 ``P_002 × 8 件 × 16000 分``，随后整单退款，
#: 因此退款金额恒等于 ``PINNED_CROSS_MONTH_REFUND_CENTS = 128000`` 分（1280.00 元），
#: 与 ``SEED`` 无关，也不受随机数影响。
PINNED_REFUND_ID: str = "RF_XM_PINNED"
PINNED_PRODUCT_ID: str = "P_002"
PINNED_QUANTITY: int = 8
PINNED_UNIT_PRICE_CENTS: int = 16_000
PINNED_CROSS_MONTH_REFUND_CENTS: int = PINNED_QUANTITY * PINNED_UNIT_PRICE_CENTS


@dataclass(frozen=True)
class OrderRow:
    """一条订单记录（金额单位：分）。"""

    order_id: str
    order_date: str
    region_id: str
    product_id: str
    quantity: int
    unit_price_cents: int
    channel: str

    @property
    def total_cents(self) -> int:
        """订单金额（分）= 数量 × 成交单价。"""
        return self.quantity * self.unit_price_cents


@dataclass(frozen=True)
class RefundRow:
    """一条退款记录（金额单位：分）。"""

    refund_id: str
    order_id: str
    refund_date: str
    amount_cents: int
    reason: str
    status: str


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


def _iter_months(first_month: str, last_month: str) -> list[str]:
    """生成闭区间 ``[first_month, last_month]`` 内的所有 ``YYYY-MM``。

    参数：
        first_month：起始月份，形如 ``"2025-01"``。
        last_month：结束月份，形如 ``"2026-09"``。

    返回：
        升序月份字符串列表；``first_month > last_month`` 时返回空列表。
    """
    year, month_number = int(first_month[:4]), int(first_month[5:7])
    end_year, end_month_number = int(last_month[:4]), int(last_month[5:7])
    months: list[str] = []
    while (year, month_number) <= (end_year, end_month_number):
        months.append(f"{year:04d}-{month_number:02d}")
        month_number += 1
        if month_number > 12:
            month_number = 1
            year += 1
    return months


def _month_bounds(month: str) -> tuple[date, date]:
    """返回某月的第一天与最后一天（``date`` 对象）。"""
    year, month_number = int(month[:4]), int(month[5:7])
    last_day_number = calendar.monthrange(year, month_number)[1]
    return date(year, month_number, 1), date(year, month_number, last_day_number)


def _orders_in_month(
    orders: Iterable[OrderRow], month: str, region_id: str | None = None
) -> list[OrderRow]:
    """筛选某月（可选限定地区）的订单，按 ``order_id`` 升序返回，保证顺序确定。"""
    selected = [
        order
        for order in orders
        if order.order_date[:7] == month and (region_id is None or order.region_id == region_id)
    ]
    return sorted(selected, key=lambda order: order.order_id)


def _order_index(orders: Iterable[OrderRow]) -> dict[str, OrderRow]:
    """建立 ``order_id -> OrderRow`` 索引，供按退款反查订单地区/日期。"""
    return {order.order_id: order for order in orders}


def _pick_refund_status(rng: random.Random) -> str:
    """按 ``approved 0.7 / pending 0.2 / rejected 0.1`` 的权重抽取退款状态。"""
    threshold = rng.random()
    cumulative = 0.0
    for status, weight in REFUND_STATUS_WEIGHTS:
        cumulative += weight
        if threshold < cumulative:
            return status
    return REFUND_STATUS_WEIGHTS[-1][0]


def _pick_product(rng: random.Random, month: str) -> str:
    """抽取商品 ID；在 ``PRODUCT_ZERO_MONTH`` 刻意排除 ``PRODUCT_ZERO_ID``（边界 7）。"""
    candidates = [
        product_id
        for product_id, _product_name, _category, _price in PRODUCTS
        if not (month == PRODUCT_ZERO_MONTH and product_id == PRODUCT_ZERO_ID)
    ]
    return rng.choice(candidates)


def _refund_amount(rng: random.Random, order_total_cents: int) -> int:
    """随机退款金额：60% 概率整单退款，否则退 30%～90%（至少 1 分）。"""
    if rng.random() < 0.6:
        return order_total_cents
    ratio = rng.randint(30, 90)
    return max(1, order_total_cents * ratio // 100)


def _rewrite_order(orders: list[OrderRow], order_id: str, **changes: Any) -> OrderRow:
    """按 ``order_id`` 就地改写一条订单并返回新行（仅边界构造使用）。

    参数：
        orders：订单列表，会被就地修改。
        order_id：目标订单号。
        **changes：``dataclasses.replace`` 接受的字段覆盖值。

    返回：
        改写后的 :class:`OrderRow`。

    异常：
        RuntimeError：列表中找不到该订单（说明生成逻辑与常量不一致）。
    """
    for index, order in enumerate(orders):
        if order.order_id == order_id:
            rewritten = replace(order, **changes)
            orders[index] = rewritten
            return rewritten
    raise RuntimeError(f"边界构造失败：找不到订单 {order_id}")


# ---------------------------------------------------------------------------
# 基础生成：订单与退款
# ---------------------------------------------------------------------------


def _build_orders(rng: random.Random, months: list[str]) -> list[OrderRow]:
    """按规格 3.2 生成基础订单，并顺带落实边界 1、2、7、8。

    规则：
        - ``EMPTY_MONTH`` 整月跳过（边界 1）。
        - ``EMPTY_REGION_MONTHS`` 内的「地区 + 月份」整块跳过（边界 2）。
        - 每地区每月 ``6～14`` 笔订单；数量 ``1～8``；成交价 = 目录价 × 千分比 ``900～1100``，
          用 ``(price * permille + 500) // 1000`` 实现四舍五入到分。
        - ``PRODUCT_ZERO_MONTH`` 不抽 ``PRODUCT_ZERO_ID``（边界 7）。
        - 每笔订单都写入 ``channel``（边界 8）。

    参数：
        rng：固定种子的随机数发生器。
        months：升序月份列表。

    返回：
        订单列表（顺序即生成顺序，``order_id`` 全局唯一）。
    """
    orders: list[OrderRow] = []
    sequence = 0
    for month in months:
        if month == EMPTY_MONTH:
            continue  # 边界 1：2025-01 全月无订单（无记录，不是 0 元）
        first_day, last_day = _month_bounds(month)
        span_days = (last_day - first_day).days
        for region_id, _region_name, _manager in REGIONS:
            if (region_id, month) in EMPTY_REGION_MONTHS:
                continue  # 边界 2：东北在 2025-09、2026-08 没有订单
            order_count = rng.randint(*ORDERS_PER_REGION_MONTH)
            for _ in range(order_count):
                product_id = _pick_product(rng, month)
                quantity = rng.randint(*QUANTITY_RANGE)
                list_price_cents = PRODUCT_PRICE_BY_ID[product_id]
                permille = rng.randint(*PRICE_PERMILLE_RANGE)
                unit_price_cents = (list_price_cents * permille + 500) // 1000
                order_date = first_day + timedelta(days=rng.randint(0, span_days))
                channel = rng.choice(CHANNELS)
                sequence += 1
                order_id = f"O_{month.replace('-', '')}_{region_id[2:]}_{sequence:05d}"
                orders.append(
                    OrderRow(
                        order_id=order_id,
                        order_date=order_date.isoformat(),
                        region_id=region_id,
                        product_id=product_id,
                        quantity=quantity,
                        unit_price_cents=unit_price_cents,
                        channel=channel,
                    )
                )
    return orders


def _build_refunds(rng: random.Random, orders: list[OrderRow]) -> list[RefundRow]:
    """按 8% 概率为订单生成基础退款记录。

    规则：
        - 每笔订单有 ``REFUND_PROBABILITY``（8%）的概率产生一条退款记录。
        - 状态权重 ``approved 0.7 / pending 0.2 / rejected 0.1``。
        - ``refund_date = order_date + 0～40 天``；若超过数据范围末尾（2026-09-30），
          则收敛到 2026-09-30，保证数据库里不出现 2026-10 及以后的数据。

    参数：
        rng：固定种子的随机数发生器。
        orders：基础订单列表。

    返回：
        退款记录列表（退款号 ``RF_<order_id>``，与订单一一对应）。
    """
    _first_day, data_end = _month_bounds(DATA_LAST_MONTH)
    refunds: list[RefundRow] = []
    for order in orders:
        if rng.random() >= REFUND_PROBABILITY:
            continue
        delay_days = rng.randint(0, REFUND_MAX_DELAY_DAYS)
        refund_date = date.fromisoformat(order.order_date) + timedelta(days=delay_days)
        if refund_date > data_end:
            refund_date = data_end
        refunds.append(
            RefundRow(
                refund_id=f"RF_{order.order_id}",
                order_id=order.order_id,
                refund_date=refund_date.isoformat(),
                amount_cents=_refund_amount(rng, order.total_cents),
                reason=rng.choice(REFUND_REASONS),
                status=_pick_refund_status(rng),
            )
        )
    return refunds


# ---------------------------------------------------------------------------
# 边界构造：每个函数就地修改 orders / refunds 列表，并返回“写进标准答案”的事实
# ---------------------------------------------------------------------------


def _construct_cross_month_refunds(
    orders: list[OrderRow], refunds: list[RefundRow]
) -> dict[str, Any]:
    """边界 5：构造 ≥3 笔跨月退款（订单在 2026-08、退款在 2026-09 且 approved）。

    做法：
        1. 取 ``2026-08`` 华东地区订单号最小的 3 笔，删除这些订单已有的基础退款；
        2. 第一笔改写为 ``P_002 × 8 × 16000 分 = 128000 分`` 的整单退款，
           退款号为 ``PINNED_REFUND_ID``，金额即 ``PINNED_CROSS_MONTH_REFUND_CENTS``（固定值）；
        3. 另外两笔按各自订单全额退款，退款日期分别取 ``2026-09-12`` / ``2026-09-18``。

    参数：
        orders：订单列表（就地修改第一笔被改写的订单）。
        refunds：退款列表（就地增删）。

    返回：
        记录跨月退款明细与固定值退款的字典，会被写入 ``generate_database`` 的统计摘要。
    """
    candidates = _orders_in_month(orders, CROSS_MONTH_ORDER_MONTH, CROSS_MONTH_REGION_ID)
    if len(candidates) < CROSS_MONTH_TARGET_COUNT:
        raise RuntimeError(
            "边界 5 构造失败：2026-08 华东订单不足 "
            f"{CROSS_MONTH_TARGET_COUNT} 笔（实际 {len(candidates)} 笔）"
        )
    chosen = candidates[:CROSS_MONTH_TARGET_COUNT]
    chosen_order_ids = {order.order_id for order in chosen}
    refunds[:] = [refund for refund in refunds if refund.order_id not in chosen_order_ids]

    pinned_order = _rewrite_order(
        orders,
        chosen[0].order_id,
        product_id=PINNED_PRODUCT_ID,
        quantity=PINNED_QUANTITY,
        unit_price_cents=PINNED_UNIT_PRICE_CENTS,
    )
    if pinned_order.total_cents != PINNED_CROSS_MONTH_REFUND_CENTS:
        raise RuntimeError(
            "边界 5 构造失败：固定值退款金额不匹配，"
            f"订单金额 {pinned_order.total_cents} 分，期望 {PINNED_CROSS_MONTH_REFUND_CENTS} 分"
        )

    pinned_refund = RefundRow(
        refund_id=PINNED_REFUND_ID,
        order_id=pinned_order.order_id,
        refund_date=CROSS_MONTH_REFUND_DATES[0],
        amount_cents=PINNED_CROSS_MONTH_REFUND_CENTS,
        reason="七天无理由",
        status="approved",
    )
    added: list[RefundRow] = [pinned_refund]
    for offset, order in enumerate(chosen[1:], start=1):
        added.append(
            RefundRow(
                refund_id=f"RF_XM_{offset + 1:02d}",
                order_id=order.order_id,
                refund_date=CROSS_MONTH_REFUND_DATES[offset],
                amount_cents=order.total_cents,
                reason="七天无理由",
                status="approved",
            )
        )
    refunds.extend(added)

    return {
        "order_month": CROSS_MONTH_ORDER_MONTH,
        "refund_month": CROSS_MONTH_REFUND_MONTH,
        "region_id": CROSS_MONTH_REGION_ID,
        "count": len(added),
        "pinned": {
            "refund_id": pinned_refund.refund_id,
            "order_id": pinned_refund.order_id,
            "refund_date": pinned_refund.refund_date,
            "amount_cents": pinned_refund.amount_cents,
        },
        "items": [
            {
                "refund_id": refund.refund_id,
                "order_id": refund.order_id,
                "refund_date": refund.refund_date,
                "amount_cents": refund.amount_cents,
            }
            for refund in added
        ],
    }


def _construct_pending_only_refunds(
    orders: list[OrderRow], refunds: list[RefundRow]
) -> dict[str, Any]:
    """边界 4：华北 2026-07 的退款记录全部为 ``pending``，金额不计入净销售额。

    做法：
        1. 删除华北地区所有 ``refund_date`` 落在 ``2026-07`` 的退款（含 approved），
           保证该地区该月没有已批准退款；
        2. 为华北 2026-07 的前 2 笔订单各补一条 ``pending`` 退款（金额取订单金额的一半）。

    参数：
        orders：订单列表（只读）。
        refunds：退款列表（就地增删）。

    返回：
        ``region_id`` / ``month`` / 待处理退款笔数等事实。
    """
    index = _order_index(orders)
    refunds[:] = [
        refund
        for refund in refunds
        if not (
            refund.refund_date[:7] == PENDING_ONLY_MONTH
            and index[refund.order_id].region_id == PENDING_ONLY_REGION_ID
        )
    ]
    candidates = _orders_in_month(orders, PENDING_ONLY_MONTH, PENDING_ONLY_REGION_ID)[:2]
    if len(candidates) < 2:
        raise RuntimeError(
            "边界 4 构造失败：华北 2026-07 订单不足 2 笔，无法构造纯 pending 退款"
        )
    added = [
        RefundRow(
            refund_id=f"RF_PEND_{order.order_id}",
            order_id=order.order_id,
            refund_date=order.order_date,
            amount_cents=max(1, order.total_cents // 2),
            reason="价格保护",
            status="pending",
        )
        for order in candidates
    ]
    refunds.extend(added)
    return {
        "region_id": PENDING_ONLY_REGION_ID,
        "month": PENDING_ONLY_MONTH,
        "pending_refund_count": len(added),
        "approved_refund_cents": 0,
        "refund_ids": [refund.refund_id for refund in added],
    }


def _construct_true_zero_region(
    orders: list[OrderRow], refunds: list[RefundRow]
) -> dict[str, Any]:
    """边界 3：西南 2026-09 的已批准退款合计恰好等于该地区当月毛销售额。

    做法：
        1. 删除西南地区所有 ``refund_date`` 落在 ``2026-09`` 的退款，避免叠加；
        2. 为西南 2026-09 的每一笔订单补一条 ``approved`` 整单退款（``refund_date`` 取订单当天），
           合计正好等于该地区当月毛销售额，因此净额“真零”（有订单，但被退款冲抵）。

    参数：
        orders：订单列表（只读）。
        refunds：退款列表（就地增删）。

    返回：
        该地区该月的毛销售额、退款额、订单数等事实。
    """
    index = _order_index(orders)
    refunds[:] = [
        refund
        for refund in refunds
        if not (
            refund.refund_date[:7] == TRUE_ZERO_MONTH
            and index[refund.order_id].region_id == TRUE_ZERO_REGION_ID
        )
    ]
    candidates = _orders_in_month(orders, TRUE_ZERO_MONTH, TRUE_ZERO_REGION_ID)
    if not candidates:
        raise RuntimeError("边界 3 构造失败：西南 2026-09 没有订单，无法构造真零")
    gross_cents = sum(order.total_cents for order in candidates)
    refunds.extend(
        RefundRow(
            refund_id=f"RF_ZERO_{order.order_id}",
            order_id=order.order_id,
            refund_date=order.order_date,
            amount_cents=order.total_cents,
            reason="七天无理由",
            status="approved",
        )
        for order in candidates
    )
    return {
        "region_id": TRUE_ZERO_REGION_ID,
        "month": TRUE_ZERO_MONTH,
        "gross_cents": gross_cents,
        "refund_cents": gross_cents,
        "net_cents": 0,
        "order_count": len(candidates),
    }


def _construct_negative_net_region(
    orders: list[OrderRow], refunds: list[RefundRow]
) -> dict[str, Any]:
    """边界 6：华南 2026-03 的已批准退款大于毛销售额，净额为负。

    做法：
        1. 删除华南地区所有 ``refund_date`` 落在 ``2026-03`` 的退款，避免叠加；
        2. 补一条 ``approved`` 退款，金额 = 该地区当月毛销售额 + ``NEGATIVE_BUFFER_CENTS``，
           于是净额恰为 ``-NEGATIVE_BUFFER_CENTS``（-500.00 元）。

    说明：
        单笔退款金额可能大于它挂靠的那笔订单金额，这是刻意构造的教学场景
        （模拟“上月订单在本月批量退款”），规格 4.9 明确允许净额为负。

    参数：
        orders：订单列表（只读）。
        refunds：退款列表（就地增删）。

    返回：
        该地区该月的毛销售额、退款额、净额与订单数等事实。
    """
    index = _order_index(orders)
    refunds[:] = [
        refund
        for refund in refunds
        if not (
            refund.refund_date[:7] == NEGATIVE_MONTH
            and index[refund.order_id].region_id == NEGATIVE_REGION_ID
        )
    ]
    candidates = _orders_in_month(orders, NEGATIVE_MONTH, NEGATIVE_REGION_ID)
    if not candidates:
        raise RuntimeError("边界 6 构造失败：华南 2026-03 没有订单，无法构造负净额")
    gross_cents = sum(order.total_cents for order in candidates)
    biggest = max(candidates, key=lambda order: (order.total_cents, order.order_id))
    refund_cents = gross_cents + NEGATIVE_BUFFER_CENTS
    refunds.append(
        RefundRow(
            refund_id=f"RF_NEG_{biggest.order_id}",
            order_id=biggest.order_id,
            refund_date=biggest.order_date,
            amount_cents=refund_cents,
            reason="批量质量问题退款",
            status="approved",
        )
    )
    return {
        "region_id": NEGATIVE_REGION_ID,
        "month": NEGATIVE_MONTH,
        "gross_cents": gross_cents,
        "refund_cents": refund_cents,
        "net_cents": -NEGATIVE_BUFFER_CENTS,
        "order_count": len(candidates),
    }


# ---------------------------------------------------------------------------
# 自查与统计摘要（用原生 SQL，不依赖 queries.py，避免“自己验证自己”）
# ---------------------------------------------------------------------------


def _count_orders(
    conn: sqlite3.Connection, month: str, region_id: str | None = None
) -> int:
    """统计某月（可选某地区）的订单笔数。"""
    if region_id is None:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM orders WHERE substr(order_date, 1, 7) = ?",
            (month,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM orders "
            "WHERE substr(order_date, 1, 7) = ? AND region_id = ?",
            (month, region_id),
        ).fetchone()
    return int(row["n"])


def _gross_cents(
    conn: sqlite3.Connection, month: str, region_id: str | None = None
) -> int:
    """统计某月（可选某地区）的毛销售额（分）。"""
    sql = """
    SELECT COALESCE(SUM(quantity * unit_price_cents), 0) AS total
    FROM orders
    WHERE substr(order_date, 1, 7) = ?
    """
    params: tuple[Any, ...] = (month,)
    if region_id is not None:
        sql += " AND region_id = ?"
        params = (month, region_id)
    return int(conn.execute(sql, params).fetchone()["total"])


def _approved_refund_cents(
    conn: sqlite3.Connection, month: str, region_id: str | None = None
) -> int:
    """统计某月（可选某地区）的已批准退款额（分），按退款发生月归属。"""
    sql = """
    SELECT COALESCE(SUM(refunds.amount_cents), 0) AS total
    FROM refunds
    JOIN orders ON orders.order_id = refunds.order_id
    WHERE refunds.status = 'approved'
      AND substr(refunds.refund_date, 1, 7) = ?
    """
    params: tuple[Any, ...] = (month,)
    if region_id is not None:
        sql += " AND orders.region_id = ?"
        params = (month, region_id)
    return int(conn.execute(sql, params).fetchone()["total"])


def _refund_record_count(
    conn: sqlite3.Connection,
    month: str,
    region_id: str | None = None,
    status: str | None = None,
) -> int:
    """统计某月（可选地区、可选状态）的退款记录笔数。"""
    sql = """
    SELECT COUNT(*) AS n
    FROM refunds
    JOIN orders ON orders.order_id = refunds.order_id
    WHERE substr(refunds.refund_date, 1, 7) = ?
    """
    params: list[Any] = [month]
    if region_id is not None:
        sql += " AND orders.region_id = ?"
        params.append(region_id)
    if status is not None:
        sql += " AND refunds.status = ?"
        params.append(status)
    return int(conn.execute(sql, tuple(params)).fetchone()["n"])


def _verify_edge_cases(conn: sqlite3.Connection, edge_facts: dict[str, Any]) -> None:
    """用 SQL 复核 8 类边界事实是否真的写进库；任何一条不满足就抛 ``RuntimeError``。

    参数：
        conn：已写入数据的连接。
        edge_facts：各 ``_construct_*`` 函数返回的事实（仅用于错误信息）。

    异常：
        RuntimeError：边界事实不成立（说明生成逻辑被改坏，标准答案会失真）。
    """
    if _count_orders(conn, EMPTY_MONTH) != 0:
        raise RuntimeError(f"边界 1 失败：{EMPTY_MONTH} 不应有订单")
    if _refund_record_count(conn, EMPTY_MONTH) != 0:
        raise RuntimeError(f"边界 1 失败：{EMPTY_MONTH} 不应有退款记录")

    for region_id, month in EMPTY_REGION_MONTHS:
        if _count_orders(conn, month, region_id) != 0:
            raise RuntimeError(f"边界 2 失败：{region_id} 在 {month} 不应有订单")

    true_zero_gross = _gross_cents(conn, TRUE_ZERO_MONTH, TRUE_ZERO_REGION_ID)
    true_zero_refund = _approved_refund_cents(conn, TRUE_ZERO_MONTH, TRUE_ZERO_REGION_ID)
    if true_zero_gross <= 0 or true_zero_gross != true_zero_refund:
        raise RuntimeError(
            "边界 3 失败：西南 2026-09 真零不成立，"
            f"毛销售额 {true_zero_gross} 分，已批准退款 {true_zero_refund} 分"
        )

    pending_count = _refund_record_count(
        conn, PENDING_ONLY_MONTH, PENDING_ONLY_REGION_ID, status="pending"
    )
    approved_cents = _approved_refund_cents(conn, PENDING_ONLY_MONTH, PENDING_ONLY_REGION_ID)
    if pending_count < 2 or approved_cents != 0:
        raise RuntimeError(
            "边界 4 失败：华北 2026-07 应只有 pending 退款，"
            f"pending {pending_count} 笔、approved 金额 {approved_cents} 分"
        )

    cross_row = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM refunds
        JOIN orders ON orders.order_id = refunds.order_id
        WHERE refunds.status = 'approved'
          AND substr(orders.order_date, 1, 7) = ?
          AND substr(refunds.refund_date, 1, 7) = ?
        """,
        (CROSS_MONTH_ORDER_MONTH, CROSS_MONTH_REFUND_MONTH),
    ).fetchone()
    if int(cross_row["n"]) < CROSS_MONTH_TARGET_COUNT:
        raise RuntimeError(
            "边界 5 失败：跨月退款不足 "
            f"{CROSS_MONTH_TARGET_COUNT} 笔（实际 {int(cross_row['n'])} 笔）"
        )
    pinned_row = conn.execute(
        "SELECT amount_cents FROM refunds WHERE refund_id = ?", (PINNED_REFUND_ID,)
    ).fetchone()
    if pinned_row is None or int(pinned_row["amount_cents"]) != PINNED_CROSS_MONTH_REFUND_CENTS:
        raise RuntimeError(
            f"边界 5 失败：固定值退款 {PINNED_REFUND_ID} 应存在且金额为 "
            f"{PINNED_CROSS_MONTH_REFUND_CENTS} 分"
        )

    negative_found = False
    for region_id, _region_name, _manager in REGIONS:
        gross_cents = _gross_cents(conn, NEGATIVE_MONTH, region_id)
        refund_cents = _approved_refund_cents(conn, NEGATIVE_MONTH, region_id)
        if gross_cents - refund_cents < 0:
            negative_found = True
            break
    if not negative_found:
        raise RuntimeError(f"边界 6 失败：{NEGATIVE_MONTH} 应至少有一个地区净额为负")

    product_row = conn.execute(
        "SELECT COUNT(*) AS n FROM orders "
        "WHERE product_id = ? AND substr(order_date, 1, 7) = ?",
        (PRODUCT_ZERO_ID, PRODUCT_ZERO_MONTH),
    ).fetchone()
    if int(product_row["n"]) != 0:
        raise RuntimeError(
            f"边界 7 失败：商品 {PRODUCT_ZERO_ID} 在 {PRODUCT_ZERO_MONTH} 不应有订单"
        )

    channel_rows = conn.execute(
        "SELECT DISTINCT channel FROM orders ORDER BY channel"
    ).fetchall()
    channels = {str(row["channel"]) for row in channel_rows}
    if not set(CHANNELS).issubset(channels):
        raise RuntimeError(f"边界 8 失败：channel 取值不完整，实际为 {sorted(channels)}")

    if edge_facts["true_zero"]["net_cents"] != 0:
        raise RuntimeError("边界 3 失败：真零事实的 net_cents 不是 0")


def _summarize(
    conn: sqlite3.Connection,
    db_path: Path,
    seed: int,
    edge_facts: dict[str, Any],
) -> dict[str, Any]:
    """生成 ``generate_database`` 的统计摘要（纯 SQL 聚合，金额单位分）。"""
    order_count = int(
        conn.execute("SELECT COUNT(*) AS n FROM orders").fetchone()["n"]
    )
    refund_count = int(
        conn.execute("SELECT COUNT(*) AS n FROM refunds").fetchone()["n"]
    )
    gross_total = int(
        conn.execute(
            "SELECT COALESCE(SUM(quantity * unit_price_cents), 0) AS total FROM orders"
        ).fetchone()["total"]
    )
    status_rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM refunds GROUP BY status ORDER BY status"
    ).fetchall()
    months = _iter_months(DATA_FIRST_MONTH, DATA_LAST_MONTH)
    return {
        "seed": seed,
        "db_file": db_path.name,
        "data_range": {"first": DATA_FIRST_MONTH, "last": DATA_LAST_MONTH},
        "months": months,
        "region_count": len(REGIONS),
        "product_count": len(PRODUCTS),
        "order_count": order_count,
        "refund_count": refund_count,
        "refund_status_counts": {str(row["status"]): int(row["n"]) for row in status_rows},
        "gross_cents_total": gross_total,
        "channels": list(CHANNELS),
        "edge_cases": edge_facts,
    }


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def generate_database(db_path: Path, seed: int = SEED) -> dict[str, Any]:
    """生成完整演示数据库，并返回统计摘要与 8 类边界事实的取值。

    参数：
        db_path：目标 SQLite 文件路径；已存在时会被删除重建（生成物不入库）。
        seed：随机种子，默认 :data:`SEED`（20260901）。

    返回：
        字典，包含 ``seed`` / ``months`` / ``order_count`` / ``refund_count`` /
        ``refund_status_counts`` / ``edge_cases`` 等键。``edge_cases`` 里的数值
        就是标准答案与测试可以直接断言的边界事实。

    异常：
        RuntimeError：边界事实复核失败时抛出（宁可生成失败，也不产出错误标准答案）。

    副作用：
        删除并重建 ``db_path`` 指向的数据库文件；这是本模块唯一的写文件入口。
    """
    target = Path(db_path)
    if target.exists():
        target.unlink()

    months = _iter_months(DATA_FIRST_MONTH, DATA_LAST_MONTH)
    rng = random.Random(seed)

    orders = _build_orders(rng, months)
    refunds = _build_refunds(rng, orders)

    # 边界构造：彼此使用的「地区 + 月份」互不重叠，顺序不影响结果。
    cross_month_facts = _construct_cross_month_refunds(orders, refunds)
    pending_only_facts = _construct_pending_only_refunds(orders, refunds)
    true_zero_facts = _construct_true_zero_region(orders, refunds)
    negative_facts = _construct_negative_net_region(orders, refunds)

    edge_facts: dict[str, Any] = {
        "empty_month": {
            "month": EMPTY_MONTH,
            "order_count": 0,
            "has_orders": False,
        },
        "empty_region_months": {
            "pairs": [
                {"region_id": region_id, "month": month}
                for region_id, month in EMPTY_REGION_MONTHS
            ]
        },
        "product_zero_month": {
            "product_id": PRODUCT_ZERO_ID,
            "month": PRODUCT_ZERO_MONTH,
            "order_count": 0,
        },
        "true_zero": true_zero_facts,
        "pending_only": pending_only_facts,
        "cross_month": cross_month_facts,
        "negative_net": negative_facts,
        "channel_values": list(CHANNELS),
    }

    conn = connect(target)
    try:
        init_schema(conn)
        conn.executemany(
            "INSERT INTO regions (region_id, region_name, manager) VALUES (?, ?, ?)",
            list(REGIONS),
        )
        conn.executemany(
            "INSERT INTO products (product_id, product_name, category, unit_price_cents) "
            "VALUES (?, ?, ?, ?)",
            list(PRODUCTS),
        )
        conn.executemany(
            "INSERT INTO orders (order_id, order_date, region_id, product_id, quantity, "
            "unit_price_cents, channel) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    order.order_id,
                    order.order_date,
                    order.region_id,
                    order.product_id,
                    order.quantity,
                    order.unit_price_cents,
                    order.channel,
                )
                for order in orders
            ],
        )
        conn.executemany(
            "INSERT INTO refunds (refund_id, order_id, refund_date, amount_cents, reason, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    refund.refund_id,
                    refund.order_id,
                    refund.refund_date,
                    refund.amount_cents,
                    refund.reason,
                    refund.status,
                )
                for refund in refunds
            ],
        )
        conn.commit()

        _verify_edge_cases(conn, edge_facts)
        summary = _summarize(conn, target, seed, edge_facts)
    finally:
        conn.close()
    return summary


def main(argv: list[str] | None = None) -> int:
    """命令行入口：``python -m ops_assistant.data.generate --db data/ops.db``。

    参数：
        argv：命令行参数（``None`` 表示读取 ``sys.argv``）。

    返回：
        进程退出码：0 成功，1 失败。
    """
    default_db_path = Path(__file__).resolve().parents[3] / "data" / "ops.db"
    parser = argparse.ArgumentParser(description="按固定种子生成 ops-assistant 演示数据库")
    parser.add_argument("--db", type=Path, default=default_db_path, help="目标数据库文件路径")
    parser.add_argument("--seed", type=int, default=SEED, help="随机种子，默认 20260901")
    args = parser.parse_args(argv)

    try:
        summary = generate_database(args.db, seed=args.seed)
    except (RuntimeError, sqlite3.Error) as error:
        print(f"[失败] 数据生成失败：{error}")
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
