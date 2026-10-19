"""单元 01 实验 A：读 .env → 发一条消息 → 打印回复。

用法（在 ops-assistant 目录执行）：uv run python ../教程/实验/01_hello_model.py
无密钥时自动走“规则降级”分支，不联网、不报错。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / "ops-assistant" / ".env"
TIMEOUT_S = 60.0  # 单次请求超时（秒）；第 8 节实验 8.5 会让你把它改小


def mask(secret: str | None) -> str:
    """只暴露密钥首 4 位与末 2 位，避免误贴到聊天窗口。"""
    if not secret:
        return "未设置"
    if len(secret) <= 8:
        return "已设置（长度过短）"
    return f"{secret[:4]}…{secret[-2:]}"


def split_model(raw: str, provider_hint: str) -> tuple[str, str]:
    """按第一个冒号拆分 "provider:model"；没有前缀时用 provider_hint。"""
    cleaned = raw.strip()
    if ":" in cleaned:
        provider, _, model_name = cleaned.partition(":")
        return provider.strip(), model_name.strip()
    return provider_hint.strip(), cleaned


def build_model_config() -> dict[str, object]:
    """读取并校验配置；返回给 ChatOpenAI 用的参数字典。"""
    if ENV_PATH.is_file():
        load_dotenv(dotenv_path=ENV_PATH, override=False)
    else:
        print(f"[提示] 未找到 {ENV_PATH}，将只用系统环境变量（多半是空 → 规则模式）")

    raw_model = (os.getenv("OPS_MODEL") or "").strip()
    provider_hint = (os.getenv("OPS_MODEL_PROVIDER") or "").strip()
    api_key = (os.getenv("OPS_API_KEY") or "").strip() or os.getenv("OPENAI_API_KEY")
    base_url = (os.getenv("OPS_BASE_URL") or "").strip() or None

    provider, model_name = split_model(raw_model, provider_hint)
    if not model_name:
        return {"enabled": False, "reason": "OPS_MODEL 为空", "timeout_s": TIMEOUT_S}
    if not api_key:
        return {
            "enabled": False,
            "reason": "有 OPS_MODEL 但没有 OPS_API_KEY",
            "timeout_s": TIMEOUT_S,
        }
    if provider in {"", "openai"} and base_url is None:
        pass  # 走 OpenAI 官方默认地址
    elif base_url is None:
        return {
            "enabled": False,
            "reason": f"provider={provider} 需要 OPS_BASE_URL（OpenAI 兼容端点）",
            "timeout_s": TIMEOUT_S,
        }
    return {
        "enabled": True,
        "provider": provider or "openai",
        "model_name": model_name,
        "api_key": api_key,
        "base_url": base_url,
        "timeout_s": TIMEOUT_S,
    }


SYSTEM_PROMPT = (
    "你是一名经营数据分析助手。规则："
    "1) 金额单位是“分”，展示时转成元；"
    "2) 只允许使用用户给出的数字，禁止自行计算或编造；"
    "3) 不确定时直接说“不确定”。"
)

HUMAN_PROMPT = (
    "已知：2026-09 净销售额 1286500 分（毛销售额 1400000 分，已批准退款 113500 分）；"
    "2026-08 净销售额 1190000 分。"
    "请用两句中文复述这两组数字，并说明“净销售额”是否已扣除退款。不要做任何计算。"
)


def rule_mode_answer() -> str:
    """无模型时的确定性回答：模板拼接，绝不编数字。"""
    return (
        "【规则模式】2026-09 净销售额 1286500 分（毛销售额 1400000 分，"
        "已批准退款 113500 分）；2026-08 净销售额 1190000 分。"
        "净销售额 = 毛销售额 − 已批准退款，因此已扣除退款。"
    )


def main() -> int:
    print("== 单元 01 实验 A：第一条模型调用 ==")
    config = build_model_config()

    if not config.get("enabled"):
        print(f"[规则模式] 未启用模型：{config.get('reason')}")
        print(f"[规则模式] 系统提示词长度：{len(SYSTEM_PROMPT)} 字")
        print(f"[规则模式] 用户提示词长度：{len(HUMAN_PROMPT)} 字")
        print("\n----- 回答（预期，未在本环境执行）-----")
        print(rule_mode_answer())
        print("----- 结束 -----")
        print("\n提示：配置 OPS_MODEL / OPS_API_KEY / OPS_BASE_URL 后重跑，可看到真实模型回复。")
        return 0

    print(f"[配置] provider = {config['provider']}")
    print(f"[配置] model    = {config['model_name']}")
    print(f"[配置] base_url = {config['base_url'] or '（OpenAI 默认）'}")
    print(f"[配置] api_key  = {mask(config['api_key'])}")
    print(f"[配置] timeout  = {config['timeout_s']} 秒")

    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_openai import ChatOpenAI

    model = ChatOpenAI(
        model=config["model_name"],
        api_key=config["api_key"],
        base_url=config["base_url"],
        temperature=0.0,
        timeout=config["timeout_s"],
        max_retries=0,
    )

    messages = [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=HUMAN_PROMPT)]
    print(f"\n[请求] 共 {len(messages)} 条消息，角色：{[m.type for m in messages]}")

    try:
        answer = model.invoke(messages)
    except Exception as exc:  # 程序边界：把异常类型与消息打印出来，而不是让用户看堆栈
        print(f"\n[失败] 调用模型时出错：{type(exc).__name__}: {exc}")
        print("排查顺序：1) API key 是否有效；2) base_url 是否正确；3) 模型名是否拼错；4) 网络/代理。")
        return 1

    print("\n----- 回答 -----")
    print(answer.content)
    print("----- 结束 -----")

    usage = getattr(answer, "usage_metadata", None)
    if usage:
        print(f"\n[用量] {usage}")
    else:
        print("\n[用量] 本次响应没有 usage_metadata（供应商可能不支持）")

    print("\n[提示] 若上面出现“加总/差额”等字眼，说明模型没有遵守“不要计算”，见第 8 节实验 8.4。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
