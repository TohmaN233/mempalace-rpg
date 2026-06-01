"""Tier budgets for the RPG narrative memory kernel.

The kernel keeps story-importance separate from raw storage: all source
scenes remain on disk, while runtime recall budgets vary by entity tier.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RecallBudget:
    tier: str
    l0_chars: int
    l1_chars: int
    l2_chars: int
    hit_limit: int
    allow_deep_recall: bool


_BUDGETS: dict[str, RecallBudget] = {
    "core": RecallBudget(
        tier="core",
        l0_chars=1200,
        l1_chars=4800,
        l2_chars=12000,
        hit_limit=24,
        allow_deep_recall=True,
    ),
    "major": RecallBudget(
        tier="major",
        l0_chars=900,
        l1_chars=2800,
        l2_chars=7200,
        hit_limit=14,
        allow_deep_recall=True,
    ),
    "recurring": RecallBudget(
        tier="recurring",
        l0_chars=650,
        l1_chars=1000,
        l2_chars=2800,
        hit_limit=7,
        allow_deep_recall=False,
    ),
    "ambient": RecallBudget(
        tier="ambient",
        l0_chars=420,
        l1_chars=320,
        l2_chars=900,
        hit_limit=3,
        allow_deep_recall=False,
    ),
}


def budget_for_tier(tier: str | None) -> RecallBudget:
    """Return the runtime recall budget for an entity tier."""

    key = (tier or "recurring").strip().lower()
    return _BUDGETS.get(key, _BUDGETS["recurring"])
