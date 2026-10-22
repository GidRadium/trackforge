"""Resource budgets with estimate and actual accounting.

A zero value in an :class:`~trackforge.domain.model.capabilities.AnalysisProfile`
budget means "unlimited"; this matches the domain default of ``Cost()``.  CPU,
network, and money are cumulative for one run.  Memory is a peak/per-step
limit because two sequential steps do not use memory concurrently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trackforge.application.ports.execution import BudgetSnapshot
from trackforge.domain.errors import DomainValidationError
from trackforge.domain.model._validation import ensure_float, ensure_int
from trackforge.domain.model.capabilities import AnalysisProfile, Cost
from trackforge.pipeline.errors import BudgetExceeded


def _limit(value: object, field_name: str) -> float | None:
    if value is None:
        return None
    number = ensure_float(value, field_name, minimum=0.0)
    return number


def _limit_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    return ensure_int(value, field_name, minimum=0)


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    """Immutable snapshot of consumed resources."""

    cpu_seconds: float = 0.0
    memory_mb_peak: float = 0.0
    network_calls: int = 0
    money_usd: float = 0.0


class Budget:
    """Mutable budget accounting for one run."""

    def __init__(
        self,
        limits: Cost | None = None,
        *,
        max_cpu_seconds: float | None = None,
        max_memory_mb: float | None = None,
        max_network_calls: int | None = None,
        max_cost_usd: float | None = None,
    ) -> None:
        if limits is not None and not isinstance(limits, Cost):
            raise DomainValidationError("limits must be a Cost")
        base_cpu = None if limits is None or limits.cpu_seconds == 0 else limits.cpu_seconds
        base_memory = None if limits is None or limits.memory_mb == 0 else limits.memory_mb
        base_network = None if limits is None or limits.network_calls == 0 else limits.network_calls
        base_money = None if limits is None or limits.money_usd == 0 else limits.money_usd
        self._max_cpu = _limit(
            base_cpu if max_cpu_seconds is None else max_cpu_seconds,
            "max_cpu_seconds",
        )
        self._max_memory = _limit(
            base_memory if max_memory_mb is None else max_memory_mb,
            "max_memory_mb",
        )
        self._max_network = _limit_int(
            base_network if max_network_calls is None else max_network_calls,
            "max_network_calls",
        )
        self._max_money = _limit(
            base_money if max_cost_usd is None else max_cost_usd,
            "max_cost_usd",
        )
        self._used_cpu = 0.0
        self._used_memory_peak = 0.0
        self._used_network = 0
        self._used_money = 0.0

    # -- construction -------------------------------------------------

    @classmethod
    def unlimited(cls) -> Budget:
        return cls()

    @classmethod
    def from_cost(cls, limits: Cost) -> Budget:
        return cls(limits)

    @classmethod
    def from_profile(cls, profile: AnalysisProfile) -> Budget:
        return cls(profile.budget if isinstance(profile.budget, Cost) else Cost())

    # -- limits and usage ---------------------------------------------

    @property
    def max_cpu_seconds(self) -> float | None:
        return self._max_cpu

    @property
    def max_memory_mb(self) -> float | None:
        return self._max_memory

    @property
    def max_network_calls(self) -> int | None:
        return self._max_network

    @property
    def max_cost_usd(self) -> float | None:
        return self._max_money

    @property
    def used_cpu_seconds(self) -> float:
        return self._used_cpu

    @property
    def used_memory_mb_peak(self) -> float:
        return self._used_memory_peak

    @property
    def used_network_calls(self) -> int:
        return self._used_network

    @property
    def used_money_usd(self) -> float:
        return self._used_money

    @property
    def used(self) -> BudgetUsage:
        return BudgetUsage(
            cpu_seconds=self._used_cpu,
            memory_mb_peak=self._used_memory_peak,
            network_calls=self._used_network,
            money_usd=self._used_money,
        )

    @property
    def is_unlimited(self) -> bool:
        return (
            self._max_cpu is None
            and self._max_memory is None
            and self._max_network is None
            and self._max_money is None
        )

    @property
    def exhausted(self) -> bool:
        return (
            self._max_cpu is not None
            and self._used_cpu >= self._max_cpu
            or self._max_memory is not None
            and self._used_memory_peak >= self._max_memory
            or self._max_network is not None
            and self._used_network >= self._max_network
            or self._max_money is not None
            and self._used_money >= self._max_money
        )

    def limit_cost(self) -> Cost:
        """Return limits as a ``Cost`` (zero means unlimited)."""

        return Cost(
            cpu_seconds=self._max_cpu or 0.0,
            memory_mb=self._max_memory or 0.0,
            network_calls=self._max_network or 0,
            money_usd=self._max_money or 0.0,
        )

    def remaining_cost(self) -> Cost:
        """Return remaining cumulative budget (unlimited fields are zero)."""

        return Cost(
            cpu_seconds=(
                0.0 if self._max_cpu is None else max(0.0, self._max_cpu - self._used_cpu)
            ),
            memory_mb=(
                0.0
                if self._max_memory is None
                else max(0.0, self._max_memory - self._used_memory_peak)
            ),
            network_calls=(
                0 if self._max_network is None else max(0, self._max_network - self._used_network)
            ),
            money_usd=(
                0.0 if self._max_money is None else max(0.0, self._max_money - self._used_money)
            ),
        )

    def snapshot(self) -> BudgetSnapshot:
        """Return the limits visible to a capability for its next attempt."""

        return BudgetSnapshot(
            cpu_seconds=(
                None if self._max_cpu is None else max(0.0, self._max_cpu - self._used_cpu)
            ),
            memory_mb=self._max_memory,
            network_calls=(
                None
                if self._max_network is None
                else max(0, self._max_network - self._used_network)
            ),
            money_usd=(
                None if self._max_money is None else max(0.0, self._max_money - self._used_money)
            ),
        )

    def clone(self) -> Budget:
        """Return an independent copy, useful for dry-run planning."""

        clone = Budget(
            max_cpu_seconds=self._max_cpu,
            max_memory_mb=self._max_memory,
            max_network_calls=self._max_network,
            max_cost_usd=self._max_money,
        )
        clone._used_cpu = self._used_cpu
        clone._used_memory_peak = self._used_memory_peak
        clone._used_network = self._used_network
        clone._used_money = self._used_money
        return clone

    def reset(self) -> None:
        """Reset usage while preserving limits (new run on the same executor)."""

        self._used_cpu = 0.0
        self._used_memory_peak = 0.0
        self._used_network = 0
        self._used_money = 0.0

    # -- accounting ---------------------------------------------------

    def can_afford(self, cost: Cost) -> bool:
        """Return whether ``cost`` fits the cumulative and peak limits."""

        if not isinstance(cost, Cost):
            raise DomainValidationError("cost must be a Cost")
        if self._max_cpu is not None and self._used_cpu + cost.cpu_seconds > self._max_cpu:
            return False
        if self._max_memory is not None and cost.memory_mb > self._max_memory:
            return False
        if (
            self._max_network is not None
            and self._used_network + cost.network_calls > self._max_network
        ):
            return False
        return not (
            self._max_money is not None and self._used_money + cost.money_usd > self._max_money
        )

    def check(self, cost: Cost, *, capability_id: str | None = None) -> None:
        """Raise :class:`BudgetExceeded` if ``cost`` cannot be afforded."""

        if self.can_afford(cost):
            return
        raise BudgetExceeded(
            "step estimate exceeds the remaining budget",
            context={
                "capability_id": capability_id,
                "estimate": _cost_dict(cost),
                "remaining": _cost_dict(self.remaining_cost()),
            },
        )

    def estimate(self, cost: Cost) -> bool:
        """Alias for :meth:`can_afford` used by planning call sites."""

        return self.can_afford(cost)

    def consume(self, actual: Cost) -> None:
        """Record actual resource use after an attempt."""

        if not isinstance(actual, Cost):
            raise DomainValidationError("actual cost must be a Cost")
        self._used_cpu += actual.cpu_seconds
        self._used_memory_peak = max(self._used_memory_peak, actual.memory_mb)
        self._used_network += actual.network_calls
        self._used_money += actual.money_usd

    def record_actual(self, actual: Cost) -> None:
        """Alias for :meth:`consume` used by executor call sites."""

        self.consume(actual)

    def add(
        self,
        *,
        cpu_seconds: float = 0.0,
        network_calls: int = 0,
        money_usd: float = 0.0,
    ) -> None:
        """Convenience helper for callers that do not build a ``Cost``."""

        self.consume(
            Cost(
                cpu_seconds=cpu_seconds,
                network_calls=network_calls,
                money_usd=money_usd,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "limits": _cost_dict(self.limit_cost()),
            "used": {
                "cpu_seconds": self._used_cpu,
                "memory_mb_peak": self._used_memory_peak,
                "network_calls": self._used_network,
                "money_usd": self._used_money,
            },
            "snapshot": {
                "cpu_seconds": self.snapshot().cpu_seconds,
                "memory_mb": self.snapshot().memory_mb,
                "network_calls": self.snapshot().network_calls,
                "money_usd": self.snapshot().money_usd,
            },
        }


def _cost_dict(cost: Cost) -> dict[str, float | int]:
    return {
        "cpu_seconds": cost.cpu_seconds,
        "memory_mb": cost.memory_mb,
        "network_calls": cost.network_calls,
        "money_usd": cost.money_usd,
    }


__all__ = ["Budget", "BudgetUsage"]
