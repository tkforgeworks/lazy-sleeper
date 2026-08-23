"""LS-53: the pre-game freeze calendar and row partitioning. DB-free."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from lazy_sleeper.ingest.stat_loaders import frozen, partition_frozen, week_kickoff


def test_week_kickoff_is_the_thursday_after_labor_day() -> None:
    # 2025: Labor Day Mon Sep 1 → week 1 Thu Sep 4. 2026: Labor Day Sep 7 → week 1 Thu Sep 10.
    assert week_kickoff(2025, 1) == datetime(2025, 9, 4, tzinfo=UTC)
    assert week_kickoff(2026, 1) == datetime(2026, 9, 10, tzinfo=UTC)
    assert week_kickoff(2026, 2) == datetime(2026, 9, 17, tzinfo=UTC)
    assert week_kickoff(2026, 18) == week_kickoff(2026, 1) + timedelta(weeks=17)
    # 2024: Sep 1 was a Sunday → Labor Day Sep 2 → week 1 Thu Sep 5
    assert week_kickoff(2024, 1) == datetime(2024, 9, 5, tzinfo=UTC)


def test_frozen_by_pull_time_not_load_time() -> None:
    pre = datetime(2026, 9, 9, 23, 59, tzinfo=UTC)
    post = datetime(2026, 9, 10, 0, 0, tzinfo=UTC)
    assert not frozen(2026, 1, pre) and frozen(2026, 1, post)
    # season totals (week None) freeze at week-1 kickoff
    assert not frozen(2026, None, pre) and frozen(2026, None, post)
    # a later week is still live while week 1 is frozen
    assert not frozen(2026, 2, post)
    assert not frozen(2026, 1, None)  # no pull time known → never freeze


def test_partition_frozen_splits_by_scope_and_thaw_bypasses() -> None:
    at = datetime(2026, 9, 12, tzinfo=UTC)  # after wk-1 kickoff, before wk 2
    rows = [
        {"season": 2026, "week": 1, "id": "a"},
        {"season": 2026, "week": 2, "id": "b"},
        {"season": 2026, "week": None, "id": "c"},  # season total — frozen with week 1
        {"season": 2025, "week": 10, "id": "d"},  # last season — long frozen
    ]
    live, ice = partition_frozen(rows, at)
    assert [r["id"] for r in live] == ["b"] and [r["id"] for r in ice] == ["a", "c", "d"]
    live, ice = partition_frozen(rows, at, thaw=True)
    assert len(live) == 4 and ice == []
    live, ice = partition_frozen(rows, None)
    assert len(live) == 4 and ice == []
