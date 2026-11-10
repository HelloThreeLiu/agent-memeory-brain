"""P0-2 / INV5：9.3 的 4×4=16 格路由表逐格断言。

这 16 格是三轮评审查出 4 个洞的地方，清单式人工核对不够用——必须逐格跑。
"""

from __future__ import annotations

import pytest

from nebula.core.assertion import rank
from nebula.core.conflict import (
    ACTION_ARBITRATE,
    ACTION_CONTRADICT,
    ACTION_MERGE,
    ACTION_SUPERSEDE_NEW,
    ACTION_SUPERSEDE_OLD,
    ACTION_UNRELATED,
    FactView,
    route,
)

TYPES = ("decision", "observation", "claim", "hypothesis")
EARLIER = "2026-01-01T00:00:00+00:00"
LATER = "2026-02-01T00:00:00+00:00"


def make(
    assertion_type: str,
    *,
    content: str = "取值 B",
    seq: int = 2,
    role: str = "agent",
    scope: str = "global",
    project_id: str | None = None,
    valid_from: str | None = LATER,
) -> FactView:
    return FactView(
        id=f"mem-{seq}",
        assertion_type=assertion_type,
        content=content,
        scope=scope,
        project_id=project_id,
        role=role,
        seq=seq,
        valid_from=valid_from,
    )


def expected_state(new_type: str, old_type: str) -> str:
    if new_type == "hypothesis":
        return "A_prime"
    if new_type == "claim":
        return "A"
    return "B" if rank(new_type) >= rank(old_type) else "C"


@pytest.mark.parametrize("new_type", TYPES)
@pytest.mark.parametrize("old_type", TYPES)
def test_sixteen_cells_are_exhaustive(new_type: str, old_type: str) -> None:
    new = make(new_type, seq=2, valid_from=LATER)
    old = make(old_type, seq=1, content="取值 A", valid_from=EARLIER)
    decision = route(new, old)
    assert decision.state == expected_state(new_type, old_type)


def test_states_cover_all_four_and_are_mutually_exclusive() -> None:
    states = {
        route(make(new, seq=2), make(old, seq=1, content="取值 A")).state
        for new in TYPES
        for old in TYPES
    }
    assert states == {"A_prime", "A", "B", "C"}


@pytest.mark.parametrize("old_type", TYPES)
def test_hypothesis_never_opens_arbitration(old_type: str) -> None:
    decision = route(make("hypothesis"), make(old_type, seq=1, content="取值 A"))
    assert decision.state == "A_prime"
    assert decision.open_arbitration is False


@pytest.mark.parametrize("old_type", TYPES)
def test_claim_never_expires_others(old_type: str) -> None:
    decision = route(make("claim"), make(old_type, seq=1, content="取值 A"))
    assert decision.action == ACTION_CONTRADICT
    assert decision.action not in (ACTION_SUPERSEDE_OLD, ACTION_SUPERSEDE_NEW)


def test_claim_cross_role_opens_arbitration() -> None:
    new = make("claim", role="reviewer")
    old = make("observation", seq=1, role="writer", content="取值 A")
    assert route(new, old).open_arbitration is True


def test_claim_same_role_does_not_open_arbitration() -> None:
    new = make("claim", role="writer")
    old = make("observation", seq=1, role="writer", content="取值 A")
    assert route(new, old).open_arbitration is False


def test_low_challenging_high_opens_arbitration() -> None:
    decision = route(make("observation"), make("decision", seq=1, content="取值 A"))
    assert decision.action == ACTION_ARBITRATE
    assert decision.open_arbitration is True


def test_different_scope_is_not_a_conflict() -> None:
    new = make("decision", scope="project", project_id="p1")
    old = make("decision", seq=1, project_id="p2")
    assert route(new, old).action == ACTION_UNRELATED


def test_content_containment_is_merge_not_conflict() -> None:
    new = make("decision", content="接口统一用 POST 方法")
    old = make("decision", seq=1, content="接口统一用 POST")
    assert route(new, old).action == ACTION_MERGE


def test_later_valid_from_expires_old() -> None:
    new = make("observation", valid_from=LATER)
    old = make("observation", seq=1, valid_from=EARLIER, content="取值 A")
    assert route(new, old).action == ACTION_SUPERSEDE_OLD


def test_earlier_valid_from_falls_into_history() -> None:
    new = make("observation", valid_from=EARLIER)
    old = make("observation", seq=1, valid_from=LATER, content="取值 A")
    assert route(new, old).action == ACTION_SUPERSEDE_NEW


def test_same_day_different_instants_compare_by_time() -> None:
    """同日但时刻不同是可比的：否则 18:00 的旧条会被 09:00 的新条"取代"，
    写出 valid_to 早于 valid_from 的空区间。"""
    new = make("observation", seq=2, valid_from="2026-03-01T09:00:00+00:00")
    old = make("observation", seq=1, valid_from="2026-03-01T18:00:00+00:00", content="取值 A")
    assert route(new, old).action == ACTION_SUPERSEDE_NEW


def test_identical_instants_fall_back_to_seq() -> None:
    new = make("observation", seq=2, valid_from="2026-03-01T09:00:00+00:00")
    old = make("observation", seq=1, valid_from="2026-03-01T09:00:00+00:00", content="取值 A")
    assert route(new, old).action == ACTION_SUPERSEDE_OLD


def test_negation_is_not_a_refinement() -> None:
    """「禁用 GET 方法」与「用 GET 方法」互相包含，但语义是反的，不能 merge。"""
    new = make("decision", content="用 GET 方法")
    old = make("decision", seq=1, content="禁用 GET 方法")
    assert route(new, old).action != ACTION_MERGE


def test_superset_with_extra_detail_is_a_refinement() -> None:
    new = make("decision", content="接口统一用 POST，超时 30 秒")
    old = make("decision", seq=1, content="接口统一用 POST")
    assert route(new, old).action == ACTION_MERGE


def test_unparsable_time_falls_back_to_seq() -> None:
    new = make("observation", seq=2, valid_from="昨天下午")
    old = make("observation", seq=1, valid_from=None, content="取值 A")
    assert route(new, old).action == ACTION_SUPERSEDE_OLD


def test_seq_fallback_can_also_put_new_into_history() -> None:
    new = make("observation", seq=1, valid_from=None)
    old = make("observation", seq=9, valid_from="昨天下午", content="取值 A")
    assert route(new, old).action == ACTION_SUPERSEDE_NEW
