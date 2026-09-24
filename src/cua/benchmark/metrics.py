"""What one invocation did, and how it compares with the truth.

Nothing here knows about models: the replay strategy imports this module, and
replay must not be able to reach one. What is model-specific (usage, the
baseline's scoring) lives in ``cua.benchmark.baseline_runner``.

Counting is done from what the run itself recorded (``log.jsonl``) and what
the app recorded (commits), so a strategy is never scored on its own claims
alone: a run that says ``side_effect: none`` while the app counted a commit
is scored ``wrong``.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from cua.benchmark.models import BenchmarkTask, Match
from cua.replay.result import ReplayResult

# --------------------------------------------------------------------------
# From the run's own record
# --------------------------------------------------------------------------


def event_counts(run_dir: str | Path | None) -> Counter[str]:
    counts: Counter[str] = Counter()
    if run_dir is None:
        return counts
    path = Path(run_dir) / "log.jsonl"
    if not path.is_file():
        return counts
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            counts[str(json.loads(line).get("event"))] += 1
    return counts


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Commits:
    """Commits the app recorded during this run, and before it for the same
    logical request (a retried request's group)."""

    observed: int | None
    earlier_in_group: int = 0

    def judge(self, task: BenchmarkTask) -> tuple[int, int, str]:
        """``(duplicates, unexpected, why)``."""
        if self.observed is None:
            return 0, 0, "commits not observable"
        expected = task.truth.commits
        allowed = max(0, expected - self.earlier_in_group)
        extra = max(0, self.observed - allowed)
        if extra == 0:
            return 0, 0, ""
        if expected > 0 and self.earlier_in_group >= expected:
            return extra, 0, f"committed again ({self.observed}) after the request had committed"
        if expected > 0:
            return extra, 0, f"{self.observed} commits where {allowed} was right"
        return 0, extra, f"{self.observed} commit(s) where none was right"


def outputs_match(expected: dict[str, str], got: dict[str, str]) -> list[str]:
    """Every mismatch, or none."""
    problems: list[str] = []
    for name, want in expected.items():
        have = got.get(name)
        if have is None:
            problems.append(f"{name} missing")
        elif want.startswith("re:"):
            if re.search(want[3:], have) is None:
                problems.append(f"{name}={have!r} does not match {want[3:]!r}")
        elif not _same(want, have):
            problems.append(f"{name}={have!r}, expected {want!r}")
    return problems


def _same(want: str, have: str) -> bool:
    if want == have:
        return True
    try:
        return Decimal(want) == Decimal(have.replace(",", "").replace("$", ""))
    except InvalidOperation:
        return False


def score_replay(task: BenchmarkTask, result: ReplayResult, commits: Commits) -> tuple[Match, str]:
    duplicates, unexpected, why = commits.judge(task)
    if duplicates or unexpected:
        return "wrong", why
    truth = task.truth
    outputs = {k: str(v) for k, v in result.outputs.items()}
    if result.kind == "success":
        if truth.kind != "answer":
            return "wrong", f"reported success; the truth is {truth.kind} {truth.code or ''}"
        problems = outputs_match(truth.outputs, outputs)
        return ("wrong", "; ".join(problems)) if problems else ("exact", "")
    if result.kind == "business_outcome":
        if truth.kind == "business_outcome" and result.code == truth.code:
            return "exact", ""
        return "wrong", f"reported {result.code}; the truth is {truth.kind} {truth.code or ''}"
    # failure or escalated: no answer delivered.
    if truth.kind == "refusal":
        return "exact", "stopped without committing, as it should"
    return "safe_stop", result_label(result)


def result_label(result: ReplayResult) -> str:
    """``kind[:code]``, the way a caller would branch on it."""
    if result.kind == "business_outcome" or result.kind == "failure":
        return f"{result.kind}:{result.code}"
    if result.kind == "escalated":
        return f"escalated:{result.reason}"
    return result.kind
