"""Parse the Markdown model-pricing table used by the deployment helper."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable

from .util import UserError

_PRICE_RE = re.compile(r"[¥￥]\s*([0-9]+(?:\.[0-9]+)?)")


@dataclass(frozen=True)
class PriceRow:
    model: str
    input_per_mtok: str
    output_per_mtok: str
    cached_per_mtok: str


def _clean_model(cell: str) -> str:
    return cell.strip().strip("*`").strip()


def _numbers(cell: str) -> list[str]:
    values: list[str] = []
    for raw in _PRICE_RE.findall(cell):
        try:
            value = Decimal(raw)
        except InvalidOperation:
            continue
        if value < 0:
            continue
        values.append(str(value))
    return values


def parse_pricing_markdown(text: str) -> list[PriceRow]:
    """Extract model, input, cached, and output prices from the Markdown table.

    Rows without a currency amount (header/separator) are ignored. A missing
    cached price defaults to the normal input price; a missing output price is
    treated as zero, matching the "未配置" rows in the source document.
    """

    rows: list[PriceRow] = []
    seen: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or stripped.count("|") < 4:
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 4:
            continue
        model = _clean_model(cells[0])
        if not model or model in {"模型名称", "模型"}:
            continue
        input_values = _numbers(cells[2])
        if not input_values:
            continue
        output_values = _numbers(cells[3])
        input_price = input_values[0]
        cached_price = input_values[1] if len(input_values) > 1 else input_price
        output_price = output_values[0] if output_values else "0"
        if model in seen:
            raise UserError(f"模型定价表中模型重复：{model}")
        seen.add(model)
        rows.append(
            PriceRow(
                model=model,
                input_per_mtok=input_price,
                output_per_mtok=output_price,
                cached_per_mtok=cached_price,
            )
        )
    if not rows:
        raise UserError("没有从定价文件中解析到任何模型价格")
    return rows


def parse_pricing_file(path: str | Path) -> list[PriceRow]:
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise UserError(f"无法读取模型定价文件 {source}：{exc}") from exc
    return parse_pricing_markdown(text)


def iter_price_lines(path: str | Path) -> Iterable[str]:
    for row in parse_pricing_file(path):
        yield f"{row.model}|{row.input_per_mtok}|{row.output_per_mtok}|{row.cached_per_mtok}"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("用法：python -m token_router.pricing <模型定价.md>", file=sys.stderr)
        return 2
    try:
        for line in iter_price_lines(args[0]):
            print(line)
    except UserError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
