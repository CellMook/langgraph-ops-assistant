"""单元 03 实验：结构化输出 + 业务校验。

用法（在 ops-assistant 目录执行）：uv run python ../教程/实验/03_validate_spec.py
不需要模型密钥：本脚本用“手工构造的脏输入”验证校验层。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
DATA_MIN_MONTH = "2025-01"
DATA_MAX_MONTH = "2026-09"

CN_DIGITS = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
    "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12,
}


class QuestionSpecModel(BaseModel):
    """问题解析结果的结构。字段口径见工程规格 5.2。"""

    model_config = ConfigDict(extra="forbid")

    raw: str = Field(min_length=1, description="用户原始问题（不可为空）")
    current_month: str = Field(pattern=MONTH_PATTERN, description="本期月份 YYYY-MM")
    previous_month: str | None = Field(
        default=None, pattern=MONTH_PATTERN, description="对比的基准月（上期）；不比较时为 None"
    )
    regions: list[str] = Field(
        default_factory=list, description="region_id 列表，如 R_EAST；空列表 = 全部地区"
    )
    dimension: str = Field(default="region", description="分析维度：region|product|month|channel|unknown")
    metric: str = Field(default="net_sales", description="指标：net_sales|gross_sales|refunds")
    wants_comparison: bool = Field(default=False, description="用户是否要求对比（环比/同比）")
    asks_refund_definition: bool = Field(default=False, description="用户是否在问“退款”的定义")
    needs_clarification: bool = Field(default=False, description="信息不足，需要向用户澄清")
    clarification: str | None = Field(
        default=None, description="给用户看的澄清问题文本；needs_clarification=True 时才有意义"
    )

    @field_validator("dimension")
    @classmethod
    def check_dimension(cls, value: str) -> str:
        allowed = {"region", "product", "month", "channel", "unknown"}
        if value not in allowed:
            raise ValueError(f"dimension 必须是 {sorted(allowed)} 之一，实际 {value!r}")
        return value

    @field_validator("metric")
    @classmethod
    def check_metric(cls, value: str) -> str:
        allowed = {"net_sales", "gross_sales", "refunds"}
        if value not in allowed:
            raise ValueError(f"metric 必须是 {sorted(allowed)} 之一，实际 {value!r}")
        return value


class BusinessRuleError(ValueError):
    """业务规则不通过（模式层合法，但业务上不接受）。"""


def normalize_month_token(text: str) -> str | None:
    """把“9月/九月/09月”这类token归一化成两位月份；无法识别返回 None。"""
    cleaned = text.strip().replace(" ", "")
    arabic = re.fullmatch(r"(0?[1-9]|1[0-2])月", cleaned)
    if arabic:
        return f"{int(arabic.group(1)):02d}"
    for cn, number in CN_DIGITS.items():
        if cleaned == f"{cn}月":
            return f"{number:02d}"
    return None


def check_month_in_range(month: str) -> None:
    """业务校验 1：月份必须在数据范围内。"""
    if not (DATA_MIN_MONTH <= month <= DATA_MAX_MONTH):
        raise BusinessRuleError(
            f"月份 {month} 超出数据范围 {DATA_MIN_MONTH}～{DATA_MAX_MONTH}；"
            "请确认是否指其他年份。"
        )


def check_period_order(current: str, previous: str | None) -> None:
    """业务校验 2：本期必须晚于上期；同时不允许相同。"""
    if previous is None:
        return
    if previous >= current:
        raise BusinessRuleError(f"上期 {previous} 必须早于本期 {current}。")
    check_month_in_range(previous)
    check_month_in_range(current)


KNOWN_REGIONS = {
    "R_EAST": "华东", "R_NORTH": "华北", "R_SOUTH": "华南",
    "R_SW": "西南", "R_NE": "东北",
}


def normalize_region(token: str) -> str:
    """业务校验 3：地区必须是已知 5 个之一，支持“华东/华东地区/R_EAST”。"""
    cleaned = token.strip().replace("地区", "").replace("大区", "")
    if cleaned in KNOWN_REGIONS:
        return cleaned
    for region_id, name in KNOWN_REGIONS.items():
        if cleaned == name or cleaned == region_id:
            return region_id
    raise BusinessRuleError(
        f"无法识别地区 {token!r}；可用地区：{', '.join(f'{k}({v})' for k, v in KNOWN_REGIONS.items())}"
    )


def validate_spec(payload: dict) -> QuestionSpecModel:
    """完整校验链：模式层 → 业务层 → 归一化。"""
    spec = QuestionSpecModel.model_validate(payload)
    check_period_order(spec.current_month, spec.previous_month)
    spec.regions = [normalize_region(item) for item in spec.regions]
    return spec


def show_case(name: str, payload: dict) -> None:
    print(f"\n--- {name} ---")
    print("输入：", json.dumps(payload, ensure_ascii=False))
    try:
        spec = validate_spec(payload)
    except ValidationError as exc:
        print("模式层拒绝：")
        for error in exc.errors():
            print(f"  - 字段 {error['loc']}：{error['msg']}（类型 {error['type']}）")
        return
    except BusinessRuleError as exc:
        print(f"业务层拒绝：{exc}")
        return
    print("通过：", spec.model_dump())


def main() -> int:
    print("== 单元 03 实验：结构化输出与业务校验 ==")
    print(f"数据范围：{DATA_MIN_MONTH} ～ {DATA_MAX_MONTH}；今天是 {date.today().isoformat()}（仅用于显示）")

    print("\n== 归一化演示 ==")
    for token in ("9月", "九月", "09月", "十二 月", "13月"):
        print(f"  {token!r:>8} → {normalize_month_token(token)}")

    show_case(
        "正常：2026-09 vs 2026-08，按地区",
        {
            "raw": "2026 年九月销售额比八月变化多少？按地区分解差额。",
            "current_month": "2026-09",
            "previous_month": "2026-08",
            "regions": [],
            "dimension": "region",
            "metric": "net_sales",
            "wants_comparison": True,
        },
    )
    show_case(
        "坏输入 1：月份 2026-13（模式层）",
        {"raw": "2026年13月销售额", "current_month": "2026-13"},
    )
    show_case(
        "坏输入 2：2027-01 超范围（业务层）",
        {"raw": "2027年一月销售额", "current_month": "2027-01"},
    )
    show_case(
        "坏输入 3：上期晚于本期（业务层）",
        {
            "raw": "八月比九月",
            "current_month": "2026-08",
            "previous_month": "2026-09",
        },
    )
    show_case(
        "坏输入 4：渠道维度（模式层放行，上层应判“不支持”）",
        {
            "raw": "2026年九月按渠道销售额",
            "current_month": "2026-09",
            "dimension": "channel",
        },
    )
    show_case(
        "坏输入 5：未知地区（业务层）",
        {
            "raw": "2026年九月华中地区销售额",
            "current_month": "2026-09",
            "regions": ["华中"],
        },
    )
    show_case(
        "坏输入 6：模型多编一个字段（extra=forbid）",
        {
            "raw": "2026年九月销售额",
            "current_month": "2026-09",
            "confidence": 0.98,
        },
    )
    show_case(
        "正常：地区归一化 华东地区 → R_EAST",
        {
            "raw": "那华东地区呢？",
            "current_month": "2026-09",
            "regions": ["华东地区"],
        },
    )

    print("\n== 结论 ==")
    print("模式层挡类型/格式，业务层挡范围/顺序/枚举；两者都不能靠提示词保证。")
    print("渠道维度这条在本实验里被 dimension 校验放行、应由上层路由判为“不支持”，见单元 08。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
