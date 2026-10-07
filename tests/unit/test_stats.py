from __future__ import annotations

import pytest

from prospectpilot.evals.stats import paired_bootstrap_ci, trial_successes


def outcomes(passes: set[int], n: int = 80) -> dict[tuple[str, int], bool]:
    return {(f"t{i // 2:02d}", i % 2): i in passes for i in range(n)}


def test_identical_runs_have_zero_delta_and_ci_spanning_zero() -> None:
    a = outcomes(set(range(0, 80, 3)))
    ci = paired_bootstrap_ci(a, dict(a))
    assert ci.delta == 0 and ci.lower <= 0 <= ci.upper and not ci.excludes_zero()


def test_large_gain_excludes_zero_and_is_deterministic() -> None:
    a, b = outcomes(set(range(20))), outcomes(set(range(50)))
    ci1, ci2 = paired_bootstrap_ci(a, b), paired_bootstrap_ci(a, b)
    assert ci1 == ci2  # fixed seed
    assert ci1.delta == pytest.approx(30 / 80) and ci1.gained_trials == 30
    assert ci1.lower > 0 and ci1.excludes_zero()


def test_pairs_only_on_shared_trials() -> None:
    a = outcomes(set(range(10)))
    b = {k: v for k, v in outcomes(set(range(10))).items() if k[0] < "t20"}
    assert paired_bootstrap_ci(a, b).pairs == 40
    with pytest.raises(ValueError):
        paired_bootstrap_ci(a, {("other", 0): True})


def test_trial_successes_accepts_json_dicts() -> None:
    rows = [{"task_id": "x", "trial_index": 0, "success": True},
            {"task_id": "x", "trial_index": 1, "success": False}]  # fmt: skip
    assert trial_successes(rows) == {("x", 0): True, ("x", 1): False}
