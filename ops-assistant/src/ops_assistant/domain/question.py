"""问题解析：把自然语言问题转成确定的结构化规格。

设计原则：
1. 纯规则、可离线运行、结果可预测；不调用任何模型。
2. 解析失败不抛异常，而是返回 ``needs_clarification=True`` 和中文澄清话术。
3. 时间、地区、指标、维度分开识别，方便工作流按字段决定路由。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

# 中文月份词到数字的映射。只支持一到十二，超出范围视为非法月份。
_CN_MONTHS: dict[str, int] = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
    "十一": 11,
    "十二": 12,
}

# 形如 "2026 年九月"、"9月"、"09 月" 的期间表达。年份可选。
_PERIOD_RE = re.compile(
    r"(?:(?P<year>20\d{2})\s*年\s*)?"
    r"(?P<month>\d{1,2}|[一二三四五六七八九十]{1,3})\s*月"
)

# 单独出现的年份，用于"今年/去年"以外的兜底。
_YEAR_RE = re.compile(r"(?P<year>20\d{2})\s*年")

# "华中华地区" 这类未知地区词，用于给出可操作澄清。
_REGION_PHRASE_RE = re.compile(r"(?P<name>[\u4e00-\u9fa5]{2,4})(?:地区|大区|区域)")

# 明显不是地区名的前缀词，避免把"这个地区"当成未知地区。
_REGION_STOPWORDS = {"这个", "那个", "哪个", "各个", "每个", "按", "该", "分", "本", "全"}

# 判断"去年/今年"时向前回看的原文窗口（含空白，比较前会先剔除空白）。
_RELATIVE_PREFIX_WINDOW = 8

# "9 月各地区" 这类表达里，_REGION_PHRASE_RE 会把月份/量词字一起吃进候选词，
# 这些前导噪声要先剥掉，_REGION_STOPWORDS 的判断才有机会生效。
_REGION_NOISE_PREFIX_RE = re.compile(r"^[年月日号每各个这那哪几\s]+")

_COMPARISON_WORDS = ("比", "对比", "相比", "变化", "增加", "减少", "下降", "增长", "涨", "跌", "差额")
_CHART_WORDS = ("图表", "柱状", "趋势图", "可视化", "画个图", "画图")
_REFUND_DEFINITION_WORDS = ("是否", "扣", "算", "口径", "包含", "扣除", "计入")


@dataclass(frozen=True)
class QuestionSpec:
    """一个问题的结构化规格。

    字段含义见 ``工程规格.md`` 第 5.2 节。所有字段都是纯数据，可 JSON 序列化。
    """

    raw: str
    current_month: str | None
    previous_month: str | None
    regions: tuple[str, ...]
    dimension: str
    metric: str
    wants_comparison: bool
    asks_refund_definition: bool
    wants_chart: bool
    needs_clarification: bool
    clarification: str | None
    metric_explicit: bool = False
    region_action: str = "inherit"
    intent: str = "analysis"
    input_error: str | None = None
    time_error: str | None = None


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    """返回 (year, month) 偏移 delta 个月后的结果，兼容跨年。"""

    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _format_month(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _month_from_token(token: str) -> int | None:
    """把 "9" / "09" / "九" / "十二" 转成 1..12；非法返回 None。"""

    token = token.strip()
    if token.isdigit():
        value = int(token)
    else:
        value = _CN_MONTHS.get(token, 0)
    return value if 1 <= value <= 12 else None


def _month_label(month: str) -> str:
    year, mon = month.split("-")
    return f"{year} 年 {int(mon)} 月"


def _match_regions(text: str, known_regions: list[dict] | None) -> tuple[tuple[str, ...], str | None]:
    """返回命中的地区 ID 元组，以及（可选的）未知地区澄清话术。"""

    if not known_regions:
        return (), None

    # 名称可能包含 ID；按名称长度倒序匹配，避免短名称抢先。
    ordered = sorted(
        known_regions,
        key=lambda item: len(str(item.get("region_name", ""))),
        reverse=True,
    )
    found: list[str] = []
    for item in ordered:
        region_id = str(item.get("region_id", ""))
        region_name = str(item.get("region_name", ""))
        if not region_id:
            continue
        if (region_name and region_name in text) or region_id in text:
            if region_id not in found:
                found.append(region_id)
    # 即使已经命中一个地区，仍须校验其余明确提及的地区。
    if any(word in text for word in ("地区", "大区", "区域")):
        for match in _REGION_PHRASE_RE.finditer(text):
            candidate = re.split(r"[和与、]", match.group("name"))[-1]
            candidate = re.sub(r"^(?:那|按|看|只看|的)", "", candidate)
            # "9 月各地区" 的候选词是 "月各"，"9 月按地区分解" 是 "月按"：
            # 前导的时间/量词字会挡住 stopword 判断，先剥掉再比对。
            candidate = _REGION_NOISE_PREFIX_RE.sub("", candidate)
            if not candidate or candidate in _REGION_STOPWORDS or candidate in {"各", "每", "所有", "全国"}:
                continue
            if any(str(item.get("region_name", "")) in candidate for item in ordered):
                continue
            names = "、".join(str(item.get("region_name", "")) for item in known_regions)
            return tuple(found), f"未找到地区「{candidate}」。当前可用地区：{names}。"
    return tuple(found), None


def _resolve_year_context(text: str, today: date) -> tuple[int | None, str | None]:
    """从文本里找年份上下文：显式年份优先，其次“今年/去年”。"""

    explicit = _YEAR_RE.search(text)
    if explicit:
        return int(explicit.group("year")), None
    if "今年" in text:
        return today.year, None
    if "去年" in text:
        return today.year - 1, None
    return None, None


def _collect_mentions(
    text: str,
    today: date,
    year_context: int | None,
) -> tuple[list[tuple[int, int, int]], bool]:
    """收集期间提及，返回 ([(位置, 年, 月)], 是否出现非法月份)。

    年可能为 0，表示“缺年份”，由调用方决定是否澄清。
    """

    mentions: list[tuple[int, int, int]] = []
    invalid = False
    # 显式年份锚点只算一次；没有显式年份时以 today 为锚。
    anchor_match = _YEAR_RE.search(text)
    year_anchor = int(anchor_match.group("year")) if anchor_match else today.year
    for match in _PERIOD_RE.finditer(text):
        month = _month_from_token(match.group("month"))
        if month is None:
            invalid = True
            continue
        raw_year = match.group("year")
        # 前缀要先去掉空白再判断：“比去年 9 月”里的空格会让 endswith("去年") 失效，
        # 于是“去年同期”被解析成今年同月（本期=上期，差额恒为 0）。
        prefix = re.sub(r"\s+", "", text[max(0, match.start() - _RELATIVE_PREFIX_WINDOW) : match.start()])
        if prefix.endswith("去年"):
            relative_year: int | None = year_anchor - 1
        elif prefix.endswith("今年"):
            relative_year = today.year
        else:
            relative_year = None
        year = int(raw_year) if raw_year else (relative_year or year_context or 0)
        mentions.append((match.start(), year, month))

    # “上个月/上月/本月/这个月”不在上面的正则里，单独按位置收集。
    relative_phrases = (
        ("上个月", -1),
        ("上月", -1),
        ("本月", 0),
        ("这个月", 0),
    )
    for phrase, delta in relative_phrases:
        start = text.find(phrase)
        if start >= 0:
            # “9 月比上月”以显式月份为锚点；单独“上月”才相对 today。
            anchor = next((item for item in reversed(mentions) if item[0] < start and item[1]), None)
            year, month = _shift_month(anchor[1] if anchor else today.year, anchor[2] if anchor else today.month, delta)
            mentions.append((start, year, month))

    mentions.sort(key=lambda item: item[0])
    return mentions, invalid


def _detect_dimension(text: str) -> str:
    if "渠道" in text:
        return "channel"
    if any(word in text for word in ("按商品", "分商品", "商品明细", "按产品", "按品类", "品类", "产品明细")):
        return "product"
    if any(word in text for word in ("按月", "月度趋势", "趋势")):
        return "month"
    if any(
        word in text
        for word in (
            "按地区",
            "分地区",
            "地区分解",
            "地区明细",
            "各地区",
            "每个地区",
            "各个地区",
            "大区",
            "按区域",
            "分区域",
            "各区域",
        )
    ):
        return "region"
    return "unknown"


def _detect_metric(text: str) -> str:
    if any(word in text for word in ("利润", "成本", "客单价", "销量", "订单数", "转化率", "毛利")):
        return "unsupported"
    if any(word in text for word in ("毛销售额", "GMV", "未扣退款", "退款前", "毛收入")):
        return "gross_sales"
    refund_only = any(word in text for word in ("退款金额", "退款额", "退款是多少", "退款多少"))
    if refund_only and "销售额" not in text:
        return "refunds"
    return "net_sales"


def parse_question(
    text: str,
    *,
    today: date | None = None,
    known_regions: list[dict] | None = None,
    min_month: str | None = None,
    max_month: str | None = None,
) -> QuestionSpec:
    """解析用户问题，返回 :class:`QuestionSpec`。

    参数
    ----
    text:
        原始问题。空字符串会被视为需要澄清。
    today:
        用于“本月/上月/今年/去年”的基准日期，默认今天；测试时显式传入。
    known_regions:
        ``[{"region_id": ..., "region_name": ...}]``，用于地区识别与可用地区提示。
    min_month / max_month:
        数据覆盖范围（``YYYY-MM``）。超出范围时给出数据范围澄清，而不是猜测。
    """

    today = today or date.today()
    # 契约是“解析失败不抛异常”，因此非字符串输入也要收敛成澄清，而不是 AttributeError。
    raw = text if isinstance(text, str) else str(text or "")
    stripped = raw.strip()

    if not stripped:
        return QuestionSpec(
            raw=raw,
            current_month=None,
            previous_month=None,
            regions=(),
            dimension="unknown",
            metric="net_sales",
            wants_comparison=False,
            asks_refund_definition=False,
            wants_chart=False,
            needs_clarification=True,
            clarification="请描述你要分析的问题，例如「2026 年 9 月销售额比 8 月变化多少，按地区分解」。",
        )

    year_context, _ = _resolve_year_context(stripped, today)
    mentions, invalid_month = _collect_mentions(stripped, today, year_context)
    regions, region_clarification = _match_regions(stripped, known_regions)

    dimension = _detect_dimension(stripped)
    metric = _detect_metric(stripped)
    comparison = any(word in stripped for word in _COMPARISON_WORDS)
    asks_refund = "退款" in stripped and any(word in stripped for word in _REFUND_DEFINITION_WORDS)
    wants_chart = any(word in stripped for word in _CHART_WORDS)

    data_range = "2025-01 至 2026-09"
    if min_month and max_month:
        data_range = f"{min_month} 至 {max_month}"

    clarification: str | None = None
    current_month: str | None = None
    previous_month: str | None = None
    needs_clarification = False
    wants_comparison = False

    if invalid_month:
        needs_clarification = True
        clarification = f"月份不合法，请使用 1–12 月。当前数据范围：{data_range}。"

    if not needs_clarification:
        if len(mentions) >= 2:
            first_pos, first_year, first_month = mentions[0]
            second_pos, second_year, second_month = mentions[1]
            # 缺年份的期间用第一个显式年份（或“今年/去年”）补全。
            if first_year == 0 and year_context:
                first_year = year_context
            if second_year == 0:
                second_year = first_year or year_context or 0

            if first_year == 0 or second_year == 0:
                needs_clarification = True
                clarification = (
                    f"请补充年份：你说的是哪一年的 {first_month} 月和 {second_month} 月？当前数据范围：{data_range}。"
                )
            else:
                first = _format_month(first_year, first_month)
                second = _format_month(second_year, second_month)
                # 区间方向由语言关系决定，不由月份提及顺序决定。
                forward = bool(re.search(r"到|至", stripped[first_pos:second_pos]))
                current_month, previous_month = (second, first) if forward else (first, second)
                wants_comparison = True

        elif len(mentions) == 1:
            _, year, month = mentions[0]
            if year == 0:
                needs_clarification = True
                clarification = f"请补充年份：你说的是哪一年的 {month} 月？当前数据范围：{data_range}。"
            else:
                current_month = _format_month(year, month)
                if "同比" in stripped or "去年同期" in stripped:
                    previous_month = _format_month(year - 1, month)
                    wants_comparison = True
                elif any(word in stripped for word in ("环比", "比上月", "较上月", "和上月比", "与上月比")):
                    prev_year, prev_month = _shift_month(year, month, -1)
                    previous_month = _format_month(prev_year, prev_month)
                    wants_comparison = True

        else:
            # 没有期间，也没有地区追问：要求补充时间范围。
            if not regions and not any(
                word in stripped for word in ("退款怎么算", "净销售额", "毛销售额", "口径", "退款归属")
            ):
                needs_clarification = True
                clarification = f"请提供要分析的时间范围，例如「2026 年 9 月」。当前数据范围：{data_range}。"

    time_error = clarification
    input_error = region_clarification
    if metric == "unsupported":
        input_error = "当前仅支持净销售额、毛销售额和退款额；该指标尚未实现，请换一个指标。"
    if input_error:
        needs_clarification = True
        clarification = input_error

    if not needs_clarification and (current_month or previous_month):
        for month in (current_month, previous_month):
            if not month:
                continue
            if min_month and month < min_month:
                needs_clarification = True
                clarification = f"{_month_label(month)} 早于数据起始月 {min_month}，无法分析。"
                break
            if max_month and month > max_month:
                needs_clarification = True
                clarification = f"{_month_label(month)} 晚于数据截止月 {max_month}，无法分析。"
                break

    if not needs_clarification and comparison and not wants_comparison and current_month:
        # “九月变化多少”这类没有第二个期间的表达，按环比补全，避免静默给单月数字。
        year, month = (int(part) for part in current_month.split("-"))
        prev_year, prev_month = _shift_month(year, month, -1)
        previous_month = _format_month(prev_year, prev_month)
        wants_comparison = True

    # 范围校验也属于时间错误，继承不得掩盖明确的非法期间。
    if needs_clarification and not input_error:
        time_error = clarification
    is_definition = any(word in stripped for word in ("定义", "怎么算", "口径", "归属", "是否", "包含", "扣除", "计入"))
    intent = "knowledge" if not mentions and is_definition else "mixed" if is_definition else "analysis"
    explicit_metric = any(
        word in stripped
        for word in ("销售额", "退款额", "退款金额", "GMV", "毛收入", "利润", "成本", "销量", "订单数", "客单价")
    )
    region_action = (
        "clear"
        if any(word in stripped for word in ("全国", "全部地区", "所有地区", "所有区域"))
        else "replace"
        if regions
        else "inherit"
    )
    if region_clarification:
        region_action = "invalid"
    return QuestionSpec(
        raw=raw,
        current_month=current_month,
        previous_month=previous_month,
        regions=regions,
        dimension=dimension,
        metric=metric,
        wants_comparison=wants_comparison,
        asks_refund_definition=asks_refund,
        wants_chart=wants_chart,
        needs_clarification=needs_clarification,
        clarification=clarification,
        metric_explicit=explicit_metric,
        region_action=region_action,
        intent=intent,
        input_error=input_error,
        time_error=time_error,
    )
