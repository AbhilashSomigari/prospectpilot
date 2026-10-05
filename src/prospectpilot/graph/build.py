"""Graph wiring: Prospector → Enricher → Verifier → Writer ⇄ Critic → Outbox → ReplySim."""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from prospectpilot.graph.deps import PipelineDeps
from prospectpilot.graph.nodes import PipelineState, build_nodes, instrument


def route_after_critic(state: PipelineState, max_rewrites: int) -> str:
    """Rewrite failed drafts (with the critique) until max_rewrites is used up."""
    if state.get("to_write") and state.get("round", 0) <= max_rewrites:
        return "writer"
    return "outbox"


def build_graph(deps: PipelineDeps, checkpointer: Any | None = None) -> Any:
    nodes = build_nodes(deps)
    g: StateGraph[PipelineState] = StateGraph(PipelineState)
    for name, fn in nodes.items():
        node: Any = instrument(name, fn)
        g.add_node(name, node)
    g.add_edge(START, "prospector")
    g.add_edge("prospector", "enricher")
    g.add_edge("enricher", "verifier")
    g.add_edge("verifier", "writer")
    g.add_edge("writer", "critic")
    max_rewrites = deps.settings.critic_max_rewrites
    g.add_conditional_edges(
        "critic",
        lambda s: route_after_critic(s, max_rewrites),
        {"writer": "writer", "outbox": "outbox"},
    )
    g.add_edge("outbox", "reply_simulator")
    g.add_edge("reply_simulator", "finalize")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer)


def mermaid() -> str:
    """Mermaid diagram of the compiled graph (used in the README)."""
    from prospectpilot.config import get_settings
    from prospectpilot.graph.deps import build_deps

    deps = build_deps({"send": False, "offline": True}, get_settings())
    text: str = build_graph(deps).get_graph().draw_mermaid()
    return text
