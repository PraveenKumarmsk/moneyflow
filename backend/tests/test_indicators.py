import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import indicators as ind  # noqa: E402


def test_rsi_all_up_is_100():
    assert ind.rsi(list(range(1, 40)), 14) == 100.0


def test_rsi_all_down_is_0():
    assert ind.rsi(list(range(40, 1, -1)), 14) == pytest.approx(0.0)


def test_rsi_wilder_reference():
    # Classic Wilder/StockCharts worked example, first RSI(14) value = 70.46
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
              45.89, 46.03, 45.61, 46.28, 46.28]
    assert ind.rsi(closes, 14) == pytest.approx(70.53, abs=0.1)


def test_rsi_insufficient_data():
    assert ind.rsi([1, 2, 3], 14) is None


def test_ema_seed_is_sma():
    s = ind.ema_series([1, 2, 3, 4, 5], 3)
    assert s[2] == pytest.approx(2.0)
    assert s[3] == pytest.approx(3.0)


def test_vwap():
    c = [{"h": 10, "l": 10, "c": 10, "v": 100}, {"h": 20, "l": 20, "c": 20, "v": 300}]
    assert ind.vwap(c) == pytest.approx(17.5)
    assert ind.vwap([{"h": 1, "l": 1, "c": 1, "v": 0}]) is None


def test_max_pain():
    rows = [{"strike": 100, "ce_oi": 10, "pe_oi": 50},
            {"strike": 110, "ce_oi": 30, "pe_oi": 30},
            {"strike": 120, "ce_oi": 50, "pe_oi": 10}]
    assert ind.max_pain(rows) == 110


def test_pcr():
    rows = [{"ce_oi": 100, "pe_oi": 150, "ce_vol": 10, "pe_vol": 5}]
    assert ind.pcr(rows, "oi") == pytest.approx(1.5)
    assert ind.pcr(rows, "volume") == pytest.approx(0.5)
    assert ind.pcr([{"ce_oi": 0, "pe_oi": 5}]) is None


@pytest.mark.parametrize("p,o,exp", [(1, 1, "Long buildup"), (-1, 1, "Short buildup"),
                                     (1, -1, "Short covering"), (-1, -1, "Long unwinding"),
                                     (0, 5, "Neutral"), (1, 0, "Neutral")])
def test_classify_oi(p, o, exp):
    assert ind.classify_oi(p, o) == exp


def test_classify_oi_thresholds():
    assert ind.classify_oi(0.05, 2, price_thr=0.1) == "Neutral"
    assert ind.classify_oi(0.2, 2, price_thr=0.1, oi_thr=0.5) == "Long buildup"


def test_position_size_basic():
    r = ind.position_size(500000, 1, 100, 95)
    assert r["risk_amount"] == pytest.approx(5000)
    assert r["qty"] == 1000 and r["actual_risk"] == pytest.approx(5000)


def test_position_size_lots_and_margin():
    r = ind.position_size(500000, 1, 100, 95, lot_size=300)
    assert r["lots"] == 3 and r["qty"] == 900 and r["actual_risk"] == pytest.approx(4500)
    r = ind.position_size(500000, 1, 100, 95, lot_size=300, margin_per_lot=100000, available_margin=150000)
    assert r["lots"] == 1 and r["limited_by"] == "available margin"


def test_position_size_too_small_and_invalid():
    assert ind.position_size(10000, 0.5, 100, 90, lot_size=100)["lots"] == 0
    with pytest.raises(ValueError):
        ind.position_size(10000, 1, 100, 100)


def test_pivots_cpr():
    p = ind.pivots(110, 90, 100)
    assert p["P"] == pytest.approx(100) and p["R1"] == pytest.approx(110) and p["S1"] == pytest.approx(90)
    assert p["BC"] <= p["TC"]


def test_volume_profile_poc_in_heaviest_zone():
    c = [{"t": i, "o": 100, "h": 101, "l": 99, "c": 100, "v": 1000} for i in range(10)]
    c += [{"t": 20, "o": 110, "h": 111, "l": 109, "c": 110, "v": 10}]
    vp = ind.volume_profile(c, bins=20)
    assert 99 <= vp["poc"] <= 101 and vp["val"] <= vp["poc"] <= vp["vah"]


def test_resample_5m():
    c = [{"t": 300 * 10 + 60 * i, "o": i, "h": i + 1, "l": i - 1, "c": i, "v": 10} for i in range(10)]
    r = ind.resample(c, 5)
    assert len(r) == 2 and r[0]["v"] == 50 and r[0]["o"] == 0 and r[0]["c"] == 4 and r[0]["h"] == 5


def test_adx_trending_high_flat_low():
    up = [float(i) for i in range(80)]
    assert ind.adx([x + 1 for x in up], [x - 1 for x in up], up) > 40
    flat_h = [10 + (1 if i % 2 else -1) * 0.5 for i in range(80)]
    flat_l = [h - 1 for h in flat_h]
    assert ind.adx(flat_h, flat_l, [h - 0.5 for h in flat_h]) < 25


def test_bs_put_call_parity():
    c = ind.bs_greeks(100, 100, 0.25, 0.2, True)["price"]
    p = ind.bs_greeks(100, 100, 0.25, 0.2, False)["price"]
    import math
    assert c - p == pytest.approx(100 - 100 * math.exp(-0.065 * 0.25), abs=1e-6)


def test_macd_none_when_short():
    assert ind.macd([1, 2, 3]) is None
    assert ind.macd([float(i) for i in range(60)])["hist"] is not None
