"""Materiality bands for exceptions and per-run aggregates (all Decimal)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

BAND_ORDER = ["above_materiality", "above_performance", "above_trivial", "trivial", "no_amount"]
CENT = Decimal("0.01")


@dataclass(frozen=True)
class Materiality:
    overall: Decimal
    performance: Decimal
    trivial: Decimal
    performance_pct: Decimal
    clearly_trivial_pct: Decimal

    @classmethod
    def from_config(cls, cfg: dict) -> "Materiality":
        m = (cfg or {}).get("materiality")
        if not isinstance(m, dict) or "overall" not in m:
            raise ValueError("config needs materiality.overall (and optionally performance_pct, clearly_trivial_pct)")
        overall = Decimal(str(m["overall"]))
        perf = Decimal(str(m.get("performance_pct", "0.75")))
        triv = Decimal(str(m.get("clearly_trivial_pct", "0.05")))
        if overall <= 0 or not (0 < perf <= 1) or not (0 <= triv < perf):
            raise ValueError("materiality: overall must be > 0, 0 < performance_pct <= 1, 0 <= clearly_trivial_pct < performance_pct")
        q = lambda x: x.quantize(CENT, rounding=ROUND_HALF_UP)
        return cls(q(overall), q(overall * perf), q(overall * triv), perf, triv)


def band(amount: Decimal | None, m: Materiality) -> str:
    if amount is None:
        return "no_amount"
    a = abs(Decimal(amount))
    if a >= m.overall:
        return "above_materiality"
    if a >= m.performance:
        return "above_performance"
    if a > m.trivial:
        return "above_trivial"
    return "trivial"


def sort_key(exc: dict) -> tuple:
    amt = exc.get("amount_at_risk")
    return (BAND_ORDER.index(exc["band"]), -abs(amt) if amt is not None else Decimal(0), exc["exception_id"])


def aggregate(exceptions: list[dict], m: Materiality) -> dict:
    total = sum((abs(e["amount_at_risk"]) for e in exceptions if e.get("amount_at_risk") is not None), Decimal(0))
    bands = [e["band"] for e in exceptions]
    highest = min(bands, key=BAND_ORDER.index) if bands else None
    has_amount = any(e.get("amount_at_risk") is not None for e in exceptions)
    return {
        "count": len(exceptions),
        "total": total,
        "highest_band": highest,
        "aggregate_band": band(total, m) if has_amount else ("no_amount" if exceptions else None),
    }
