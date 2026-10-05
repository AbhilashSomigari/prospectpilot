"""Generate the provisioned Grafana dashboard (docker/grafana/dashboards/prospectpilot.json).

Prometheus panels show live pipeline operations; Postgres panels read the improvement changelog
(`improvement_rounds`) so quality / cost per lead / latency per round survive restarts.

Usage: uv run python scripts/build_dashboard.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OUT = (
    Path(__file__).resolve().parents[1] / "docker" / "grafana" / "dashboards" / "prospectpilot.json"
)
PROM = {"type": "prometheus", "uid": "prometheus"}
PG = {"type": "grafana-postgresql-datasource", "uid": "ppdb"}

_id = 0


def _next_id() -> int:
    global _id
    _id += 1
    return _id


def prom(title: str, exprs: list[tuple[str, str]], x: int, y: int, w: int = 8, h: int = 8,
         kind: str = "timeseries", unit: str = "short") -> dict[str, Any]:  # fmt: skip
    return {
        "id": _next_id(),
        "type": kind,
        "title": title,
        "datasource": PROM,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "options": {"legend": {"displayMode": "list", "placement": "bottom"}},
        "targets": [
            {"refId": chr(65 + i), "datasource": PROM, "expr": e, "legendFormat": legend}
            for i, (e, legend) in enumerate(exprs)
        ],
    }


def sql(title: str, query: str, x: int, y: int, w: int = 8, h: int = 8, kind: str = "barchart",
        unit: str = "short", options: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    return {
        "id": _next_id(),
        "type": kind,
        "title": title,
        "datasource": PG,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "options": options
        or {"xField": "round", "legend": {"displayMode": "list", "placement": "bottom"}},
        "targets": [
            {
                "refId": "A",
                "datasource": PG,
                "format": "table",
                "rawQuery": True,
                "editorMode": "code",
                "rawSql": query,
            }
        ],
    }


def row(title: str, y: int) -> dict[str, Any]:
    return {
        "id": _next_id(),
        "type": "row",
        "title": title,
        "collapsed": False,
        "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
        "panels": [],
    }


ROUND_SQL = """
SELECT 'r' || round AS round,
       (metrics_before->>'{k}')::float AS "active (before)",
       (metrics_after->>'{k}')::float  AS "best candidate"
FROM improvement_rounds WHERE status = 'completed' ORDER BY improvement_rounds.round
"""


def build() -> dict[str, Any]:
    panels: list[dict[str, Any]] = []
    panels.append(row("Self-improvement loop (per round, from the changelog table)", 0))
    panels.append(
        sql(
            "Quality: grounded-and-passing rate",
            ROUND_SQL.format(k="pass_rate"),
            0,
            1,
            unit="percentunit",
        )
    )
    panels.append(
        sql(
            "Cost per lead (USD)", ROUND_SQL.format(k="cost_per_task_usd"), 8, 1, unit="currencyUSD"
        )
    )
    panels.append(
        sql("Latency per lead p50 (ms)", ROUND_SQL.format(k="p50_latency_ms"), 16, 1, unit="ms")
    )
    panels.append(
        sql(
            "Improvement changelog",
            """SELECT round, decision, provider, models->>'large' AS writer_model,
                  (metrics_before->>'pass_rate')::float AS pass_before,
                  (metrics_after->>'pass_rate')::float AS pass_after,
                  (metrics_before->>'judge_mean')::float AS judge_before,
                  (metrics_after->>'judge_mean')::float AS judge_after,
                  finished_at
           FROM improvement_rounds ORDER BY round DESC""",
            0,
            9,
            w=24,
            h=7,
            kind="table",
            options={"showHeader": True},
        )
    )
    panels.append(row("Pipeline (Prometheus)", 16))
    panels.append(
        prom(
            "Leads processed by outcome",
            [("sum by (outcome) (increase(pp_leads_processed_total[$__range]))", "{{outcome}}")],
            0,
            17,
            kind="bargauge",
        )
    )
    panels.append(
        prom(
            "LLM spend (USD, range)",
            [("sum(increase(pp_llm_cost_usd_total[$__range]))", "spend")],
            8,
            17,
            w=4,
            kind="stat",
            unit="currencyUSD",
        )
    )
    panels.append(
        prom(
            "Cost per lead (USD, range)",
            [
                (
                    "sum(increase(pp_llm_cost_usd_total[$__range])) / clamp_min(sum(increase(pp_leads_processed_total[$__range])), 1)",
                    "per lead",
                )
            ],
            12,
            17,
            w=4,
            kind="stat",
            unit="currencyUSD",
        )
    )
    panels.append(
        prom(
            "Lead latency p50 / p95",
            [
                (
                    "histogram_quantile(0.5, sum by (le) (rate(pp_lead_latency_seconds_bucket[5m])))",
                    "p50",
                ),
                (
                    "histogram_quantile(0.95, sum by (le) (rate(pp_lead_latency_seconds_bucket[5m])))",
                    "p95",
                ),
            ],
            16,
            17,
            unit="s",
        )
    )
    panels.append(
        prom(
            "Graph node duration p95",
            [
                (
                    "histogram_quantile(0.95, sum by (le, node) (rate(pp_graph_node_duration_seconds_bucket[5m])))",
                    "{{node}}",
                )
            ],
            0,
            25,
            unit="s",
        )
    )
    panels.append(
        prom(
            "LLM latency p95 by purpose",
            [
                (
                    "histogram_quantile(0.95, sum by (le, purpose) (rate(pp_llm_latency_seconds_bucket[5m])))",
                    "{{purpose}}",
                )
            ],
            8,
            25,
            unit="s",
        )
    )
    panels.append(
        prom(
            "LLM tokens / s by model",
            [
                (
                    "sum by (model, direction) (rate(pp_llm_tokens_total[5m]))",
                    "{{model}} {{direction}}",
                )
            ],
            16,
            25,
        )
    )
    panels.append(
        prom(
            "Critic verdicts by draft round",
            [
                (
                    "sum by (round, verdict) (increase(pp_critic_verdicts_total[$__range]))",
                    "round {{round}} {{verdict}}",
                )
            ],
            0,
            33,
            kind="bargauge",
        )
    )
    panels.append(
        prom(
            "Emails (Mailpit sandbox)",
            [("sum by (status) (increase(pp_emails_total[$__range]))", "{{status}}")],
            8,
            33,
            kind="bargauge",
        )
    )
    panels.append(
        prom(
            "Tool calls by status",
            [("sum by (tool, status) (rate(pp_tool_calls_total[5m]))", "{{tool}} {{status}}")],
            16,
            33,
        )
    )
    panels.append(row("Runs", 41))
    panels.append(
        sql(
            "Recent runs (open trace_id in Jaeger at :16686)",
            """SELECT created_at, status, (stats->>'prospects')::int AS leads,
                  (stats->>'final_pass')::int AS passed, (stats->>'emails_sent')::int AS sent,
                  (stats->>'cost_usd')::float AS cost_usd, trace_id
           FROM runs ORDER BY created_at DESC LIMIT 20""",
            0,
            42,
            w=24,
            h=8,
            kind="table",
            options={"showHeader": True},
        )
    )
    return {
        "uid": "prospectpilot",
        "title": "ProspectPilot",
        "tags": ["prospectpilot"],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "refresh": "10s",
        "time": {"from": "now-6h", "to": "now"},
        "panels": panels,
    }


if __name__ == "__main__":
    OUT.write_text(json.dumps(build(), indent=2) + "\n")
    print(f"wrote {OUT}")
