"""领域层：问题解析与指标比较。

对外只暴露两个入口：

- :func:`parse_question`：把自然语言问题解析成 :class:`QuestionSpec`。
- :func:`compare_months`：按指标比较两个月，并给出地区差额分解。
"""

from ops_assistant.domain.metrics import (
    ComparisonResult,
    RegionDelta,
    compare_months,
)
from ops_assistant.domain.question import QuestionSpec, parse_question

__all__ = [
    "ComparisonResult",
    "QuestionSpec",
    "RegionDelta",
    "compare_months",
    "parse_question",
]
