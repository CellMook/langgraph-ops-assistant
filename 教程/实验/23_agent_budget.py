"""Verified middleware fix pattern: nine requests, at most two tool executions."""

import json

from langchain.agents import create_agent
from langchain.agents.middleware import ToolCallLimitMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

executed = []


@tool
def calculate_change(current_cents: int, previous_cents: int) -> str:
    """Calculate a change in integer cents."""
    executed.append((current_cents, previous_cents))
    return json.dumps({"delta_cents": current_cents - previous_cents})


class BurstModel(BaseChatModel):
    @property
    def _llm_type(self):
        return "audit-burst"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if any(isinstance(m, ToolMessage) for m in messages):
            message = AIMessage(content="完成。")
        else:
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "calculate_change",
                        "args": {"current_cents": 200, "previous_cents": 100},
                        "id": f"call-{i}",
                        "type": "tool_call",
                    }
                    for i in range(9)
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=message)])


agent = create_agent(
    BurstModel(),
    tools=[calculate_change],
    middleware=[ToolCallLimitMiddleware(run_limit=2, exit_behavior="continue")],
)
result = agent.invoke(
    {"messages": [{"role": "user", "content": "计算差额"}]}, {"recursion_limit": 8}
)
assert len(executed) == 2
record = {
    "requested_tool_calls": 9,
    "executed_tool_calls": len(executed),
    "limit": 2,
    "passed": True,
    "real_model_calls": False,
}
print(json.dumps(record, ensure_ascii=False, indent=2))
