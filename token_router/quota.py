"""Concurrent-safe quota reservation and settlement.

Quotas are checked before forwarding. Because the final output size is not known
in advance, each request reserves the estimated prompt plus its requested
``max_tokens`` (or a configured default). Actual usage is reconciled afterwards.
This prevents concurrent requests from racing past a quota without imposing a
hard mid-stream cutoff; a single response can still overshoot its reservation if
the provider generates more than requested.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from .config import Config
from .db import ApiKey, Database, ResolvedPrice
from .usage import (
    TokenUsage,
    is_billable_path,
    is_embeddings_path,
    estimate_prompt_tokens,
    requested_output_tokens,
)
from .util import PeriodBounds, cost_micros, period_bounds


@dataclass(frozen=True)
class Reservation:
    key: ApiKey
    bounds: PeriodBounds
    admitted_at: int
    input_tokens_estimate: int
    output_tokens_reserved: int
    cost_micros_reserved: int
    metered: bool
    price: ResolvedPrice
    model: str | None = None


@dataclass(frozen=True)
class SettlementResult:
    usage: TokenUsage
    cost_micros: int


@dataclass(frozen=True)
class QuotaViolation:
    period: str
    kind: str
    limit: int
    used: int
    reserved: int
    requested: int
    reset_at: int

    def details(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "kind": self.kind,
            "limit": self.limit,
            "used": self.used,
            "reserved": self.reserved,
            "requested": self.requested,
            "reset_at": self.reset_at,
        }


class QuotaExceeded(Exception):
    def __init__(self, violation: QuotaViolation) -> None:
        self.violation = violation
        label = "daily" if violation.period == "day" else "monthly"
        unit = "token" if violation.kind == "tokens" else "cost"
        super().__init__(
            f"{label} {unit} quota exceeded: limit={violation.limit}, "
            f"used={violation.used}, reserved={violation.reserved}, "
            f"requested={violation.requested}"
        )


@dataclass
class _ReservedAggregate:
    tokens: int = 0
    cost_micros: int = 0


class QuotaManager:
    def __init__(self, db: Database, config: Config) -> None:
        self.db = db
        self.config = config
        self._lock = threading.Lock()
        self._reserved: dict[int, _ReservedAggregate] = {}

    def reserve(
        self,
        key: ApiKey,
        payload: dict[str, Any] | None,
        path: str,
        *,
        now: int | None = None,
    ) -> Reservation:
        timestamp = int(now if now is not None else time.time())
        bounds = period_bounds(timestamp, self.config.timezone_offset_minutes)
        metered = is_billable_path(path) and payload is not None
        model = payload.get("model") if payload is not None else None
        if isinstance(model, str):
            stripped = model.strip()
            model = stripped if 0 < len(stripped) <= 200 and "\n" not in stripped and "\r" not in stripped else None
        else:
            model = None
        if not metered:
            return Reservation(
                key=key,
                bounds=bounds,
                admitted_at=timestamp,
                input_tokens_estimate=0,
                output_tokens_reserved=0,
                cost_micros_reserved=0,
                metered=False,
                price=ResolvedPrice.from_key(key, model),
                model=model,
            )

        assert payload is not None
        price = self.db.resolve_price(key, model)
        input_estimate = estimate_prompt_tokens(payload)
        output_reserve = 0 if is_embeddings_path(path) else requested_output_tokens(
            payload, path, self.config.default_max_output_tokens
        )
        token_reserve = input_estimate + output_reserve
        # The cached-token share is unknown before the response. Reserve all
        # prompt tokens at the higher of input/cache price so a cache price
        # above the normal input price cannot bypass the cost quota.
        reserved_input_price = max(
            price.input_micros_per_mtok,
            price.cached_input_micros_per_mtok,
        )
        cost_reserve = cost_micros(
            input_estimate,
            output_reserve,
            reserved_input_price,
            price.output_micros_per_mtok,
        )
        has_limits = any(
            limit is not None
            for limit in (
                key.daily_token_limit,
                key.monthly_token_limit,
                key.daily_cost_limit_micros,
                key.monthly_cost_limit_micros,
            )
        )
        reservation = Reservation(
            key=key,
            bounds=bounds,
            admitted_at=timestamp,
            input_tokens_estimate=input_estimate,
            output_tokens_reserved=output_reserve,
            cost_micros_reserved=cost_reserve,
            metered=True,
            price=price,
            model=model,
        )
        if not has_limits:
            return reservation

        with self._lock:
            day_usage, month_usage = self.db.get_period_usage(key.id, bounds)
            aggregate = self._reserved.setdefault(key.id, _ReservedAggregate())
            try:
                for period, usage, reset_at in (
                    ("day", day_usage, bounds.next_day),
                    ("month", month_usage, bounds.next_month),
                ):
                    token_limit = key.token_limit(period)
                    if token_limit is not None:
                        projected = usage.total_tokens + aggregate.tokens + token_reserve
                        if projected > token_limit:
                            raise QuotaExceeded(
                                QuotaViolation(
                                    period=period,
                                    kind="tokens",
                                    limit=token_limit,
                                    used=usage.total_tokens,
                                    reserved=aggregate.tokens,
                                    requested=token_reserve,
                                    reset_at=reset_at,
                                )
                            )
                    cost_limit = key.cost_limit(period)
                    if cost_limit is not None:
                        projected = usage.cost_micros + aggregate.cost_micros + cost_reserve
                        if projected > cost_limit:
                            raise QuotaExceeded(
                                QuotaViolation(
                                    period=period,
                                    kind="cost",
                                    limit=cost_limit,
                                    used=usage.cost_micros,
                                    reserved=aggregate.cost_micros,
                                    requested=cost_reserve,
                                    reset_at=reset_at,
                                )
                            )
            except BaseException:
                if aggregate.tokens == 0 and aggregate.cost_micros == 0:
                    self._reserved.pop(key.id, None)
                raise
            aggregate.tokens += token_reserve
            aggregate.cost_micros += cost_reserve
            return reservation

    def settle(
        self,
        reservation: Reservation,
        *,
        request_id: str,
        endpoint: str,
        model: str | None,
        status: int,
        usage: TokenUsage,
        latency_ms: int,
        client_ip: str | None,
        error: str | None = None,
        timestamp: int | None = None,
    ) -> SettlementResult:
        """Persist the request ledger and release the reservation exactly once."""

        if not reservation.metered:
            effective_usage = TokenUsage()
            effective_cost = 0
        elif 200 <= status < 300:
            effective_usage = usage
            effective_cost = cost_micros(
                usage.input_tokens,
                usage.output_tokens,
                reservation.price.input_micros_per_mtok,
                reservation.price.output_micros_per_mtok,
                usage.cached_input_tokens,
                reservation.price.cached_input_micros_per_mtok,
            )
        else:
            effective_usage = TokenUsage()
            effective_cost = 0

        with self._lock:
            try:
                self.db.record_request(
                    request_id=request_id,
                    key=reservation.key,
                    timestamp=int(time.time()) if timestamp is None else timestamp,
                    endpoint=endpoint,
                    model=model,
                    status=status,
                    usage=effective_usage,
                    cost_micros=effective_cost,
                    latency_ms=latency_ms,
                    client_ip=client_ip,
                    error=error,
                    bounds=reservation.bounds,
                )
            finally:
                if reservation.metered:
                    aggregate = self._reserved.get(reservation.key.id)
                    if aggregate is not None:
                        aggregate.tokens = max(0, aggregate.tokens - (
                            reservation.input_tokens_estimate + reservation.output_tokens_reserved
                        ))
                        aggregate.cost_micros = max(
                            0, aggregate.cost_micros - reservation.cost_micros_reserved
                        )
                        if aggregate.tokens == 0 and aggregate.cost_micros == 0:
                            self._reserved.pop(reservation.key.id, None)
        return SettlementResult(usage=effective_usage, cost_micros=effective_cost)

    def reserved_for(self, key_id: int) -> tuple[int, int]:
        with self._lock:
            aggregate = self._reserved.get(key_id)
            if aggregate is None:
                return 0, 0
            return aggregate.tokens, aggregate.cost_micros
