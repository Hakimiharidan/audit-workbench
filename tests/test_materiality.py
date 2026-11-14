from decimal import Decimal as D

import pytest

from workbench.materiality import Materiality, aggregate, band, sort_key

M = Materiality.from_config({"materiality": {"overall": 50000, "performance_pct": 0.75, "clearly_trivial_pct": 0.05}})


def test_thresholds_exact():
    assert (M.overall, M.performance, M.trivial) == (D("50000.00"), D("37500.00"), D("2500.00"))


@pytest.mark.parametrize("amount,expected", [
    (D("50000"), "above_materiality"), (D("49999.99"), "above_performance"), (D("37500"), "above_performance"),
    (D("37499.99"), "above_trivial"), (D("2500.01"), "above_trivial"), (D("2500"), "trivial"),
    (D("0"), "trivial"), (None, "no_amount"), (D("-60000"), "above_materiality"),
])
def test_band_boundaries(amount, expected):
    assert band(amount, M) == expected


def test_sort_key_orders_band_then_amount_then_id():
    rows = [{"exception_id": "b", "amount_at_risk": D("10")}, {"exception_id": "a", "amount_at_risk": None},
            {"exception_id": "c", "amount_at_risk": D("60000")}, {"exception_id": "a2", "amount_at_risk": D("10")}]
    for r in rows:
        r["band"] = band(r["amount_at_risk"], M)
    assert [r["exception_id"] for r in sorted(rows, key=sort_key)] == ["c", "a2", "b", "a"]


def test_aggregate_many_small_items_can_be_material():
    rows = [{"amount_at_risk": D("2000"), "band": "trivial"} for _ in range(30)]
    a = aggregate(rows, M)
    assert a == {"count": 30, "total": D("60000"), "highest_band": "trivial", "aggregate_band": "above_materiality"}


def test_aggregate_empty_and_control_only():
    assert aggregate([], M)["highest_band"] is None
    a = aggregate([{"amount_at_risk": None, "band": "no_amount"}], M)
    assert a["total"] == D("0") and a["highest_band"] == "no_amount" and a["aggregate_band"] == "no_amount"


def test_config_rejects_bad_values():
    with pytest.raises(ValueError):
        Materiality.from_config({"materiality": {"overall": -1}})
    with pytest.raises(ValueError):
        Materiality.from_config({})
