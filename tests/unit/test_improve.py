from __future__ import annotations

from typing import Any

from evalforge.models import AgentOutput, EvalRun, GradeResult, RunSummary, TrialResult, Usage

from prospectpilot.evals.improve import (
    Candidate,
    cluster_failures,
    compose_prompt,
    promotion_gates,
    prompt_diff,
    valid_candidate,
)


def trial(
    task: str, success: bool, final: list[str], first: list[str], cost: float = 0.0
) -> TrialResult:
    return TrialResult(
        task_id=task,
        trial_index=0,
        success=success,
        output=AgentOutput(
            output="{}",
            usage=Usage(cost_usd=cost),
            metadata={
                "final_failures": final,
                "first_failures": first,
                "feedback": f"- [{(final or first or ['x'])[0]}] detail",
            },
        ),
        grades=[
            GradeResult(grader="sequence_checks", score=1.0 if success else 0.0, passed=success)
        ],
    )


def run(results: list[TrialResult], success: float, cost: float = 0.0) -> EvalRun:
    return EvalRun(
        suite_name="s",
        agent_name="a",
        results=results,
        summary=RunSummary(task_success=success, avg_cost_usd=cost, trials=len(results)),
    )


def metrics(pass_rate: float, judge: float, tokens: float, grounded: float = 0.8) -> dict[str, Any]:
    return {"pass_rate": pass_rate, "judge_mean": judge, "tokens_per_task": tokens,
            "grounded_claim_rate": grounded}  # fmt: skip


def paired_run(passes: int, cost: float = 0.0, tasks: int = 40, trials: int = 2) -> EvalRun:
    """A run over the standard suite shape whose first `passes` trials succeed."""
    results = []
    for i in range(tasks * trials):
        results.append(
            TrialResult(task_id=f"t{i // trials:02d}", trial_index=i % trials, success=i < passes,
                        output=AgentOutput(output="{}", usage=Usage(cost_usd=cost)))
        )  # fmt: skip
    return run(results, passes / (tasks * trials), cost=cost)


def gates_by_name(gates: list[dict[str, Any]]) -> dict[str, bool]:
    return {g["gate"]: g["passed"] for g in gates}


def test_cluster_by_primary_failure() -> None:
    r = run(
        [
            trial("a", False, ["word_limit", "ungrounded_claims"], ["ungrounded_claims"]),
            trial("b", False, ["ungrounded_claims"], ["ungrounded_claims"]),
            trial("c", False, ["spam_words"], ["spam_words"]),
            trial("d", True, [], ["missing_fact_markers"]),
        ],
        0.25,
    )
    clusters = cluster_failures(r)
    assert [(c.code, c.count, c.stage) for c in clusters] == [
        ("ungrounded_claims", 2, "final"),
        ("spam_words", 1, "final"),
    ]


def test_first_draft_failures_used_when_finals_are_rare() -> None:
    r = run(
        [
            trial("a", True, [], ["missing_fact_markers"]),
            trial("b", True, [], ["missing_fact_markers"]),
        ],
        1.0,
    )
    clusters = cluster_failures(r)
    assert clusters[0].code == "missing_fact_markers" and clusters[0].stage == "first_draft"


def test_gates_require_strict_improvement() -> None:
    active, same = paired_run(48), paired_run(48)
    by = gates_by_name(
        promotion_gates(active, same, metrics(0.6, 4.0, 1000), metrics(0.6, 4.0, 1000))
    )
    assert not by["task_success"] and not by["pass_rate_gain_significant"]


def test_noise_level_gain_is_not_promoted() -> None:
    """+1 of 80 trials (what round 3 promoted under the old gate) beats ACTIVE but not noise."""
    active, cand = paired_run(19), paired_run(20)
    gates = promotion_gates(active, cand, metrics(0.2375, 3.2, 4000), metrics(0.25, 3.3, 4000))
    by = gates_by_name(gates)
    assert by["task_success"]  # strictly better …
    assert not by["pass_rate_gain_significant"]  # … but the CI includes 0
    sig = next(g for g in gates if g["gate"] == "pass_rate_gain_significant")
    assert sig["ci"][0] <= 0 <= sig["ci"][1] and "+1 of 80" in sig["threshold"]


def test_clear_gain_is_promoted() -> None:
    active, cand = paired_run(20), paired_run(50)  # +30 of 80
    gates = promotion_gates(active, cand, metrics(0.25, 3.2, 4000), metrics(0.625, 3.4, 4000))
    assert all(g["passed"] for g in gates), gates


def test_grounding_regression_blocks_promotion() -> None:
    active, cand = paired_run(20), paired_run(50)
    worse = gates_by_name(
        promotion_gates(
            active,
            cand,
            metrics(0.25, 3.2, 4000, grounded=0.80),
            metrics(0.625, 3.4, 4000, grounded=0.75),
        )
    )
    assert worse["pass_rate_gain_significant"] and not worse["grounded_claim_rate"]
    within = gates_by_name(
        promotion_gates(
            active,
            cand,
            metrics(0.25, 3.2, 4000, grounded=0.80),
            metrics(0.625, 3.4, 4000, grounded=0.785),
        )
    )
    assert within["grounded_claim_rate"]  # fmt: skip


def test_gates_block_cost_and_judge_regressions() -> None:
    active = paired_run(20, cost=0.010)
    better_but_pricey = paired_run(50, cost=0.0116)  # +16%
    by = gates_by_name(
        promotion_gates(
            active, better_but_pricey, metrics(0.25, 4.0, 1000), metrics(0.625, 3.7, 1000)
        )
    )
    assert by["task_success"] and not by["avg_cost_usd"] and not by["judge_mean"]
    ok = paired_run(50, cost=0.0114)  # +14%
    gates = promotion_gates(active, ok, metrics(0.25, 4.0, 1000), metrics(0.625, 3.85, 1000))
    assert all(g["passed"] for g in gates), gates  # fmt: skip


def test_unpriced_provider_uses_token_cost_proxy() -> None:
    active, cand = paired_run(20), paired_run(50)
    gates = promotion_gates(active, cand, metrics(0.25, 4.0, 1000), metrics(0.625, 4.0, 1200))
    assert not gates_by_name(gates)["tokens_per_task (cost proxy; provider unpriced)"]


def test_no_paired_trials_cannot_promote() -> None:
    gates = promotion_gates(
        run([], 0.2), run([], 0.9), metrics(0.2, 4.0, 1000), metrics(0.9, 4.0, 1000)
    )
    assert not gates_by_name(gates)["pass_rate_gain_significant"]


def test_candidate_validation() -> None:
    active = 'Use [fact:ID] markers. Reply {"emails": [...]}'
    assert (
        valid_candidate(Candidate(name="x", rationale="", prompt=active), active)
        == "identical to active"
    )
    assert valid_candidate(Candidate(name="x", rationale="", prompt="be nice"), active) is not None
    assert (
        valid_candidate(Candidate(name="x", rationale="", prompt=active + " Be brief."), active)
        is None
    )


def test_prompt_diff() -> None:
    d = prompt_diff("a\nb\n", "a\nc\n", "v1", "v2")
    assert "-b" in d and "+c" in d and "--- v1" in d


def test_compose_prompt_keeps_output_section_identical() -> None:
    active = 'Cite [fact:ID].\n\nReply with JSON only:\n{"emails": []}\n'
    new = compose_prompt(active, "- Restate the fact.", "targeted", 3)
    assert new.endswith('Reply with JSON only:\n{"emails": []}\n')
    assert "Additional guidance (round 3, targeted):\n- Restate the fact." in new
    c = Candidate(name="t", rationale="", prompt=new, guidance='Return {"emails": 1}')
    assert valid_candidate(c, active) == "guidance tries to change the output format"
