"""MoneyFlow Intelligence Terminal - API server. Run: uvicorn main:app --host 0.0.0.0 --port 8000"""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import indicators as ind
from providers import ProviderError, make_provider
from universe import INDEX_UNDERLYINGS, INDICES, OPTION_UNDERLYINGS, SECTORS, STOCK_SECTOR, STOCKS

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("moneyflow")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

CFG_DEFAULT = {"vix_high": 20.0, "vix_low": 13.0, "adx_strong": 25.0, "adx_weak": 20.0, "rsi_period": 14,
               "oi_price_thr": 0.1, "oi_oi_thr": 0.5, "relvol_thr": 2.0}


class State:
    def __init__(self):
        self.provider = None
        self.q = {}                 # symbol -> quote (indices + stocks)
        self.f = {}                 # symbol -> futures quote
        self.ts = None
        self.last_ok = None
        self.error = None
        self.errors = 0
        self.m = collections.defaultdict(dict)   # symbol -> computed metrics
        self.cc = {}                # (symbol, interval) -> candles
        self.hist = collections.deque(maxlen=4000)   # (ts, {sym: {ltp, oi, vol}})
        self.events = collections.deque(maxlen=300)
        self.last_cls = {}
        self.straddle = collections.defaultdict(list)
        self.glob = None
        self.cfg = dict(CFG_DEFAULT)
        self.started = time.time()
        self.metric_errors = {}


S = State()


# ------------------------------------------------------------------ helpers
def market_status(now=None):
    now = now or datetime.now(IST)
    if now.weekday() >= 5:
        return "CLOSED (weekend)"
    t = now.time()
    if dtime(9, 0) <= t < dtime(9, 15):
        return "PRE-OPEN"
    if dtime(9, 15) <= t <= dtime(15, 30):
        return "OPEN"
    return "CLOSED"


def session_fraction():
    now = datetime.now(IST)
    if market_status(now) != "OPEN":
        return 1.0
    mins = (now.hour * 60 + now.minute) - (9 * 60 + 15)
    return min(max(mins / 375, 0.05), 1.0)


def day_of(t):
    return datetime.fromtimestamp(t, IST).date()


def intraday_metrics(c5, c15, period):
    m = {}
    if c5:
        closes = [c["c"] for c in c5]
        m["rsi5"] = ind.rsi(closes, period)
        e9, e21 = ind.ema_series(closes, 9), ind.ema_series(closes, 21)
        if e9[-1] is not None and e21[-1] is not None:
            m["ema_x5"] = "bull" if e9[-1] > e21[-1] else "bear"
            if len(closes) > 1 and e9[-2] is not None and e21[-2] is not None:
                m["ema_x5_fresh"] = (e9[-2] > e21[-2]) != (e9[-1] > e21[-1])
        mc = ind.macd(closes)
        m["macd5"] = mc["hist"] if mc else None
        hs, ls = [c["h"] for c in c5], [c["l"] for c in c5]
        a = ind.atr(hs, ls, closes, 14)
        if a and len(closes) > 1:
            last_tr = ind.true_ranges(hs, ls, closes)[-1]
            m["atr_exp5"] = last_tr / a
    if c15:
        m["rsi15"] = ind.rsi([c["c"] for c in c15], period)
    return m


def daily_metrics(c, period):
    if len(c) < 5:
        return {}
    closes, highs, lows = [x["c"] for x in c], [x["h"] for x in c], [x["l"] for x in c]
    today = datetime.now(IST).date()
    done = c[:-1] if day_of(c[-1]["t"]) == today else c      # completed sessions only
    m = {"rsiD": ind.rsi(closes, period), "ema20D": ind.ema(closes, 20), "ema50D": ind.ema(closes, 50),
         "adxD": ind.adx(highs, lows, closes, 14), "atrD": ind.atr(highs, lows, closes, 14),
         "hi52": max(highs[-252:]), "lo52": min(lows[-252:])}
    vols = [x["v"] for x in done[-20:] if x["v"]]
    m["avgvol20"] = sum(vols) / len(vols) if vols else None
    if done:
        m["pdh"], m["pdl"], m["pdc"] = done[-1]["h"], done[-1]["l"], done[-1]["c"]
    return m


def trend_state(ltp, m):
    adx, e20, e50 = m.get("adxD"), m.get("ema20D"), m.get("ema50D")
    if adx is None or e20 is None or e50 is None or ltp is None:
        return None
    cfg = S.cfg
    if adx >= cfg["adx_strong"] and ltp > e20 > e50:
        return "Strong uptrend"
    if adx >= cfg["adx_strong"] and ltp < e20 < e50:
        return "Strong downtrend"
    if adx < cfg["adx_weak"]:
        return "Range-bound"
    return "Weak trend"


# ------------------------------------------------------------------ OI flow
def record_history():
    if not S.f:
        return
    S.hist.append((S.ts, {s: {"ltp": q["ltp"], "oi": q["oi"], "vol": q["volume"]} for s, q in S.f.items() if q.get("oi") is not None}))


def oi_rows(window_min=5, price_thr=None, oi_thr=None):
    price_thr = S.cfg["oi_price_thr"] if price_thr is None else price_thr
    oi_thr = S.cfg["oi_oi_thr"] if oi_thr is None else oi_thr
    if len(S.hist) < 2:
        return [], None, True
    now_ts, now_d = S.hist[-1]
    target = now_ts - window_min * 60
    base = None
    for ts, d in S.hist:
        if ts <= target:
            base = (ts, d)
        else:
            break
    partial = base is None
    if partial:
        base = S.hist[0]
    base_ts, base_d = base
    rows = []
    for sym, cur in now_d.items():
        b = base_d.get(sym)
        if not b or not b["ltp"] or not b["oi"]:
            continue
        pchg = (cur["ltp"] - b["ltp"]) / b["ltp"] * 100
        ochg = (cur["oi"] - b["oi"]) / b["oi"] * 100
        fq = S.f.get(sym, {})
        cls = ind.classify_oi(pchg, ochg, price_thr, oi_thr)
        rows.append({"symbol": sym, "sector": STOCK_SECTOR.get(sym, "Index"), "expiry": fq.get("expiry"),
                     "contract": "FUT", "ltp": cur["ltp"], "price_chg": pchg, "oi": cur["oi"], "oi_chg": cur["oi"] - b["oi"],
                     "oi_chg_pct": ochg, "volume": cur["vol"], "vol_window": (cur["vol"] or 0) - (b["vol"] or 0),
                     "cls": cls, "ts": now_ts, "age_s": time.time() - now_ts})
    return rows, {"base_ts": base_ts, "elapsed_s": now_ts - base_ts, "window_min": window_min}, partial


def explain_oi(r, info, partial, price_thr, oi_thr):
    pdir = "increased" if r["price_chg"] > 0 else "decreased"
    odir = "increased" if r["oi_chg_pct"] > 0 else "decreased"
    return {
        "title": r["cls"].upper() + (" DETECTED" if r["cls"] != "Neutral" else ""),
        "lines": [f"Contract price {pdir} {r['price_chg']:+.2f}% over the window.",
                  f"Open interest {odir} {r['oi_chg_pct']:+.2f}% ({r['oi_chg']:+,.0f} contracts).",
                  f"Volume in window: {r['vol_window']:,.0f} (day {r['volume']:,.0f}).",
                  f"Window: {info['window_min']} min requested, {info['elapsed_s'] / 60:.1f} min of history available"
                  + (" (baseline still building)" if partial else "") + "."],
        "rule": f"Price change and OI change both measured on the same {r['expiry']} futures contract. "
                f"Neutral if |price|<={price_thr}% or |OI|<={oi_thr}%.",
        "limitation": "Heuristic only. It does not reveal who traded or why. OI change is measured from when this app "
                      "started tracking, not from the previous close.",
        "updated_at": r["ts"]}


def detect_events():
    rows, info, partial = oi_rows(5)
    if not info or info["elapsed_s"] < 60:
        return
    for r in rows:
        prev = S.last_cls.get(r["symbol"])
        if r["cls"] != "Neutral" and r["cls"] != prev:
            S.events.appendleft({"ts": r["ts"], "symbol": r["symbol"], "cls": r["cls"], "price_chg": r["price_chg"],
                                 "oi_chg_pct": r["oi_chg_pct"], "window_min": 5})
        S.last_cls[r["symbol"]] = r["cls"]


# ------------------------------------------------------------------ background loops
async def poll_snapshot():
    p, fails = S.provider, 0
    while True:
        try:
            snap = await p.snapshot()
            S.q, S.f, S.ts = snap["quotes"], snap["futures"], snap["ts"]
            S.last_ok, S.error, fails = time.time(), None, 0
            record_history()
            detect_events()
        except Exception as e:  # keep serving last good data, flagged stale
            fails += 1
            S.errors += 1
            S.error = str(e)
            log.warning("snapshot failed: %s", e)
        await asyncio.sleep(min(60, p.poll_seconds * (2 ** min(fails, 4))) if fails else p.poll_seconds)


async def refresh_intraday():
    p = S.provider
    sem = asyncio.Semaphore(4)
    syms = STOCKS + ["NIFTY", "BANKNIFTY"] + [s for s in OPTION_UNDERLYINGS if s in INDICES and s not in ("NIFTY", "BANKNIFTY")]

    async def one(sym):
        async with sem:
            try:
                c5, c15 = await p.candles(sym, "5m"), await p.candles(sym, "15m")
                S.cc[(sym, "5m")], S.cc[(sym, "15m")] = c5, c15
                S.m[sym].update(intraday_metrics(c5, c15, int(S.cfg["rsi_period"])))
                S.metric_errors.pop(sym + ":i", None)
            except Exception as e:
                S.metric_errors[sym + ":i"] = str(e)

    while True:
        if S.q:
            await asyncio.gather(*[one(s) for s in syms])
        await asyncio.sleep(60 if p.mode != "DELAYED" else 120)


async def refresh_daily():
    p = S.provider
    sem = asyncio.Semaphore(4)
    syms = STOCKS + ["NIFTY", "BANKNIFTY"]

    async def one(sym):
        async with sem:
            try:
                c = await p.candles(sym, "1d")
                S.cc[(sym, "1d")] = c
                S.m[sym].update(daily_metrics(c, int(S.cfg["rsi_period"])))
                S.metric_errors.pop(sym + ":d", None)
            except Exception as e:
                S.metric_errors[sym + ":d"] = str(e)

    while True:
        if S.q:
            await asyncio.gather(*[one(s) for s in syms])
        await asyncio.sleep(600)


async def refresh_global():
    p = S.provider
    use = p.mode == "DEMO" or p.mode == "DELAYED" or os.getenv("GLOBAL_CUES", "").lower() == "yahoo"
    fetch = p.global_cues
    if p.mode == "LIVE" and use:
        from providers import YahooProvider
        y = YahooProvider()
        fetch = y.global_cues
    while use:
        try:
            S.glob = await fetch()
        except Exception as e:
            log.warning("global cues failed: %s", e)
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app):
    S.provider = make_provider()
    try:
        await S.provider.start()
    except Exception as e:
        log.error("provider start failed: %s", e)
        S.error = f"Provider start failed: {e}"
    tasks = [asyncio.create_task(f()) for f in (poll_snapshot, refresh_intraday, refresh_daily, refresh_global)]
    yield
    for t in tasks:
        t.cancel()
    await S.provider.close()


app = FastAPI(title="MoneyFlow Intelligence Terminal", lifespan=lifespan)


# ------------------------------------------------------------------ snapshot
def regime():
    nm, vix = S.m.get("NIFTY", {}), (S.q.get("INDIAVIX") or {}).get("ltp")
    nq = S.q.get("NIFTY") or {}
    cfg, why = S.cfg, []
    adx, e20, e50, ltp = nm.get("adxD"), nm.get("ema20D"), nm.get("ema50D"), nq.get("ltp")
    if vix is not None:
        why.append(f"India VIX {vix:.2f} (high >= {cfg['vix_high']}, low <= {cfg['vix_low']})")
    if adx is not None:
        why.append(f"NIFTY daily ADX {adx:.1f} (strong >= {cfg['adx_strong']}, weak < {cfg['adx_weak']})")
    if None not in (ltp, e20, e50):
        why.append(f"NIFTY {ltp:,.0f} vs EMA20 {e20:,.0f} / EMA50 {e50:,.0f}")
    if adx is None and vix is None:
        return {"label": "Mixed or uncertain", "why": ["Not enough data yet (waiting for candles / VIX)."]}
    if vix is not None and vix >= cfg["vix_high"]:
        label = "High volatility"
    elif adx is not None and adx >= cfg["adx_strong"] and None not in (ltp, e20, e50) and (ltp > e20 > e50 or ltp < e20 < e50):
        label = "Strong trend"
    elif adx is not None and adx < cfg["adx_weak"]:
        label = "Low volatility / range-bound" if (vix is not None and vix <= cfg["vix_low"]) else "Range-bound"
    elif adx is not None and adx >= cfg["adx_weak"]:
        label = "Weak trend"
    else:
        label = "Mixed or uncertain"
    return {"label": label, "why": why}


def stock_rows():
    frac, nifty_pct = session_fraction(), (S.q.get("NIFTY") or {}).get("pct")
    oi_by = {r["symbol"]: r for r in oi_rows(15)[0]}
    rows = []
    for sym in STOCKS:
        q = S.q.get(sym)
        if not q:
            continue
        m = S.m.get(sym, {})
        avg = m.get("avgvol20")
        relvol = q["volume"] / (avg * frac) if avg and q["volume"] else None
        o = oi_by.get(sym)
        rows.append({
            "symbol": sym, "sector": STOCK_SECTOR[sym], "ltp": q["ltp"], "chg": q["chg"], "pct": q["pct"],
            "volume": q["volume"], "turnover": q["ltp"] * q["volume"], "relvol": relvol, "vwap": q["vwap"],
            "vs_vwap": (q["ltp"] / q["vwap"] - 1) * 100 if q["vwap"] else None,
            "rsi5": m.get("rsi5"), "rsi15": m.get("rsi15"), "rsiD": m.get("rsiD"), "ema_x5": m.get("ema_x5"),
            "macd5": m.get("macd5"), "adxD": m.get("adxD"), "atr_exp5": m.get("atr_exp5"),
            "trend": trend_state(q["ltp"], m), "hi52": m.get("hi52"), "lo52": m.get("lo52"),
            "pdh": m.get("pdh"), "pdl": m.get("pdl"),
            "gap": (q["open"] / q["prev_close"] - 1) * 100 if q.get("open") and q["prev_close"] else None,
            "rs": (q["pct"] - nifty_pct) if nifty_pct is not None else None,
            "oi_class": o["cls"] if o else None, "oi_chg_pct": o["oi_chg_pct"] if o else None, "ts": q["ts"]})
    return rows


@app.get("/api/snapshot")
def snapshot():
    rows = stock_rows()
    adv = sum(1 for r in rows if r["pct"] > 0)
    dec = sum(1 for r in rows if r["pct"] < 0)
    sectors = []
    for sec in SECTORS:
        rs = [r for r in rows if r["sector"] == sec]
        if rs:
            sectors.append({"sector": sec, "pct": sum(r["pct"] for r in rs) / len(rs), "n": len(rs),
                            "adv": sum(1 for r in rs if r["pct"] > 0), "turnover": sum(r["turnover"] for r in rs),
                            "rs": (sum(r["rs"] for r in rs if r["rs"] is not None) / len(rs)) if rs[0]["rs"] is not None else None})
    p = S.provider
    age = time.time() - S.last_ok if S.last_ok else None
    return {
        "meta": {"mode": p.mode, "source": p.name, "ts": S.ts, "age_s": age, "stale": age is None or age > max(p.poll_seconds * 4, 30),
                 "error": S.error, "market": market_status(), "has_derivatives": p.has_derivatives,
                 "universe": f"{len(rows)} tracked stocks (not the full NSE universe)"},
        "indices": [S.q[s] for s in INDICES if s in S.q], "stocks": rows, "sectors": sectors,
        "breadth": {"adv": adv, "dec": dec, "unch": len(rows) - adv - dec}, "regime": regime(),
        "global": S.glob, "config": S.cfg}


@app.get("/api/status")
def status():
    p = S.provider
    return {"provider": p.name, "mode": p.mode, "market": market_status(), "last_ok": S.last_ok, "error": S.error,
            "errors": S.errors, "uptime_s": time.time() - S.started, "futures_tracked": len(S.f),
            "history_points": len(S.hist), "candle_series": len(S.cc), "metric_errors": dict(list(S.metric_errors.items())[:10])}


@app.get("/api/oiflow")
def oiflow(window: int = 5, price_thr: float | None = None, oi_thr: float | None = None):
    if not S.provider.has_derivatives:
        return {"available": False, "reason": f"{S.provider.name} does not provide futures OI.", "rows": [], "events": []}
    pt = S.cfg["oi_price_thr"] if price_thr is None else price_thr
    ot = S.cfg["oi_oi_thr"] if oi_thr is None else oi_thr
    rows, info, partial = oi_rows(window, pt, ot)
    for r in rows:
        r["explain"] = explain_oi(r, info, partial, pt, ot)
    return {"available": True, "rows": rows, "info": info, "partial": partial, "events": list(S.events)[:100],
            "label": "Analytical heuristic - not proof of any trader's intent."}


# ------------------------------------------------------------------ options
def analyze_chain(sym, expiry, ch, pcr_method):
    rows, spot = ch["rows"], ch["spot"]
    strikes = [r["strike"] for r in rows]
    atm = min(strikes, key=lambda k: abs(k - spot)) if strikes else None
    for r in rows:
        r["ce_class"] = "ATM" if r["strike"] == atm else ("ITM" if r["strike"] < spot else "OTM")
        r["pe_class"] = "ATM" if r["strike"] == atm else ("ITM" if r["strike"] > spot else "OTM")
        r["ce_oi_chg"] = (r["ce_oi"] or 0) - (r["ce_prev_oi"] or 0)
        r["pe_oi_chg"] = (r["pe_oi"] or 0) - (r["pe_prev_oi"] or 0)
    atm_row = next((r for r in rows if r["strike"] == atm), None)
    straddle = (atm_row["ce_ltp"] or 0) + (atm_row["pe_ltp"] or 0) if atm_row else None
    hist = S.straddle[(sym, expiry)]
    if straddle and (not hist or time.time() - hist[-1][0] >= 10):
        hist.append((time.time(), straddle))
        del hist[:-500]
    top_c = sorted(rows, key=lambda r: r["ce_oi"] or 0, reverse=True)[:3]
    top_p = sorted(rows, key=lambda r: r["pe_oi"] or 0, reverse=True)[:3]
    p_oi, p_vol, mp = ind.pcr(rows, "oi"), ind.pcr(rows, "volume"), ind.max_pain(rows)
    notes = []
    if top_c and top_p:
        notes.append(f"Largest call OI is at {top_c[0]['strike']:,.0f} and largest put OI at {top_p[0]['strike']:,.0f}. "
                     "These are the strikes where most contracts are open; they are often watched as reference levels, not guarantees.")
    if p_oi is not None:
        notes.append(f"PCR (OI) is {p_oi:.2f}. PCR alone does not indicate direction - read it with price action, volume and expiry proximity.")
    if mp:
        notes.append(f"Max pain computes to {mp:,.0f} (a model-derived statistic assuming option writers minimise payout; not a forecast).")
    notes.append("IV rank / percentile need weeks of stored IV history, so they are not shown yet.")
    return {"symbol": sym, "expiry": expiry, "spot": spot, "atm": atm, "straddle": straddle,
            "straddle_hist": [{"t": t, "v": v} for t, v in hist], "pcr_oi": p_oi, "pcr_vol": p_vol, "pcr_method": pcr_method,
            "max_pain": mp, "top_call_oi": [r["strike"] for r in top_c], "top_put_oi": [r["strike"] for r in top_p],
            "rows": rows, "ts": ch["ts"], "interpretation": notes,
            "labels": {"max_pain": "model-derived", "pcr": "calculated", "oi": "source data"}}


@app.get("/api/options")
async def options(symbol: str = "NIFTY", expiry: str | None = None, pcr_method: str = "oi"):
    if symbol not in OPTION_UNDERLYINGS:
        raise HTTPException(404, "Unknown underlying")
    p = S.provider
    if not p.has_derivatives:
        return {"available": False, "reason": f"{p.name} does not provide option chains."}
    exps = await p.expiries(symbol)
    if not exps:
        return {"available": False, "reason": f"No expiries found for {symbol}."}
    expiry = expiry if expiry in exps else exps[0]
    try:
        ch = await p.option_chain(symbol, expiry)
    except ProviderError as e:
        return {"available": False, "reason": str(e)}
    out = analyze_chain(symbol, expiry, ch, pcr_method)
    out.update({"available": True, "expiries": exps, "lot_size": p.lot_size(symbol)})
    return out


# ------------------------------------------------------------------ RSI scan / candles / levels
@app.get("/api/rsiscan")
def rsiscan(period: int = 14):
    period = min(max(period, 2), 50)
    rows = []
    for r in stock_rows():
        row = {k: r[k] for k in ("symbol", "sector", "ltp", "pct", "vs_vwap", "relvol", "ema_x5", "adxD")}
        for iv, key in (("5m", "rsi5"), ("15m", "rsi15"), ("1d", "rsiD")):
            c = S.cc.get((r["symbol"], iv))
            if c:
                closes = [x["c"] for x in c]
                ser = ind.rsi_series(closes, period)
                row[key], row["prev_" + key] = ser[-1], (ser[-2] if len(ser) > 1 else None)
                if iv == "5m":
                    row["div"] = ind.divergence(closes[-150:], ser[-150:])
        rows.append(row)
    return {"period": period, "rows": rows}


@app.get("/api/candles")
async def candles(symbol: str, interval: str = "5m", rsi_period: int = 14):
    if interval not in ("5m", "15m", "1d"):
        raise HTTPException(400, "interval must be 5m, 15m or 1d")
    c = S.cc.get((symbol, interval))
    if c is None:
        try:
            c = await S.provider.candles(symbol, interval)
        except ProviderError as e:
            raise HTTPException(502, str(e))
    c = c[-300:]
    return {"symbol": symbol, "interval": interval, "candles": c,
            "rsi": ind.rsi_series([x["c"] for x in c], min(max(rsi_period, 2), 50))}


def _level(name, price, typ, note=""):
    return {"name": name, "price": price, "type": typ, "note": note}


@app.get("/api/levels")
async def levels(symbol: str, interval: str = "5m"):
    try:
        c5 = S.cc.get((symbol, "5m")) or await S.provider.candles(symbol, "5m")
        cd = S.cc.get((symbol, "1d")) or await S.provider.candles(symbol, "1d")
    except ProviderError as e:
        raise HTTPException(502, str(e))
    if not c5:
        raise HTTPException(404, "No intraday candles available")
    days = sorted({day_of(c["t"]) for c in c5})
    sess = days[-1]
    prev = days[-2] if len(days) > 1 else None
    today_c = [c for c in c5 if day_of(c["t"]) == sess]
    prev_c = [c for c in c5 if prev and day_of(c["t"]) == prev]
    lv, events = [], []
    q = S.q.get(symbol)
    if q:
        lv.append(_level("LTP", q["ltp"], "observed"))
    lv += [_level("Day high", max(c["h"] for c in today_c), "observed"), _level("Day low", min(c["l"] for c in today_c), "observed")]
    if prev_c:
        lv += [_level("Prev day high", max(c["h"] for c in prev_c), "observed"), _level("Prev day low", min(c["l"] for c in prev_c), "observed")]
        pv = ind.pivots(max(c["h"] for c in prev_c), min(c["l"] for c in prev_c), prev_c[-1]["c"])
        for k in ("P", "BC", "TC", "R1", "S1", "R2", "S2"):
            lv.append(_level({"P": "Pivot", "BC": "CPR bottom", "TC": "CPR top"}.get(k, k), pv[k], "derived", "from previous session H/L/C"))
    vw = ind.vwap(today_c)
    if vw:
        lv.append(_level("VWAP (session)", vw, "derived"))
    avw = ind.vwap(prev_c + today_c)
    if avw and prev_c:
        lv.append(_level("Anchored VWAP (prev open)", avw, "derived"))
    for n, bars in (("OR5", 1), ("OR15", 3), ("OR30", 6)):
        seg = today_c[:bars]
        if len(seg) == bars:
            lv += [_level(f"{n} high", max(c["h"] for c in seg), "derived"), _level(f"{n} low", min(c["l"] for c in seg), "derived")]
    # previous week from daily candles
    if cd:
        wk = {}
        for c in cd:
            d = day_of(c["t"]).isocalendar()
            wk.setdefault((d[0], d[1]), []).append(c)
        keys = sorted(wk)
        cur = datetime.now(IST).date().isocalendar()
        pw = [k for k in keys if k != (cur[0], cur[1])]
        if pw:
            w = wk[pw[-1]]
            lv += [_level("Prev week high", max(c["h"] for c in w), "derived"), _level("Prev week low", min(c["l"] for c in w), "derived")]
    # swings + estimated liquidity pools (equal highs / lows)
    sw = ind.swings(c5[-400:])
    for t, p in sw["highs"][-2:]:
        lv.append(_level("Swing high", p, "derived"))
    for t, p in sw["lows"][-2:]:
        lv.append(_level("Swing low", p, "derived"))
    for kind, pts in (("highs", sw["highs"]), ("lows", sw["lows"])):
        prices = sorted(p for _, p in pts)
        for i in range(len(prices) - 1):
            if prices[i] and abs(prices[i + 1] - prices[i]) / prices[i] < 0.001:
                lv.append(_level(f"Equal {kind} (liquidity pool est.)", (prices[i] + prices[i + 1]) / 2, "estimated",
                                 "clustered swing points; resting orders are an assumption, not observed"))
    # volume profile (last 3 sessions)
    vp = ind.volume_profile([c for c in c5 if day_of(c["t"]) in days[-3:]])
    if vp:
        lv += [_level("POC", vp["poc"], "derived"), _level("VAH", vp["vah"], "derived"), _level("VAL", vp["val"], "derived")]
    # liquidity sweeps (estimated): wick through a prior level, close back inside
    ref_hi = max((c["h"] for c in prev_c), default=None)
    ref_lo = min((c["l"] for c in prev_c), default=None)
    for c in today_c:
        if ref_hi and c["h"] > ref_hi and c["c"] < ref_hi:
            events.append({"t": c["t"], "kind": "Sweep of prev-day high", "price": ref_hi})
        if ref_lo and c["l"] < ref_lo and c["c"] > ref_lo:
            events.append({"t": c["t"], "kind": "Sweep of prev-day low", "price": ref_lo})
    # gap
    if prev_c and today_c and prev_c[-1]["c"]:
        g = (today_c[0]["o"] / prev_c[-1]["c"] - 1) * 100
        if abs(g) >= 0.3:
            lv.append(_level("Gap (prev close)", prev_c[-1]["c"], "derived", f"gap {g:+.2f}% at open"))
    # option OI levels (observed)
    if S.provider.has_derivatives and symbol in OPTION_UNDERLYINGS:
        try:
            exps = await S.provider.expiries(symbol)
            ch = await S.provider.option_chain(symbol, exps[0])
            rows = ch["rows"]
            lv.append(_level("Max call OI strike", max(rows, key=lambda r: r["ce_oi"] or 0)["strike"], "observed", "option-chain OI"))
            lv.append(_level("Max put OI strike", max(rows, key=lambda r: r["pe_oi"] or 0)["strike"], "observed", "option-chain OI"))
        except Exception:
            pass
    cc = c5 if interval == "5m" else (S.cc.get((symbol, "15m")) or await S.provider.candles(symbol, "15m"))
    return {"symbol": symbol, "interval": interval, "session": sess.isoformat(), "candles": cc[-400:], "levels": lv,
            "profile": vp["profile"] if vp else [], "sweeps": events,
            "notes": ["Observed = printed by the exchange feed. Derived = calculated from history. Estimated = algorithmic guess.",
                      "Liquidity zones are estimates unless backed by real market-depth data; bid/ask depth is not shown (not in this feed)."
                      + (" Index volume is zero, so no volume profile for indices." if not vp else "")]}


# ------------------------------------------------------------------ config & position sizing
class CfgIn(BaseModel):
    vix_high: float | None = None
    vix_low: float | None = None
    adx_strong: float | None = None
    adx_weak: float | None = None
    rsi_period: int | None = None
    oi_price_thr: float | None = None
    oi_oi_thr: float | None = None
    relvol_thr: float | None = None


@app.post("/api/config")
def set_config(c: CfgIn):
    for k, v in c.model_dump(exclude_none=True).items():
        S.cfg[k] = max(v, 0)
    S.cfg["rsi_period"] = int(min(max(S.cfg["rsi_period"], 2), 50))
    return S.cfg


class PosIn(BaseModel):
    capital: float
    risk_pct: float
    entry: float
    stop: float
    lot_size: int = 1
    margin_per_lot: float | None = None
    available_margin: float | None = None


@app.post("/api/position")
def position(p: PosIn):
    try:
        return ind.position_size(p.capital, p.risk_pct, p.entry, p.stop, p.lot_size, p.margin_per_lot, p.available_margin)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ------------------------------------------------------------------ static frontend
STATIC = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
