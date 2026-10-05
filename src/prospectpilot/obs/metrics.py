"""Prometheus metrics. The API serves them at /metrics; the worker on WORKER_METRICS_PORT."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, start_http_server

LLM_CALLS = Counter("pp_llm_calls_total", "LLM calls", ["provider", "model", "purpose", "status"])
LLM_TOKENS = Counter("pp_llm_tokens_total", "LLM tokens", ["provider", "model", "direction"])
LLM_COST = Counter("pp_llm_cost_usd_total", "LLM spend in USD", ["provider", "model", "purpose"])
LLM_LATENCY = Histogram(
    "pp_llm_latency_seconds",
    "LLM call latency",
    ["provider", "model", "purpose"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 4, 8, 16, 32, 64, 128),
)
NODE_DURATION = Histogram(
    "pp_graph_node_duration_seconds",
    "LangGraph node duration",
    ["node"],
    buckets=(0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600),
)
TOOL_CALLS = Counter(
    "pp_tool_calls_total", "Tool calls (fetch, dns, smtp, ...)", ["tool", "status"]
)
TOOL_LATENCY = Histogram(
    "pp_tool_latency_seconds",
    "Tool call latency",
    ["tool"],
    buckets=(0.001, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10),
)
LEADS = Counter("pp_leads_processed_total", "Leads finished by the pipeline", ["outcome"])
LEAD_LATENCY = Histogram(
    "pp_lead_latency_seconds",
    "End-to-end processing time per lead",
    buckets=(0.1, 0.5, 1, 2, 5, 10, 20, 40, 80, 160, 320),
)
LEAD_COST = Histogram(
    "pp_lead_cost_usd",
    "LLM cost per lead",
    buckets=(0.0, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1),
)
CRITIC_VERDICTS = Counter(
    "pp_critic_verdicts_total", "Critic verdicts per draft round", ["round", "verdict"]
)
EMAILS = Counter("pp_emails_total", "Outbox email events", ["status"])
RUNS = Counter("pp_runs_total", "Campaign runs finished", ["status"])
IMPROVE_METRIC = Gauge(
    "pp_improve_metric", "Latest improvement-round metrics", ["metric", "variant"]
)


def start_metrics_server(port: int) -> None:
    start_http_server(port)
