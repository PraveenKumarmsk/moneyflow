"""Market-data providers behind one interface.

  UpstoxProvider  - LIVE, official broker API (needs UPSTOX_ACCESS_TOKEN). Quotes, candles, futures OI, option chain.
  YahooProvider   - DELAYED, unofficial public endpoint (opt-in). Quotes + candles only. No OI / option chain.
  DemoProvider    - SYNTHETIC data. Always shown with a DEMO banner. Never presented as live.

Normalised quote: {symbol, name, kind, ltp, prev_close, chg, pct, volume, vwap, open, high, low, oi, ts}
Normalised candle: {t (epoch s), o, h, l, c, v}
"""
from __future__ import annotations

import asyncio
import gzip
import json
import math
import os
import random
import time
import zlib
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote as urlquote
from zoneinfo import ZoneInfo

import httpx

import indicators as ind
from universe import GLOBAL_YAHOO, INDEX_UNDERLYINGS, INDICES, OPTION_UNDERLYINGS, STOCK_SECTOR, STOCKS

IST = ZoneInfo("Asia/Kolkata")


class ProviderError(Exception):
    pass


class Provider:
    name = "base"
    mode = "DEMO"          # LIVE | DELAYED | DEMO
    poll_seconds = 3.0
    has_derivatives = False

    async def start(self): ...
    async def close(self): ...
    async def snapshot(self) -> dict: raise NotImplementedError
    async def candles(self, sym: str, interval: str) -> list: raise NotImplementedError
    async def expiries(self, sym: str) -> list: return []
    async def option_chain(self, sym: str, expiry: str) -> dict:
        raise ProviderError("Option chain is not available from this data source.")
    def lot_size(self, sym: str):
        return None
    async def global_cues(self):
        return None


def _quote(sym, name, kind, ltp, prev, volume=0, vwap=None, o=None, h=None, l=None, oi=None, ts=None):
    chg = ltp - prev if prev else 0.0
    return {"symbol": sym, "name": name, "kind": kind, "ltp": ltp, "prev_close": prev, "chg": chg,
            "pct": (chg / prev * 100) if prev else 0.0, "volume": volume or 0, "vwap": vwap, "open": o,
            "high": h, "low": l, "oi": oi, "ts": ts or time.time()}


# =====================================================================================
# Upstox
# =====================================================================================
UPSTOX_BASE = "https://api.upstox.com/v2"
UPSTOX_INSTRUMENTS = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"


def _ms_to_date(ms) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, IST).date().isoformat()


def build_instruments(raw: list) -> dict:
    """Reduce the (huge) Upstox master file to what we need."""
    wanted_eq = set(STOCKS)
    wanted_und = set(OPTION_UNDERLYINGS)
    alias_map = {}
    for sym, meta in INDICES.items():
        for a in meta["aliases"]:
            alias_map[a] = sym
    out = {"index": {}, "eq": {}, "fut": {}, "opt_exp": {}, "lot": {}, "built": date.today().isoformat()}
    for r in raw:
        t = r.get("instrument_type")
        seg = r.get("segment")
        und = r.get("underlying_symbol")
        if t == "INDEX":
            for n in (str(r.get("name", "")).lower(), str(r.get("trading_symbol", "")).lower()):
                sym = alias_map.get(n)
                if sym and sym not in out["index"]:
                    out["index"][sym] = r["instrument_key"]
        elif seg == "NSE_EQ" and t == "EQ" and r.get("trading_symbol") in wanted_eq:
            out["eq"][r["trading_symbol"]] = r["instrument_key"]
        elif t == "FUT" and und in wanted_und:
            out["fut"].setdefault(und, []).append([_ms_to_date(r["expiry"]), r["instrument_key"]])
            if r.get("lot_size"):
                out["lot"][und] = int(r["lot_size"])
        elif t in ("CE", "PE") and und in wanted_und and r.get("expiry"):
            out["opt_exp"].setdefault(und, set()).add(_ms_to_date(r["expiry"]))
    out["opt_exp"] = {k: sorted(v) for k, v in out["opt_exp"].items()}
    for v in out["fut"].values():
        v.sort()
    return out


class UpstoxProvider(Provider):
    name = "Upstox API"
    mode = "LIVE"
    poll_seconds = 3.0
    has_derivatives = True

    def __init__(self, token: str, cache_dir: Path):
        self.token = token
        self.cache_dir = cache_dir
        self.client = httpx.AsyncClient(timeout=20, headers={"Authorization": f"Bearer {token}",
                                                             "Accept": "application/json"})
        self.inst = None
        self.keymap = {}     # instrument_key -> (kind, symbol)
        self._raw = {}
        self._sem = asyncio.Semaphore(8)

    async def start(self):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / "upstox_instruments.json"
        inst = None
        if path.exists():
            try:
                cached = json.loads(path.read_text())
                if cached.get("built") == date.today().isoformat():
                    inst = cached
            except Exception:
                inst = None
        if inst is None:
            async with httpx.AsyncClient(timeout=180, follow_redirects=True) as c:
                r = await c.get(UPSTOX_INSTRUMENTS)
            if r.status_code != 200:
                raise ProviderError(f"Could not download Upstox instrument master (HTTP {r.status_code}).")
            inst = await asyncio.to_thread(lambda: build_instruments(json.loads(gzip.decompress(r.content))))
            path.write_text(json.dumps(inst))
        self.inst = inst
        self._build_keymap()

    def _build_keymap(self):
        self.keymap = {}
        today = date.today().isoformat()
        for sym, key in self.inst["index"].items():
            self.keymap[key] = ("index", sym)
        for sym, key in self.inst["eq"].items():
            self.keymap[key] = ("stock", sym)
        self.fut_keys = {}
        for und, lst in self.inst["fut"].items():
            nxt = next(((e, k) for e, k in lst if e >= today), None)
            if nxt:
                self.fut_keys[und] = nxt
                self.keymap[nxt[1]] = ("future", und)

    def lot_size(self, sym):
        return (self.inst or {}).get("lot", {}).get(sym)

    async def _get(self, path, params=None):
        async with self._sem:
            try:
                r = await self.client.get(UPSTOX_BASE + path, params=params)
            except httpx.HTTPError as e:
                raise ProviderError(f"Network error talking to Upstox: {e}")
        if r.status_code == 401:
            raise ProviderError("Upstox rejected the access token (401). Tokens expire daily - generate a new UPSTOX_ACCESS_TOKEN.")
        if r.status_code == 429:
            raise ProviderError("Upstox rate limit hit (429). Increase POLL_SECONDS.")
        if r.status_code != 200:
            raise ProviderError(f"Upstox HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def _key(self, sym):
        k = self.inst["index"].get(sym) or self.inst["eq"].get(sym)
        if not k:
            raise ProviderError(f"No Upstox instrument found for {sym}.")
        return k

    async def snapshot(self):
        keys = list(self.keymap.keys())
        quotes, futures = {}, {}
        for i in range(0, len(keys), 400):
            chunk = keys[i:i + 400]
            data = (await self._get("/market-quote/quotes", {"instrument_key": ",".join(chunk)})).get("data", {})
            for q in data.values():
                tok = q.get("instrument_token")
                if tok not in self.keymap:
                    continue
                kind, sym = self.keymap[tok]
                ltp = q.get("last_price")
                if ltp is None:
                    continue
                chg = q.get("net_change")
                ohlc = q.get("ohlc") or {}
                prev = ltp - chg if chg is not None else ohlc.get("close")
                nq = _quote(sym, INDICES.get(sym, {}).get("name", sym), kind, ltp, prev, q.get("volume"),
                            q.get("average_price") or None, ohlc.get("open"), ohlc.get("high"), ohlc.get("low"),
                            q.get("oi"))
                if kind == "future":
                    nq["expiry"] = self.fut_keys[sym][0]
                    futures[sym] = nq
                else:
                    quotes[sym] = nq
        if not quotes:
            raise ProviderError("Upstox returned no quotes. Check the token and instrument keys.")
        return {"quotes": quotes, "futures": futures, "ts": time.time()}

    @staticmethod
    def _parse(rows):
        out = {}
        for r in rows:
            t = int(datetime.fromisoformat(r[0]).timestamp())
            out[t] = {"t": t, "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5] or 0}
        return [out[k] for k in sorted(out)]

    async def _raw_1m(self, sym):
        hit = self._raw.get(sym)
        if hit and time.time() - hit[0] < 50:
            return hit[1]
        key = urlquote(self._key(sym), safe="")
        today = datetime.now(IST).date()
        hist = await self._get(f"/historical-candle/{key}/1minute/{(today - timedelta(days=1)).isoformat()}/{(today - timedelta(days=5)).isoformat()}")
        intr = await self._get(f"/historical-candle/intraday/{key}/1minute")
        rows = self._parse((hist.get("data") or {}).get("candles", []) + (intr.get("data") or {}).get("candles", []))
        self._raw[sym] = (time.time(), rows)
        return rows

    async def candles(self, sym, interval):
        if interval in ("5m", "15m"):
            return ind.resample(await self._raw_1m(sym), int(interval[:-1]))
        if interval == "1d":
            key = urlquote(self._key(sym), safe="")
            today = datetime.now(IST).date()
            d = await self._get(f"/historical-candle/{key}/day/{today.isoformat()}/{(today - timedelta(days=400)).isoformat()}")
            return self._parse((d.get("data") or {}).get("candles", []))
        raise ProviderError(f"Unsupported interval {interval}")

    async def expiries(self, sym):
        today = date.today().isoformat()
        return [e for e in (self.inst or {}).get("opt_exp", {}).get(sym, []) if e >= today]

    async def option_chain(self, sym, expiry):
        d = await self._get("/option/chain", {"instrument_key": self._key(sym), "expiry_date": expiry})
        data = d.get("data") or []
        if not data:
            raise ProviderError("Upstox returned an empty option chain for this expiry.")
        rows = []
        for x in data:
            ce, pe = x.get("call_options") or {}, x.get("put_options") or {}
            cm, pm = ce.get("market_data") or {}, pe.get("market_data") or {}
            cg, pg = ce.get("option_greeks") or {}, pe.get("option_greeks") or {}
            rows.append({"strike": x["strike_price"],
                         "ce_ltp": cm.get("ltp"), "ce_oi": cm.get("oi"), "ce_prev_oi": cm.get("prev_oi"),
                         "ce_vol": cm.get("volume"), "ce_iv": cg.get("iv"), "ce_delta": cg.get("delta"),
                         "ce_gamma": cg.get("gamma"), "ce_theta": cg.get("theta"), "ce_vega": cg.get("vega"),
                         "pe_ltp": pm.get("ltp"), "pe_oi": pm.get("oi"), "pe_prev_oi": pm.get("prev_oi"),
                         "pe_vol": pm.get("volume"), "pe_iv": pg.get("iv"), "pe_delta": pg.get("delta"),
                         "pe_gamma": pg.get("gamma"), "pe_theta": pg.get("theta"), "pe_vega": pg.get("vega")})
        rows.sort(key=lambda r: r["strike"])
        return {"spot": data[0].get("underlying_spot_price"), "rows": rows, "ts": time.time()}

    async def close(self):
        await self.client.aclose()


# =====================================================================================
# Yahoo (unofficial, delayed, opt-in)
# =====================================================================================
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/"


class YahooProvider(Provider):
    name = "Yahoo Finance (unofficial, delayed)"
    mode = "DELAYED"
    poll_seconds = 20.0

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=20, headers={"User-Agent": "Mozilla/5.0 MoneyFlow"})
        self._sem = asyncio.Semaphore(6)

    @staticmethod
    def _ysym(sym):
        if sym in INDICES:
            return INDICES[sym]["yahoo"]
        return f"{sym}.NS"

    async def _chart(self, ysym, rng, interval):
        async with self._sem:
            try:
                r = await self.client.get(YAHOO_CHART + urlquote(ysym, safe=""), params={"range": rng, "interval": interval})
            except httpx.HTTPError as e:
                raise ProviderError(f"Network error talking to Yahoo: {e}")
        if r.status_code != 200:
            raise ProviderError(f"Yahoo HTTP {r.status_code} for {ysym}")
        res = (r.json().get("chart") or {}).get("result")
        if not res:
            raise ProviderError(f"Yahoo returned no data for {ysym}")
        return res[0]

    @staticmethod
    def _candles_from(res):
        ts = res.get("timestamp") or []
        q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        out = []
        for i, t in enumerate(ts):
            o, h, l, c = (q.get(k, [None] * len(ts))[i] for k in ("open", "high", "low", "close"))
            if None in (o, h, l, c):
                continue
            out.append({"t": int(t), "o": o, "h": h, "l": l, "c": c, "v": (q.get("volume") or [0] * len(ts))[i] or 0})
        return out

    async def snapshot(self):
        syms = [s for s in INDICES if INDICES[s]["yahoo"]] + STOCKS

        async def one(sym):
            try:
                res = await self._chart(self._ysym(sym), "1d", "5m")
            except ProviderError:
                return None
            m = res.get("meta") or {}
            ltp = m.get("regularMarketPrice")
            prev = m.get("chartPreviousClose") or m.get("previousClose")
            if ltp is None or not prev:
                return None
            cs = self._candles_from(res)
            return _quote(sym, INDICES.get(sym, {}).get("name", sym), "index" if sym in INDICES else "stock", ltp, prev,
                          m.get("regularMarketVolume") or sum(c["v"] for c in cs), ind.vwap(cs),
                          cs[0]["o"] if cs else None, m.get("regularMarketDayHigh"), m.get("regularMarketDayLow"))

        results = await asyncio.gather(*[one(s) for s in syms])
        quotes = {q["symbol"]: q for q in results if q}
        if not quotes:
            raise ProviderError("Yahoo returned no quotes (blocked or rate limited).")
        return {"quotes": quotes, "futures": {}, "ts": time.time()}

    async def candles(self, sym, interval):
        rng, iv = {"5m": ("5d", "5m"), "15m": ("5d", "15m"), "1d": ("1y", "1d")}[interval]
        return self._candles_from(await self._chart(self._ysym(sym), rng, iv))

    async def global_cues(self):
        return await fetch_global_yahoo(self.client)

    async def close(self):
        await self.client.aclose()


async def fetch_global_yahoo(client):
    async def one(label, ysym):
        try:
            r = await client.get(YAHOO_CHART + urlquote(ysym, safe=""), params={"range": "1d", "interval": "5m"})
            m = r.json()["chart"]["result"][0]["meta"]
            ltp, prev = m["regularMarketPrice"], m.get("chartPreviousClose") or m.get("previousClose")
            return {"name": label, "ltp": ltp, "pct": (ltp - prev) / prev * 100 if prev else None}
        except Exception:
            return None
    res = await asyncio.gather(*[one(n, s) for n, s in GLOBAL_YAHOO])
    return {"items": [x for x in res if x], "source": "Yahoo Finance (unofficial, delayed)", "ts": time.time()}


# =====================================================================================
# Demo (synthetic)
# =====================================================================================
def _next_weekday(d: date, wd: int) -> date:
    return d + timedelta(days=(wd - d.weekday()) % 7)


def _last_weekday_of_month(y, m, wd) -> date:
    d = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    while d.weekday() != wd:
        d -= timedelta(days=1)
    return d


class DemoProvider(Provider):
    name = "DEMO - synthetic data"
    mode = "DEMO"
    poll_seconds = 2.0
    has_derivatives = True
    STEP = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "MIDCPNIFTY": 25, "SENSEX": 100}
    BASE = {"NIFTY": 24000, "BANKNIFTY": 52000, "SENSEX": 79000, "FINNIFTY": 23500, "MIDCPNIFTY": 12000, "INDIAVIX": 14.5}

    def __init__(self):
        self.st = {}
        self.fut = {}
        self.oi_book = {}
        for sym in list(INDICES) + STOCKS:
            r = random.Random(sym)
            base = self.BASE.get(sym) or r.uniform(150, 4500)
            ltp = base * (1 + r.gauss(0, 0.008))
            self.st[sym] = {"prev": base, "ltp": ltp, "vol": 0, "pv": 0.0, "open": ltp, "high": ltp, "low": ltp,
                            "avg_vol": r.uniform(2e6, 2e7), "rng": random.Random(time.time() + len(self.st))}
            if sym not in INDICES:   # plausible partial-day volume so relative volume is meaningful in demo
                self.st[sym]["vol"] = int(self.st[sym]["avg_vol"] * r.uniform(0.3, 1.5))
                self.st[sym]["pv"] = ltp * self.st[sym]["vol"]
        for sym in INDEX_UNDERLYINGS + STOCKS:
            if sym in self.st:
                r = random.Random("oi" + sym)
                self.fut[sym] = {"oi": r.uniform(2e5, 5e6), "drift": r.gauss(0, 0.0006), "vol": 0, "rng": random.Random(sym + "f")}

    def lot_size(self, sym):
        return {"NIFTY": 75, "BANKNIFTY": 30, "FINNIFTY": 65, "MIDCPNIFTY": 120, "SENSEX": 20}.get(sym, 500)

    def _fut_expiry(self):
        today = date.today()
        e = _last_weekday_of_month(today.year, today.month, 1)
        if e < today:
            e = _last_weekday_of_month(today.year + (today.month == 12), today.month % 12 + 1, 1)
        return e.isoformat()

    async def snapshot(self):
        now = time.time()
        quotes, futures = {}, {}
        for sym, s in self.st.items():
            r = s["rng"]
            vol_pct = 0.003 if sym == "INDIAVIX" else (0.0002 if sym in INDICES else 0.0005)
            s["ltp"] *= 1 + r.gauss(0, vol_pct)
            dv = 0 if sym in INDICES else int(s["avg_vol"] * r.uniform(0.0002, 0.001))
            s["vol"] += dv
            s["pv"] += s["ltp"] * dv
            s["high"], s["low"] = max(s["high"], s["ltp"]), min(s["low"], s["ltp"])
            vw = s["pv"] / s["vol"] if s["vol"] else None
            kind = "index" if sym in INDICES else "stock"
            quotes[sym] = _quote(sym, INDICES.get(sym, {}).get("name", sym), kind, s["ltp"], s["prev"], s["vol"], vw,
                                 s["open"], s["high"], s["low"], ts=now)
            f = self.fut.get(sym)
            if f:
                f["oi"] *= 1 + f["rng"].gauss(f["drift"], 0.0012)
                f["vol"] += int(s["avg_vol"] * 0.0004 * f["rng"].uniform(0.5, 1.5)) + 1
                fq = _quote(sym, sym + " FUT", "future", s["ltp"] * 1.002, s["prev"] * 1.002, f["vol"], None,
                            oi=f["oi"], ts=now)
                fq["expiry"] = self._fut_expiry()
                futures[sym] = fq
        return {"quotes": quotes, "futures": futures, "ts": now}

    # ---- synthetic candles ------------------------------------------------------
    def _session_days(self, n):
        days, d = [], datetime.now(IST).date()
        now = datetime.now(IST)
        while len(days) < n:
            if d.weekday() < 5 and not (d == now.date() and now.time() < datetime(2000, 1, 1, 9, 15).time()):
                days.append(d)
            d -= timedelta(days=1)
        return sorted(days)

    async def candles(self, sym, interval):
        s = self.st[sym]
        rng = random.Random(f"{sym}{interval}{date.today()}")
        vol_scale = s["avg_vol"] if sym not in INDICES else 0
        out = []
        if interval == "1d":
            days = self._session_days(260)
            px = 1.0
            for d in days:
                o = px
                px *= 1 + rng.gauss(0.0004, 0.012)
                h, l = max(o, px) * (1 + abs(rng.gauss(0, 0.004))), min(o, px) * (1 - abs(rng.gauss(0, 0.004)))
                t = int(datetime(d.year, d.month, d.day, tzinfo=IST).timestamp())
                out.append({"t": t, "o": o, "h": h, "l": l, "c": px, "v": vol_scale * rng.uniform(0.6, 1.4)})
        else:
            step = int(interval[:-1])
            nbars = 375 // step
            now = time.time()
            px = 1.0
            for d in self._session_days(6):
                start = int(datetime(d.year, d.month, d.day, 9, 15, tzinfo=IST).timestamp())
                for i in range(nbars):
                    t = start + i * step * 60
                    if t > now:
                        break
                    o = px
                    px *= 1 + rng.gauss(0, 0.0007 * math.sqrt(step / 5))
                    h, l = max(o, px) * (1 + abs(rng.gauss(0, 0.0003))), min(o, px) * (1 - abs(rng.gauss(0, 0.0003)))
                    out.append({"t": t, "o": o, "h": h, "l": l, "c": px,
                                "v": vol_scale * 0.0025 * step / 5 * rng.uniform(0.3, 2.0)})
        if not out:
            return out
        k = s["ltp"] / out[-1]["c"]
        for c in out:
            for f in ("o", "h", "l", "c"):
                c[f] *= k
        return out

    # ---- synthetic option chain -------------------------------------------------
    async def expiries(self, sym):
        if sym in INDEX_UNDERLYINGS:
            e = _next_weekday(date.today(), 1)
            return [(e + timedelta(days=7 * i)).isoformat() for i in range(4)]
        e = _last_weekday_of_month(date.today().year, date.today().month, 1)
        if e < date.today():
            e = _last_weekday_of_month(date.today().year + (date.today().month == 12), date.today().month % 12 + 1, 1)
        return [e.isoformat()]

    async def option_chain(self, sym, expiry):
        spot = self.st[sym]["ltp"]
        step = self.STEP.get(sym) or max(round(spot * 0.01 / 5) * 5, 5)
        atm = round(spot / step) * step
        exp_dt = datetime.fromisoformat(expiry).replace(hour=15, minute=30, tzinfo=IST)
        t_years = max((exp_dt.timestamp() - time.time()) / (365 * 86400), 0.5 / 365)
        rows = []
        for i in range(-15, 16):
            k = atm + i * step
            book = self.oi_book.setdefault((sym, expiry, k), None)
            if book is None:
                r = random.Random(f"{sym}{expiry}{k}")
                peak = math.exp(-abs(i) / 7)
                book = {"ce": 1e5 * r.uniform(0.5, 4) * peak * (1.4 if i > 0 else 0.8) * 10,
                        "pe": 1e5 * r.uniform(0.5, 4) * peak * (1.4 if i < 0 else 0.8) * 10}
                book["ce0"], book["pe0"] = book["ce"], book["pe"]
                self.oi_book[(sym, expiry, k)] = book
            rr = random.Random()
            book["ce"] *= 1 + rr.gauss(0, 0.004)
            book["pe"] *= 1 + rr.gauss(0, 0.004)
            iv = 0.13 + 0.015 * abs(i) / 5
            c = ind.bs_greeks(spot, k, t_years, iv, True)
            p = ind.bs_greeks(spot, k, t_years, iv, False)
            rows.append({"strike": k,
                         "ce_ltp": round(c["price"], 2), "ce_oi": int(book["ce"]), "ce_prev_oi": int(book["ce0"]),
                         "ce_vol": int(book["ce"] * 0.3), "ce_iv": iv * 100, "ce_delta": c["delta"],
                         "ce_gamma": c["gamma"], "ce_theta": c["theta"], "ce_vega": c["vega"],
                         "pe_ltp": round(p["price"], 2), "pe_oi": int(book["pe"]), "pe_prev_oi": int(book["pe0"]),
                         "pe_vol": int(book["pe"] * 0.3), "pe_iv": iv * 100, "pe_delta": p["delta"],
                         "pe_gamma": p["gamma"], "pe_theta": p["theta"], "pe_vega": p["vega"]})
        return {"spot": spot, "rows": rows, "ts": time.time()}

    async def global_cues(self):
        r = random.Random(int(time.time() // 30))
        items = [{"name": n, "ltp": None, "pct": r.gauss(0, 0.6)} for n, _ in GLOBAL_YAHOO]
        return {"items": items, "source": "DEMO - synthetic", "ts": time.time()}


def make_provider():
    choice = os.getenv("DATA_PROVIDER", "").lower()
    token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
    if choice == "yahoo":
        return YahooProvider()
    if choice == "demo" or (not choice and not token):
        return DemoProvider()
    if not token:
        raise ProviderError("DATA_PROVIDER=upstox requires UPSTOX_ACCESS_TOKEN.")
    return UpstoxProvider(token, Path(os.getenv("CACHE_DIR", "./.cache")))
