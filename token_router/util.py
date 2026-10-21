"""Small dependency-free helpers shared by the router, store and CLI."""

from __future__ import annotations

import hashlib
import math
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable, Sequence

MICROS_PER_UNIT = 1_000_000
TOKENS_PER_MILLION = 1_000_000

_QUANTITY_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmb]?)\s*$", re.IGNORECASE)
_SUFFIXES = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


class UserError(ValueError):
    """An error that should be shown to the operator without a traceback."""


def generate_api_key() -> str:
    """Return a high-entropy, URL-safe downstream API key."""

    return "tr_" + secrets.token_urlsafe(32)


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def api_key_prefix(api_key: str) -> str:
    """A short, non-secret identifier that can be printed and searched."""

    return api_key[:12]


def new_request_id() -> str:
    return "req_" + secrets.token_hex(12)


def parse_token_quantity(value: str, *, allow_none: bool = True) -> int | None:
    """Parse ``100000``, ``100k``, ``1.5m`` or ``none`` into an integer."""

    text = value.strip()
    if allow_none and text.lower() in {"none", "unlimited", "off", "-"}:
        return None
    match = _QUANTITY_RE.match(text)
    if not match:
        raise UserError(f"无效的 token 数量：{value!r}（示例：100000、100k、1.5m、none）")
    amount = Decimal(match.group(1)) * _SUFFIXES[match.group(2).lower()]
    if amount != amount.to_integral_value():
        raise UserError(f"token 数量必须是整数：{value!r}")
    result = int(amount)
    if result < 0:
        raise UserError("token 数量不能为负数")
    return result


def parse_money_to_micros(value: str) -> int:
    """Parse an amount such as ``2.50`` into integer millionths."""

    try:
        amount = Decimal(value.strip())
    except InvalidOperation as exc:
        raise UserError(f"无效的金额：{value!r}") from exc
    if not amount.is_finite() or amount < 0:
        raise UserError("金额必须是大于或等于 0 的数字")
    micros = int((amount * MICROS_PER_UNIT).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return micros


def parse_price_to_micros_per_mtok(value: str) -> int:
    """Parse a price per 1M tokens into integer micro-units per 1M tokens."""

    return parse_money_to_micros(value)


def cost_micros(input_tokens: int, output_tokens: int, input_price: int, output_price: int) -> int:
    """Cost in integer micro-units, rounded up so tiny calls are not free."""

    numerator = input_tokens * input_price + output_tokens * output_price
    if numerator <= 0:
        return 0
    return (numerator + TOKENS_PER_MILLION - 1) // TOKENS_PER_MILLION


def format_amount(micros: int, symbol: str = "$") -> str:
    """Render integer micro-units without binary floating point noise."""

    amount = Decimal(int(micros)) / Decimal(MICROS_PER_UNIT)
    text = f"{amount:.6f}".rstrip("0").rstrip(".")
    if text == "-0":
        text = "0"
    return f"{symbol}{text}"


def format_price_per_mtok(micros_per_mtok: int, symbol: str = "$") -> str:
    return f"{format_amount(micros_per_mtok, symbol)}/M"


def format_int(value: int) -> str:
    return f"{int(value):,}"


@dataclass(frozen=True)
class PeriodBounds:
    day_start: int
    month_start: int
    next_day: int
    next_month: int


def period_bounds(timestamp: float | int, utc_offset_minutes: int) -> PeriodBounds:
    """Return local-day and local-month boundaries as UTC epoch seconds.

    A fixed UTC offset is used deliberately: it needs no tzdata dependency and
    makes quota reset times stable for a single deployment.
    """

    tz = timezone(timedelta(minutes=utc_offset_minutes))
    local = datetime.fromtimestamp(timestamp, tz=tz)
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    month = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if month.month == 12:
        next_month = month.replace(year=month.year + 1, month=1)
    else:
        next_month = month.replace(month=month.month + 1)
    return PeriodBounds(
        day_start=int(day.timestamp()),
        month_start=int(month.timestamp()),
        next_day=int((day + timedelta(days=1)).timestamp()),
        next_month=int(next_month.timestamp()),
    )


def format_timestamp(timestamp: int | float | None, utc_offset_minutes: int) -> str:
    if timestamp is None:
        return "-"
    tz = timezone(timedelta(minutes=utc_offset_minutes))
    return datetime.fromtimestamp(timestamp, tz=tz).strftime("%Y-%m-%d %H:%M:%S")


def retry_after_seconds(reset_at: int, now: int | None = None) -> int:
    if now is None:
        now = int(datetime.now().timestamp())
    return max(1, int(reset_at - now))


def display_width(text: str) -> int:
    width = 0
    for char in text:
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def _pad(text: str, width: int, right: bool = False) -> str:
    padding = max(0, width - display_width(text))
    return (" " * padding + text) if right else (text + " " * padding)


def format_table(headers: Sequence[str], rows: Iterable[Sequence[object]], *, numeric: Sequence[int] = ()) -> str:
    """Render a compact plain-text table that aligns CJK names reasonably."""
    numeric_set = set(numeric)
    rendered = [[str(cell if cell is not None else "-") for cell in row] for row in rows]
    widths = [display_width(str(header)) for header in headers]
    for row in rendered:
        for index, cell in enumerate(row):
            if index < len(widths):
                widths[index] = max(widths[index], display_width(cell))
    header_line = "  ".join(
        _pad(str(header), widths[index], index in numeric_set)
        for index, header in enumerate(headers)
    )
    separator = "  ".join("-" * width for width in widths)
    lines = [header_line, separator]
    for row in rendered:
        lines.append(
            "  ".join(
                _pad(cell, widths[index], index in numeric_set)
                for index, cell in enumerate(row)
            )
        )
    return "\n".join(lines)


def is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    return normalized in {"127.0.0.1", "localhost", "::1"} or normalized.startswith("127.")


def estimate_text_tokens(text: str) -> int:
    """Cheap tokenizer-free estimate used only for quota reservation/fallback.

    It intentionally over-estimates CJK a little and ASCII a reasonable amount.
    Actual provider-reported usage always wins over this estimate.
    """

    if not text:
        return 0
    ascii_count = 0
    wide_count = 0
    other_count = 0
    for char in text:
        if char.isascii():
            if not char.isspace():
                ascii_count += 1
        elif unicodedata.east_asian_width(char) in {"W", "F"}:
            wide_count += 1
        else:
            other_count += 1
    estimate = ascii_count / 4.0 + wide_count * 1.05 + other_count / 2.0
    return max(1, math.ceil(estimate))
