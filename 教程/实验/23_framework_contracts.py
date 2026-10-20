"""Executable corrections for tutorial units 06, 17 and 18; no API key."""

import json
import operator
from typing import Annotated, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import InvalidUpdateError
from langgraph.graph import END, START, StateGraph


class State(TypedDict):
    value: int


results = {}
for label, node, explicit_end, expected in [
    ("none_is_no_update", lambda state: None, True, {"value": 1}),
    (
        "terminal_node_needs_no_explicit_end",
        lambda state: {"value": 2},
        False,
        {"value": 2},
    ),
    ("unknown_output_key_is_ignored", lambda state: {"typo": 3}, True, {"value": 1}),
]:
    builder = StateGraph(State).add_node("node", node).add_edge(START, "node")
    if explicit_end:
        builder.add_edge("node", END)
    result = builder.compile().invoke({"value": 1})
    assert result == expected
    results[label] = result

builder = StateGraph(State)
builder.add_node("a", lambda state: {"value": 2})
builder.add_node("b", lambda state: {"value": 3})
builder.add_edge(START, "a").add_edge(START, "b")
try:
    builder.compile().invoke({"value": 1})
except InvalidUpdateError:
    results["same_step_conflict"] = "InvalidUpdateError"
else:
    raise AssertionError("Parallel writes without reducer must be rejected")


class JoinState(TypedDict):
    log: Annotated[list[str], operator.add]


for barrier in (False, True):
    builder = StateGraph(JoinState)
    for name in ("a", "b", "b2"):
        builder.add_node(name, lambda state, name=name: {"log": [name]})
    builder.add_node("join", lambda state: {"log": ["JOIN"]})
    builder.add_edge(START, "a").add_edge(START, "b").add_edge("b", "b2")
    if barrier:
        builder.add_edge(["a", "b2"], "join")
    else:
        builder.add_edge("a", "join").add_edge("b2", "join")
    builder.add_edge("join", END)
    result = builder.compile().invoke({"log": []})
    assert result["log"].count("JOIN") == (1 if barrier else 2)
    results[f"barrier_{barrier}"] = result

# Actual replay and fork, unlike merely reading get_state_history().
graph = (
    StateGraph(State)
    .add_node("set", lambda state: {"value": 3})
    .add_node("double", lambda state: {"value": state["value"] * 2})
    .add_edge(START, "set")
    .add_edge("set", "double")
    .add_edge("double", END)
    .compile(checkpointer=InMemorySaver())
)
config = {"configurable": {"thread_id": "tutorial-fork"}}
assert graph.invoke({"value": 0}, config)["value"] == 6
before = next(s for s in graph.get_state_history(config) if s.next == ("double",))
assert graph.invoke(None, before.config)["value"] == 6
fork = graph.update_state(before.config, {"value": 7}, as_node="set")
assert graph.invoke(None, fork)["value"] == 14
assert graph.get_state(before.config).values["value"] == 3
results["replay_fork"] = {"replay": 6, "fork": 14, "original_checkpoint_value": 3}
print(json.dumps(results, ensure_ascii=False, indent=2))
