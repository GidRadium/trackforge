"""Unit tests for resource budgeting."""

from __future__ import annotations

import pytest

from trackforge.domain.model.capabilities import AnalysisProfile, Cost
from trackforge.pipeline.budget import Budget
from trackforge.pipeline.errors import BudgetExceeded

pytestmark = pytest.mark.unit


def test_zero_profile_budget_means_unlimited() -> None:
    budget = Budget.from_profile(AnalysisProfile(name="test"))
    assert budget.is_unlimited
    assert budget.can_afford(Cost(cpu_seconds=10_000, network_calls=10_000))
    assert budget.snapshot().cpu_seconds is None
    assert budget.snapshot().memory_mb is None


def test_cumulative_limits() -> None:
    budget = Budget(max_cpu_seconds=10, max_network_calls=2, max_cost_usd=1.0)
    assert budget.can_afford(Cost(cpu_seconds=5, network_calls=1, money_usd=0.5))
    budget.consume(Cost(cpu_seconds=5, network_calls=1, money_usd=0.5))
    assert budget.can_afford(Cost(cpu_seconds=5, network_calls=1, money_usd=0.5))
    budget.consume(Cost(cpu_seconds=5, network_calls=1, money_usd=0.5))
    assert not budget.can_afford(Cost(cpu_seconds=0.1))
    assert not budget.can_afford(Cost(network_calls=1))
    assert not budget.can_afford(Cost(money_usd=0.1))
    assert budget.remaining_cost().cpu_seconds == 0
    assert budget.remaining_cost().network_calls == 0


def test_memory_is_a_peak_limit() -> None:
    budget = Budget(max_memory_mb=100)
    assert budget.can_afford(Cost(memory_mb=100))
    assert not budget.can_afford(Cost(memory_mb=101))
    budget.consume(Cost(memory_mb=80))
    assert budget.can_afford(Cost(memory_mb=60))
    budget.consume(Cost(memory_mb=20))
    assert budget.used_memory_mb_peak == 80
    assert budget.remaining_cost().memory_mb == 20


def test_check_raises_with_context() -> None:
    budget = Budget(max_cpu_seconds=1)
    with pytest.raises(BudgetExceeded) as raised:
        budget.check(Cost(cpu_seconds=2), capability_id="probe")
    assert raised.value.context["capability_id"] == "probe"
    assert raised.value.retryable is False


def test_exhausted_and_snapshot() -> None:
    budget = Budget(max_cpu_seconds=1, max_network_calls=1, max_cost_usd=1)
    assert not budget.exhausted
    budget.consume(Cost(cpu_seconds=1))
    assert budget.exhausted
    snapshot = budget.snapshot()
    assert snapshot.cpu_seconds == 0
    assert snapshot.exhausted
    assert snapshot.memory_mb is None


def test_clone_and_reset_are_independent() -> None:
    budget = Budget(max_cpu_seconds=5)
    budget.consume(Cost(cpu_seconds=2))
    clone = budget.clone()
    clone.consume(Cost(cpu_seconds=1))
    assert budget.used_cpu_seconds == 2
    assert clone.used_cpu_seconds == 3
    clone.reset()
    assert clone.used_cpu_seconds == 0
    assert clone.max_cpu_seconds == 5


def test_limit_cost_round_trips() -> None:
    budget = Budget(Cost(cpu_seconds=4, memory_mb=8, network_calls=2, money_usd=0.5))
    assert budget.limit_cost() == Cost(
        cpu_seconds=4,
        memory_mb=8,
        network_calls=2,
        money_usd=0.5,
    )
    payload = budget.to_dict()
    assert payload["limits"]["cpu_seconds"] == 4
