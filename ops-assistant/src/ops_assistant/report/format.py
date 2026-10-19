"""报告渲染：把程序计算结果按固定 8 段结构渲染成 Markdown。

本模块只做"格式化"，不做任何业务计算，也不产生任何新数字：
报告里的每个数字都必须由上游 ``workflow.compute`` 节点写入 ``calculation``。
纯模板 + 程序注入，因此没有模型也不会失败；模型润色只能进入"局限与未验证"。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

# 指标与维度的中文标签（只做展示，不参与计算）
METRIC_LABELS = {
    "net_sales": "净销售额",
    "gross_sales": "毛销售额",
    "refunds": "退款额",
    "net": "净销售额",
}

DIMENSION_LABELS = {
    "region": "地区",
    "product": "商品",
    "month": "月份",
    "channel": "渠道（不支持）",
}

MISSING_TEXT = "（无数据）"
SUPPORTED_DIMENSIONS = ("region", "product", "month")


def format_cents(cents: int | None, unit: str = "元") -> str:
    """把"分"为单位的整数金额格式化为展示字符串。

    参数：
        cents: 金额（分），必须是整数；None 表示该数字缺失。
        unit: 展示单位，仅支持 "元" 与 "万元"。

    返回：
        str：带千分位、保留两位小数的金额；缺失时返回"（无数据）"。

    说明：
        内部用 Decimal 做减法，避免 float 在"分→元"换算时出现 0.1+0.2 这类误差。
    """
    if cents is None:
        return MISSING_TEXT
    if unit not in {"元", "万元"}:
        raise ValueError(f"不支持的金额单位：{unit}（只支持 元 / 万元）")
    divisor = Decimal(100) if unit == "元" else Decimal(1_000_000)
    value = Decimal(int(cents)) / divisor
    return f"{value:,.2f}"


def format_pct(pct: float | None, *, base_zero: bool = False) -> str:
    """格式化变化率。

    参数：
        pct: 比率（0.25 表示 25%），由程序按"差额 / |上期|"算出。
        base_zero: 基期是否为 0；为 True 时变化率无定义。

    返回：
        str：形如 "12.34%"；无定义时返回带原因的中文说明，绝不写 ∞ 或 100%。
    """
    if base_zero:
        return "无定义（基期为 0）"
    if pct is None:
        return "无定义（缺少可比较的基期数据）"
    return f"{float(pct) * 100:.2f}%"


def format_int(value: int | None) -> str:
    """格式化整数（带千分位）；None 返回"（无数据）"。"""
    if value is None:
        return MISSING_TEXT
    return f"{int(value):,}"


def format_share(share: float | None) -> str:
    """格式化"占总差额比例"；总差额为 0 时无定义。"""
    if share is None:
        return "无定义（总差额为 0）"
    return f"{float(share) * 100:.2f}%"


def markdown_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """把二维数据渲染成 Markdown 表格。

    参数：
        headers: 表头。
        rows: 每行单元格；单元格中的 "|" 会被转义，换行会被替换为空格。

    返回：
        str：Markdown 表格文本（末尾不含多余空行）。
    """

    def _cell(value: Any) -> str:
        text = "" if value is None else str(value)
        return text.replace("|", "\\|").replace("\n", " ").strip()

    lines = ["| " + " | ".join(_cell(h) for h in headers) + " |"]
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        lines.append("| " + " | ".join(_cell(v) for v in row) + " |")
    return "\n".join(lines)


def _metric_label(metric: str | None) -> str:
    """返回指标中文名。"""
    return METRIC_LABELS.get(str(metric or "net_sales"), "净销售额")


def _dimension_label(dimension: str | None) -> str:
    """返回维度中文名。"""
    return DIMENSION_LABELS.get(str(dimension or "region"), "地区")


def _as_dict(value: Any) -> dict[str, Any]:
    """把 dataclass / dict / None 统一成 dict，缺失时返回空 dict。"""
    if isinstance(value, Mapping):
        return dict(value)
    if value is None:
        return {}
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return {}


def _money(value: Any, unit: str) -> str:
    """金额展示：value 为"分"整数或 None。"""
    if value is None:
        return MISSING_TEXT
    try:
        return format_cents(int(value), unit)
    except (TypeError, ValueError):
        return MISSING_TEXT


def _metric_cents(calculation: Mapping[str, Any], key: str) -> int | None:
    """从 calculation 里读取一个"分"为单位的指标值（缺失返回 None）。"""
    value = calculation.get(key)
    return None if value is None else int(value)


def render_analysis_report(
    *,
    calculation: Mapping[str, Any],
    policy_context: str = "",
    sources: Sequence[Mapping[str, Any]] | None = None,
    prefs: Mapping[str, Any] | None = None,
    question: str = "",
    notes: Sequence[str] | None = None,
) -> str:
    """渲染完整分析报告（固定 8 段结构）。

    参数：
        calculation: ``compute`` 节点产出的计算结果字典（唯一数字来源）。
        policy_context: 知识库口径/政策文本（可含 [doc_id#chunk_id] 引用）。
        sources: 来源列表（doc / sql 两类），写入第 8 段。
        prefs: 用户偏好（读 amount_unit）。
        question: 本轮原始问题，写在报告抬头。
        notes: 附加说明（含模型润色文本、计算层备注），只进"局限与未验证"段。

    返回：
        str：Markdown 报告全文，严格按 结论 / 关键数字 / 地区差额分解 /
        口径与依据 / 数据边界 / 计算过程 / 局限与未验证 / 来源 八段输出。
    """
    calc = dict(calculation or {})
    pref_map = dict(prefs or {})
    unit = str(calc.get("unit") or pref_map.get("amount_unit") or "元")
    if unit not in {"元", "万元"}:
        unit = "元"

    metric = str(calc.get("metric") or "net_sales")
    metric_label = _metric_label(metric)
    dimension = str(calc.get("dimension") or "region")
    dimension_label = _dimension_label(dimension)

    current_month = str(calc.get("current_month") or "（未知）")
    previous_month = str(calc.get("previous_month") or "（未知）")
    current = _as_dict(calc.get("current"))
    previous = _as_dict(calc.get("previous"))
    edge = _as_dict(calc.get("edge_flags"))

    metric_current = _metric_cents(calc, "metric_current_cents")
    metric_previous = _metric_cents(calc, "metric_previous_cents")
    metric_delta = calc.get("metric_delta_cents")
    metric_delta = None if metric_delta is None else int(metric_delta)
    metric_pct = calc.get("metric_pct")
    base_zero = bool(calc.get("metric_base_zero", False))

    net_delta = calc.get("delta_net_cents")
    net_delta = None if net_delta is None else int(net_delta)
    region_sum = calc.get("region_delta_sum_cents")
    region_sum = None if region_sum is None else int(region_sum)

    regions_filter = [str(r) for r in (calc.get("regions_filter") or [])]
    region_deltas = [dict(item) for item in (calc.get("region_deltas") or []) if isinstance(item, Mapping)]
    unsupported = str(calc.get("unsupported_dimension") or "").strip()

    lines: list[str] = []

    # ---------------- 抬头 ----------------
    lines.append("# 经营数据分析报告")
    lines.append("")
    lines.append(
        "> 本报告由程序按固定模板渲染：所有数字均由程序计算后注入，"
        "模板与任何模型都不产生数字，也不对原因做推断。"
    )
    lines.append("")
    lines.append(f"- 问题：{question.strip() or '（未记录）'}")
    lines.append(f"- 时间范围：{current_month}（本期）对比 {previous_month}（上期）")
    lines.append(f"- 指标：{metric_label}；分析维度：{dimension_label}；金额展示单位：{unit}")
    if regions_filter:
        lines.append(f"- 地区过滤：{'、'.join(regions_filter)}（汇总数字已按过滤后重算）")
    lines.append("")

    # ---------------- 1. 结论 ----------------
    lines.append("## 结论")
    lines.append("")
    lines.append(f"- 本期（{current_month}）{metric_label}为 {_money(metric_current, unit)}。")
    lines.append(f"- 上期（{previous_month}）{metric_label}为 {_money(metric_previous, unit)}。")
    lines.append(f"- 差额（本期−上期）为 {_money(metric_delta, unit)}。")
    lines.append(f"- 变化率为 {format_pct(metric_pct, base_zero=base_zero)}（口径：差额 / |上期|）。")
    if net_delta is not None and region_sum is not None and not unsupported:
        agree = "一致" if bool(edge.get("delta_sum_matches", region_sum == net_delta)) else "不一致"
        lines.append(
            f"- 地区差额分解校验：各地区差额合计 {_money(region_sum, unit)}，"
            f"总差额 {_money(net_delta, unit)}，校验结果{agree}。"
        )
    if unsupported:
        lines.append(f"- 说明：维度 `{unsupported}` 不在本项目分析范围内，本轮未做该维度分解。")
    lines.append("- 本段只陈述数据事实；无记录、真零、零基数、负净额等情况在“数据边界”逐条标注。")
    lines.append("")

    # ---------------- 2. 关键数字 ----------------
    lines.append("## 关键数字")
    lines.append("")
    headers = [
        "月份",
        "毛销售额",
        "退款额（仅 approved）",
        "净销售额",
        "订单数",
        "已批准退款笔数",
        "待处理退款笔数",
    ]
    rows: list[list[Any]] = []
    for month, label, sales in (
        (previous_month, "上期", previous),
        (current_month, "本期", current),
    ):
        if not sales:
            rows.append(
                [
                    f"{month}（{label}）",
                    MISSING_TEXT,
                    MISSING_TEXT,
                    MISSING_TEXT,
                    MISSING_TEXT,
                    MISSING_TEXT,
                    MISSING_TEXT,
                ]
            )
            continue
        rows.append(
            [
                f"{month}（{label}）",
                _money(sales.get("gross_cents"), unit),
                _money(sales.get("refund_cents"), unit),
                _money(sales.get("net_cents"), unit),
                format_int(sales.get("order_count")),
                format_int(sales.get("approved_refund_count")),
                format_int(sales.get("pending_refund_count")),
            ]
        )
    lines.append(markdown_table(headers, rows))
    lines.append("")
    lines.append(f"表内金额单位为{unit}；数据来自本地 SQLite 的订单与退款表（见“来源”段）。")
    lines.append("")

    # ---------------- 3. 地区差额分解 ----------------
    # 标题固定为"地区差额分解"（规格 5.9 的固定结构）；维度不是地区时在段内说明。
    lines.append("## 地区差额分解")
    lines.append("")
    if dimension != "region":
        lines.append(f"- 本轮分析维度为{dimension_label}，下表按该维度展示差额分解（小计口径同地区）。")
        lines.append("")
    if unsupported or dimension not in SUPPORTED_DIMENSIONS or not region_deltas:
        lines.append("- 本轮没有可展示的维度分解：未指定支持的分析维度，或没有可用记录。")
        if dimension == "region" and not region_deltas:
            lines.append("- 若问题只要求总量对比，可忽略本段；需要分解时请明确“按地区分解”。")
    else:
        delta_headers = [dimension_label, "本期净额", "上期净额", "差额（本期−上期）", "占总差额"]
        delta_rows: list[list[Any]] = []
        sum_current = 0
        sum_previous = 0
        sum_delta = 0
        for item in region_deltas:
            cur = item.get("current_net_cents")
            prev = item.get("previous_net_cents")
            delta = item.get("delta_cents")
            if cur is not None:
                sum_current += int(cur)
            if prev is not None:
                sum_previous += int(prev)
            if delta is not None:
                sum_delta += int(delta)
            name = str(item.get("region_name") or item.get("region_id") or "（未知）")
            region_id = str(item.get("region_id") or "")
            label = f"{name}（{region_id}）" if region_id and region_id != name else name
            delta_rows.append(
                [
                    label,
                    _money(cur, unit),
                    _money(prev, unit),
                    _money(delta, unit),
                    format_share(item.get("share_of_delta")),
                ]
            )
        delta_rows.append(
            [
                "合计（校验）",
                _money(sum_current, unit),
                _money(sum_previous, unit),
                _money(sum_delta, unit),
                "无定义（总差额为 0）" if sum_delta == 0 else "100.00%",
            ]
        )
        lines.append(markdown_table(delta_headers, delta_rows))
        lines.append("")
        if net_delta is not None:
            agree = "一致" if sum_delta == net_delta else "不一致"
            lines.append(
                f"校验：各地区差额合计 {_money(sum_delta, unit)} 与总差额 "
                f"{_money(net_delta, unit)} {agree}（分解只改变展示方式，不改变总量）。"
            )
            lines.append("")
        lines.append("- 各地区金额均为净销售额（毛销售额 − 已批准退款额），正负号表示相对上期的增减。")
        lines.append("")

    # ---------------- 4. 口径与依据 ----------------
    lines.append("## 口径与依据")
    lines.append("")
    lines.append("- 净销售额 = 毛销售额 − 已批准退款额；金额在数据库内以“分”为整数存储，展示时换算。")
    lines.append(
        "- 退款按退款发生月归属（`refunds.refund_date`），不按原订单所在月；`pending` 与 `rejected` 不计入退款额。"
    )
    lines.append("- 毛销售额 = Σ(quantity × orders.unit_price_cents)，使用订单成交单价，不使用商品目录价。")
    lines.append("- 变化率分母为前一期的同一指标；分母为 0 时无定义，不用 ∞ 或 100% 代替。")
    lines.append(f"- 当前分析维度：{dimension_label}；本项目只支持 region / product / month，`channel` 不参与分析。")
    if metric == "gross_sales":
        lines.append("- 本轮结论指标为毛销售额：不扣退款，退款情况见关键数字表。")
    elif metric == "refunds":
        lines.append("- 本轮结论指标为退款额：只统计已批准退款，待处理退款不计入。")
    lines.append("")
    context = str(policy_context or "").strip()
    if context:
        lines.append("### 知识库依据")
        lines.append("")
        lines.append(context)
        lines.append("")
        lines.append("> 引用格式为 `[doc_id#chunk_id]`，对应第 8 段“来源”中的片段。")
    else:
        lines.append("### 知识库依据")
        lines.append("")
        lines.append("- 本轮未检索到可用资料，因此报告不包含任何知识库结论；口径以上述通用定义为准。")
    lines.append("")

    # ---------------- 5. 数据边界 ----------------
    lines.append("## 数据边界")
    lines.append("")
    boundary = _boundary_bullets(
        edge=edge,
        current_month=current_month,
        previous_month=previous_month,
        current=current,
        previous=previous,
        unsupported=unsupported,
        regions_filter=regions_filter,
    )
    lines.extend(boundary)
    lines.append("")

    # ---------------- 6. 计算过程 ----------------
    lines.append("## 计算过程")
    lines.append("")
    gross_current = current.get("gross_cents")
    refund_current = current.get("refund_cents")
    net_current = current.get("net_cents")
    gross_previous = previous.get("gross_cents")
    net_previous = previous.get("net_cents")
    lines.append("公式（金额全程以“分”为整数参与计算，展示时才换算）：")
    lines.append("")
    lines.append(
        f"- 本期毛销售额：Σ(quantity × unit_price_cents) = {format_int(gross_current)} 分"
        f"（= {_money(gross_current, unit)}）"
    )
    lines.append(
        f"- 本期退款额：Σ(amount_cents)，条件 status='approved' 且 refund_date 属于 {current_month} = "
        f"{format_int(refund_current)} 分（= {_money(refund_current, unit)}）"
    )
    lines.append(
        f"- 本期净销售额：{format_int(gross_current)} − {format_int(refund_current)} = "
        f"{format_int(net_current)} 分（= {_money(net_current, unit)}）"
    )
    lines.append(
        f"- 上期净销售额：{format_int(gross_previous)} − {format_int(previous.get('refund_cents'))} = "
        f"{format_int(net_previous)} 分（= {_money(net_previous, unit)}）"
    )
    lines.append(
        f"- 差额：{format_int(net_current)} − {format_int(net_previous)} = "
        f"{format_int(net_delta)} 分（= {_money(net_delta, unit)}）"
    )
    lines.append(
        f"- 变化率：{format_int(net_delta)} / |{format_int(net_previous)}| = "
        f"{format_pct(calc.get('pct_change'), base_zero=bool(calc.get('base_zero')))}"
    )
    if metric != "net_sales":
        lines.append(
            f"- 结论指标为{metric_label}，其差额为 {format_int(metric_delta)} 分"
            f"（{_money(metric_current, unit)} − {_money(metric_previous, unit)}）。"
        )
    if region_sum is not None:
        lines.append(
            f"- 维度分解校验：Σ 各地区差额 = {format_int(region_sum)} 分，总差额 = {format_int(net_delta)} 分。"
        )
    lines.append("- 上述数字全部来自 data 层查询与 metrics 层计算函数的返回值，模板不做任何算术。")
    lines.append("")

    # ---------------- 7. 局限与未验证 ----------------
    lines.append("## 局限与未验证")
    lines.append("")
    lines.append("- 本报告不做因果归因：只描述数字的差异与分布，不解释原因，也不给出经营建议。")
    lines.append("- 数据来自本地 SQLite 演示数据集（固定随机种子生成），不代表真实经营事实。")
    lines.append("- 会话级偏好、知识库检索与模型润色都可能影响措辞，但不会影响任何数字。")
    lines.append("- 本轮未做并发压测、未连接真实生产数据库、未做人工事实核查与模型评估。")
    lines.append("- 报告导出为单一 Markdown 文件；文件名由 thread_id 决定，重复导出覆盖同一路径。")
    for note in (notes or []):
        text = str(note).strip()
        if not text:
            continue
        lines.append(f"- {text}")
    lines.append("")

    # ---------------- 8. 来源 ----------------
    lines.append("## 来源")
    lines.append("")
    lines.extend(_sources_section(list(sources or [])))
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _boundary_bullets(
    *,
    edge: Mapping[str, Any],
    current_month: str,
    previous_month: str,
    current: Mapping[str, Any],
    previous: Mapping[str, Any],
    unsupported: str,
    regions_filter: Sequence[str],
) -> list[str]:
    """生成"数据边界"段的无序列表，逐条解释无记录 / 真零 / 零基数 / 负净额。

    参数：
        edge: 边界标记字典（由 compute 节点程序计算，全部是布尔或名称列表）。
        current_month: 本期月份。
        previous_month: 上期月份。
        current: 本期销量字典。
        previous: 上期销量字典。
        unsupported: 不支持的维度名（空串表示没有）。
        regions_filter: 地区过滤列表。

    返回：
        list[str]：可直接拼进 Markdown 的条目。
    """
    bullets: list[str] = []
    order_count_current = current.get("order_count")
    order_count_previous = previous.get("order_count")

    if edge.get("current_no_records"):
        bullets.append(
            f"- 无记录：{current_month} 没有任何订单（has_orders=False），"
            "因此不写“销售额为 0”，上表相关金额只代表无数据。"
        )
    elif edge.get("true_zero"):
        bullets.append(
            f"- 真零：{current_month} 有 {format_int(order_count_current)} 笔订单，"
            "但退款冲抵后净销售额为 0（是真零，不是无记录）。"
        )
    if edge.get("previous_no_records"):
        bullets.append(
            f"- 无记录：上期 {previous_month} 没有任何订单，基期金额按 0 参与计算，"
            "但这不代表真实销售额为 0。"
        )
    if edge.get("base_zero"):
        bullets.append("- 零基数：上期同一指标为 0，变化率无定义；建议改看差额，或把比较区间拉长。")
    if edge.get("negative_net"):
        bullets.append(
            "- 负净额：本期或上期存在“退款额 > 毛销售额”的情况，净销售额为负是公式的正常结果；"
            "本报告不对其成因做推断。"
        )
    if edge.get("pending_refunds"):
        bullets.append(
            f"- 待处理退款：本期存在 {format_int(current.get('pending_refund_count'))} 笔待处理或已拒绝的退款记录，"
            "按口径不计入退款额。"
        )
    if edge.get("any_refund_records") is False:
        bullets.append("- 本期没有任何退款记录（has_any_refund_records=False），退款额按 0 参与计算。")
    if not edge.get("current_has_orders") and not edge.get("current_no_records"):
        bullets.append(f"- 本期（{current_month}）订单标记缺失，金额按查询结果展示。")
    if previous and order_count_previous is None:
        bullets.append("- 上期订单数缺失，仅展示金额相关字段。")

    for name in (edge.get("zero_order_regions_current") or []):
        bullets.append(f"- 地区无记录：{name} 在本期没有订单（不是“销售额为 0”）。")
    for name in (edge.get("zero_order_regions_previous") or []):
        bullets.append(f"- 地区无记录：{name} 在上期没有订单。")
    if regions_filter:
        bullets.append(f"- 地区过滤：本报告只统计 {'、'.join(regions_filter)}，总量已按过滤后重算。")
    if unsupported:
        bullets.append(
            f"- 不支持维度：`{unsupported}` 不在当前分析工具范围内（仅支持 region/product/month），"
            "程序没有静默忽略，而是显式标注。"
        )
    if edge.get("delta_sum_matches") is False:
        bullets.append(
            "- 校验不一致：各地区差额合计与总差额不相等，可能存在未被分解的地区或过滤差异，请以明细为准。"
        )

    if not bullets:
        bullets.append("- 本期数据未触发无记录 / 真零 / 零基数 / 负净额等边界情况，数字可直接按金额口径理解。")
    return bullets


def _sources_section(sources: Sequence[Mapping[str, Any]]) -> list[str]:
    """生成"来源"段：SQL（含参数）与知识库片段（doc_id#chunk_id）。

    参数：
        sources: compute / knowledge / data 节点写入的来源列表。

    返回：
        list[str]：Markdown 行。
    """
    sql_blocks = [dict(s) for s in sources if isinstance(s, Mapping) and s.get("type") == "sql"]
    doc_blocks = [dict(s) for s in sources if isinstance(s, Mapping) and s.get("type") == "doc"]
    lines: list[str] = []

    lines.append("### SQL 与参数")
    lines.append("")
    if sql_blocks:
        for item in sql_blocks:
            lines.append("```sql")
            lines.append(str(item.get("sql") or "").strip())
            lines.append("```")
            params = item.get("params") or {}
            param_text = ", ".join(f"{key}={value!r}" for key, value in dict(params).items()) or "（无参数）"
            lines.append(f"- 参数：{param_text}")
            note = str(item.get("note") or "").strip()
            if note:
                lines.append(f"- 说明：{note}")
            lines.append("")
    else:
        lines.append("- 本轮没有可展示的 SQL 来源。")
        lines.append("")

    lines.append("### 知识库片段")
    lines.append("")
    if doc_blocks:
        for item in doc_blocks:
            doc_id = str(item.get("doc_id") or "").strip()
            chunk_id = str(item.get("chunk_id") or "").strip()
            title = str(item.get("title") or "").strip()
            snippet = str(item.get("snippet") or "").strip()
            ref = f"[{doc_id}#{chunk_id}]"
            suffix = f"《{title}》" if title else ""
            lines.append(f"- {ref}{suffix}：{snippet}")
    else:
        lines.append("- 本轮没有知识库引用（未检索或未命中）。")
    return lines
