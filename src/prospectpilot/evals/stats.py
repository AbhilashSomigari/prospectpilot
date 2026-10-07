"""Paired bootstrap for comparing two EvalForge runs over the same tasks and trial seeds.

Trials are paired by (task_id, trial_index): every variant in a round sees the same fixtures and
the same per-trial seeds, so pairing removes fixture difficulty from the comparison. Resampling is
clustered by fixture (all trials of a fixture are drawn together), because trials of one fixture
are correlated.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

TrialKey = tuple[str, int]


@dataclass(frozen=True)
class PairedCI:
    delta: float  # point estimate of pass-rate(candidate) - pass-rate(baseline)
    lower: float
    upper: float
    pairs: int  # paired trials compared
    level: float

    @property
    def gained_trials(self) -> int:
        return round(self.delta * self.pairs)

    def excludes_zero(self) -> bool:
        return self.lower > 0 or self.upper < 0


def trial_successes(results: list[Any]) -> dict[TrialKey, bool]:
    """From EvalForge TrialResult objects or their JSON dicts."""
    out: dict[TrialKey, bool] = {}
    for r in results:
        if isinstance(r, dict):
            out[(str(r["task_id"]), int(r["trial_index"]))] = bool(r["success"])
        else:
            out[(str(r.task_id), int(r.trial_index))] = bool(r.success)
    return out


def paired_bootstrap_ci(
    baseline: dict[TrialKey, bool],
    candidate: dict[TrialKey, bool],
    *,
    samples: int = 5000,
    level: float = 0.95,
    seed: int = 7,
) -> PairedCI:
    keys = sorted(set(baseline) & set(candidate))
    if not keys:
        raise ValueError("runs share no (task_id, trial_index) pairs")
    tasks = sorted({k[0] for k in keys})
    by_task: dict[str, list[int]] = {t: [] for t in tasks}
    for k in keys:
        by_task[k[0]].append(int(candidate[k]) - int(baseline[k]))

    def mean_diff(sample: list[str]) -> float:
        diffs = [d for t in sample for d in by_task[t]]
        return sum(diffs) / len(diffs)

    rng = random.Random(seed)
    stats = sorted(mean_diff([rng.choice(tasks) for _ in tasks]) for _ in range(samples))
    tail = (1 - level) / 2
    lo = stats[int(tail * samples)]
    hi = stats[min(samples - 1, int((1 - tail) * samples))]
    return PairedCI(delta=mean_diff(tasks), lower=lo, upper=hi, pairs=len(keys), level=level)
