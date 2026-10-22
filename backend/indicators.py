"""Pure calculation functions (no I/O). Covered by tests/test_indicators.py.

Candles are dicts: {"t": epoch_seconds, "o", "h", "l", "c", "v"}.
"""
from __future__ import annotations

import math
from typing import Optional, Sequence


# ---------------------------------------------------------------- moving averages
def ema_series(values: Sequence[float], period: int) -> list:
    out = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = 2 / (period + 1)
    out[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def ema(values: Sequence[float], period: int) -> Optional[float]:
    s = ema_series(values, period)
    return s[-1] if s else None


# ---------------------------------------------------------------- RSI (Wilder)
def _rsi_val(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def rsi_series(closes: Sequence[float], period: int = 14) -> list:
    out = [None] * len(closes)
    if period < 1 or len(closes) <= period:
        return out
    gain = loss = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gain += max(d, 0.0)
        loss += max(-d, 0.0)
    ag, al = gain / period, loss / period
    out[period] = _rsi_val(ag, al)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0.0)) / period
        al = (al * (period - 1) + max(-d, 0.0)) / period
        out[i] = _rsi_val(ag, al)
    return out


def rsi(closes: Sequence[float], period: int = 14) -> Optional[float]:
    s = rsi_series(closes, period)
    return s[-1] if s else None


# ---------------------------------------------------------------- MACD
def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Optional[dict]:
    ef, es = ema_series(closes, fast), ema_series(closes, slow)
    line = [(a - b) for a, b in zip(ef, es) if a is not None and b is not None]
    if len(line) < signal:
        return None
    sig = ema_series(line, signal)
    m, s = line[-1], sig[-1]
    return {"macd": m, "signal": s, "hist": m - s}


# ---------------------------------------------------------------- VWAP / ATR / ADX
def vwap(candles: Sequence[dict]) -> Optional[float]:
    pv = vol = 0.0
    for c in candles:
        v = c.get("v") or 0
        pv += (c["h"] + c["l"] + c["c"]) / 3 * v
        vol += v
    return pv / vol if vol > 0 else None


def true_ranges(highs, lows, closes) -> list:
    return [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
            for i in range(1, len(closes))]


def atr(highs, lows, closes, period: int = 14) -> Optional[float]:
    tr = true_ranges(highs, lows, closes)
    if len(tr) < period:
        return None
    a = sum(tr[:period]) / period
    for x in tr[period:]:
        a = (a * (period - 1) + x) / period
    return a


def adx(highs, lows, closes, period: int = 14) -> Optional[float]:
    n = len(closes)
    if n < 2 * period + 1:
        return None
    tr, pdm, ndm = [], [], []
    for i in range(1, n):
        up, dn = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        pdm.append(up if up > dn and up > 0 else 0.0)
        ndm.append(dn if dn > up and dn > 0 else 0.0)
        tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    s_tr, s_p, s_m = sum(tr[:period]), sum(pdm[:period]), sum(ndm[:period])

    def dx() -> float:
        pdi = 100 * s_p / s_tr if s_tr else 0.0
        mdi = 100 * s_m / s_tr if s_tr else 0.0
        tot = pdi + mdi
        return 100 * abs(pdi - mdi) / tot if tot else 0.0

    dxs = [dx()]
    for i in range(period, len(tr)):
        s_tr = s_tr - s_tr / period + tr[i]
        s_p = s_p - s_p / period + pdm[i]
        s_m = s_m - s_m / period + ndm[i]
        dxs.append(dx())
    if len(dxs) < period:
        return None
    a = sum(dxs[:period]) / period
    for d in dxs[period:]:
        a = (a * (period - 1) + d) / period
    return a


# ---------------------------------------------------------------- levels
def pivots(h: float, l: float, c: float) -> dict:
    p = (h + l + c) / 3
    bc = (h + l) / 2
    tc = 2 * p - bc
    return {"P": p, "BC": min(bc, tc), "TC": max(bc, tc), "R1": 2 * p - l, "S1": 2 * p - h,
            "R2": p + (h - l), "S2": p - (h - l)}


def swings(candles: Sequence[dict], left: int = 3, right: int = 3) -> dict:
    highs, lows = [], []
    for i in range(left, len(candles) - right):
        h, l = candles[i]["h"], candles[i]["l"]
        if all(h > candles[j]["h"] for j in range(i - left, i)) and all(h >= candles[j]["h"] for j in range(i + 1, i + right + 1)):
            highs.append((candles[i]["t"], h))
        if all(l < candles[j]["l"] for j in range(i - left, i)) and all(l <= candles[j]["l"] for j in range(i + 1, i + right + 1)):
            lows.append((candles[i]["t"], l))
    return {"highs": highs, "lows": lows}


def volume_profile(candles: Sequence[dict], bins: int = 40, va_pct: float = 0.7) -> Optional[dict]:
    candles = [c for c in candles if (c.get("v") or 0) > 0]
    if not candles:
        return None
    lo, hi = min(c["l"] for c in candles), max(c["h"] for c in candles)
    if hi <= lo:
        return None
    step = (hi - lo) / bins
    vols = [0.0] * bins
    for c in candles:
        i0 = min(int((c["l"] - lo) / step), bins - 1)
        i1 = min(int((c["h"] - lo) / step), bins - 1)
        share = c["v"] / (i1 - i0 + 1)
        for i in range(i0, i1 + 1):
            vols[i] += share
    total = sum(vols)
    poc = max(range(bins), key=lambda i: vols[i])
    a = b = poc
    area = vols[poc]
    while area < va_pct * total and (a > 0 or b < bins - 1):
        up = vols[b + 1] if b < bins - 1 else -1
        dn = vols[a - 1] if a > 0 else -1
        if up >= dn:
            b += 1
            area += up
        else:
            a -= 1
            area += dn
    return {"poc": lo + (poc + 0.5) * step, "vah": lo + (b + 1) * step, "val": lo + a * step,
            "profile": [{"price": lo + (i + 0.5) * step, "vol": v} for i, v in enumerate(vols)]}


def resample(candles: Sequence[dict], minutes: int) -> list:
    """Aggregate 1-minute candles into N-minute candles (epoch-aligned; valid for 5 and 15)."""
    sec = minutes * 60
    out, cur = [], None
    for c in candles:
        b = c["t"] - c["t"] % sec
        if cur is None or cur["t"] != b:
            if cur:
                out.append(cur)
            cur = {"t": b, "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"], "v": c.get("v") or 0}
        else:
            cur["h"] = max(cur["h"], c["h"])
            cur["l"] = min(cur["l"], c["l"])
            cur["c"] = c["c"]
            cur["v"] += c.get("v") or 0
    if cur:
        out.append(cur)
    return out


def divergence(closes: Sequence[float], rsis: Sequence, left: int = 3, right: int = 3) -> Optional[str]:
    """Candidate only: compares the last two swing lows/highs of price against RSI."""
    lows, highs = [], []
    for i in range(left, len(closes) - right):
        if rsis[i] is None:
            continue
        w = closes[i - left:i + right + 1]
        if closes[i] == min(w):
            lows.append(i)
        if closes[i] == max(w):
            highs.append(i)
    if len(lows) >= 2:
        a, b = lows[-2], lows[-1]
        if closes[b] < closes[a] and rsis[b] > rsis[a]:
            return "bullish"
    if len(highs) >= 2:
        a, b = highs[-2], highs[-1]
        if closes[b] > closes[a] and rsis[b] < rsis[a]:
            return "bearish"
    return None


# ---------------------------------------------------------------- options
def pcr(rows: Sequence[dict], method: str = "oi") -> Optional[float]:
    ck, pk = ("ce_oi", "pe_oi") if method == "oi" else ("ce_vol", "pe_vol")
    c = sum(r.get(ck) or 0 for r in rows)
    p = sum(r.get(pk) or 0 for r in rows)
    return p / c if c else None


def max_pain(rows: Sequence[dict]) -> Optional[float]:
    strikes = [r["strike"] for r in rows]
    if not strikes:
        return None
    best, best_pain = None, None
    for k in strikes:
        pain = sum((r.get("ce_oi") or 0) * max(k - r["strike"], 0) + (r.get("pe_oi") or 0) * max(r["strike"] - k, 0)
                   for r in rows)
        if best_pain is None or pain < best_pain:
            best, best_pain = k, pain
    return best


def bs_greeks(spot: float, strike: float, t_years: float, iv: float, is_call: bool, r: float = 0.065) -> dict:
    """Black-Scholes price and greeks (theta per day, vega per 1 vol point)."""
    t = max(t_years, 1e-6)
    iv = max(iv, 1e-4)
    d1 = (math.log(spot / strike) + (r + iv * iv / 2) * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)
    cdf = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
    pdf = math.exp(-d1 * d1 / 2) / math.sqrt(2 * math.pi)
    disc = math.exp(-r * t)
    if is_call:
        price = spot * cdf(d1) - strike * disc * cdf(d2)
        delta = cdf(d1)
        theta = (-spot * pdf * iv / (2 * math.sqrt(t)) - r * strike * disc * cdf(d2)) / 365
    else:
        price = strike * disc * cdf(-d2) - spot * cdf(-d1)
        delta = cdf(d1) - 1
        theta = (-spot * pdf * iv / (2 * math.sqrt(t)) + r * strike * disc * cdf(-d2)) / 365
    return {"price": price, "delta": delta, "gamma": pdf / (spot * iv * math.sqrt(t)),
            "theta": theta, "vega": spot * pdf * math.sqrt(t) / 100}


# ---------------------------------------------------------------- OI classification
def classify_oi(price_chg_pct: float, oi_chg_pct: float, price_thr: float = 0.0, oi_thr: float = 0.0) -> str:
    """Analytical heuristic on contract price change vs contract OI change."""
    if abs(price_chg_pct) <= price_thr or abs(oi_chg_pct) <= oi_thr:
        return "Neutral"
    if price_chg_pct > 0 and oi_chg_pct > 0:
        return "Long buildup"
    if price_chg_pct < 0 and oi_chg_pct > 0:
        return "Short buildup"
    if price_chg_pct > 0 and oi_chg_pct < 0:
        return "Short covering"
    return "Long unwinding"


# ---------------------------------------------------------------- risk
def position_size(capital: float, risk_pct: float, entry: float, stop: float, lot_size: int = 1,
                  margin_per_lot: Optional[float] = None, available_margin: Optional[float] = None) -> dict:
    if capital <= 0 or risk_pct <= 0:
        raise ValueError("capital and risk_pct must be positive")
    per_unit = abs(entry - stop)
    if per_unit <= 0:
        raise ValueError("entry and stop must differ")
    lot_size = max(int(lot_size or 1), 1)
    risk_amount = capital * risk_pct / 100
    lots = int(risk_amount // (per_unit * lot_size))
    limited_by = "risk budget"
    if margin_per_lot and available_margin is not None and margin_per_lot > 0:
        m_lots = int(available_margin // margin_per_lot)
        if m_lots < lots:
            lots, limited_by = m_lots, "available margin"
    qty = lots * lot_size
    return {"risk_amount": risk_amount, "per_unit_risk": per_unit, "lots": lots, "qty": qty,
            "actual_risk": qty * per_unit, "notional": qty * entry, "limited_by": limited_by,
            "warning": "Risk budget too small for one lot at this stop distance." if lots == 0 else None}
