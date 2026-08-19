# app.py
# ================================================================
# MARKET INTELLIGENCE DASHBOARD v3
# Streamlit + D&S + Technicals + News + Macro + India Events
# + Nifty PCR/OI + Stock F&O PCR/OI + FII/DII + Auto Hypothesis
# ================================================================

import io
import re
import time
import math
import urllib.parse
import concurrent.futures
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, time as dtime
from typing import Optional, Dict, List, Any, Tuple

import numpy as np
import pandas as pd
import requests
import streamlit as st
import yfinance as yf

try:
    import feedparser
except ImportError:
    feedparser = None

try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except ImportError:
    HAS_AUTOREFRESH = False


# ================================================================
# 1. APP CONFIG
# ================================================================

st.set_page_config(
    page_title="Institutional Market Intelligence",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

IST = timezone(timedelta(hours=5, minutes=30))

NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

RAW_STOCKS = """
RELIANCE,HDFCBANK,ICICIBANK,SBIN,AXISBANK,KOTAKBANK,
TCS,INFY,HCLTECH,WIPRO,TECHM,
BHARTIARTL,ITC,LT,BAJFINANCE,
TATAMOTORS,MARUTI,M&M,
TATASTEEL,JSWSTEEL,HINDALCO,
SUNPHARMA,CIPLA,DRREDDY,
ONGC,BPCL,IOC,COALINDIA,NTPC,
ADANIENT,ADANIPORTS,POWERGRID
"""

WATCHLIST = list(
    dict.fromkeys(
        x.strip()
        for x in RAW_STOCKS.replace("\n", "").split(",")
        if x.strip()
    )
)

YF_FIX = {
    "BAJAJ_AUTO": "BAJAJ-AUTO",
}

TV_FIX = {
    "BAJAJ_AUTO": "BAJAJ-AUTO",
}

GLOBAL_SYMBOLS = {
    "S&P500": "^GSPC",
    "NASDAQ": "^IXIC",
    "Dow": "^DJI",
    "DXY": "DX-Y.NYB",
    "US10Y": "^TNX",
    "USDINR": "INR=X",
    "Crude": "CL=F",
    "Gold": "GC=F",
    "Silver": "SI=F",
    "Copper": "HG=F",
    "Nikkei": "^N225",
    "FTSE": "^FTSE",
}

TIMEFRAMES = {
    "5 Min":  {"interval": "5m",  "period": "5d",  "resample": None},
    "15 Min": {"interval": "5m",  "period": "5d",  "resample": "15min"},
    "1 Hour": {"interval": "60m", "period": "1mo", "resample": None},
    "Daily":  {"interval": "1d",  "period": "6mo", "resample": None},
}

# D&S
ATR_PERIOD = 14
LEG_OUT_ATR_MULT = 1.2
HQ_LEG_OUT_ATR = 2.0
MAX_BASE_ATR_MULT = 1.0
MAX_WICK_PCT = 0.25

BASE_MAX_BODY_RATIO = 0.35
BASE_MIN_OVERLAP = 0.50
LEG_OUT_VOL_MULT = 1.5

MIN_BASE = 1
MAX_BASE = 3

TARGET_RR = 3.0
SL_ATR_BUFFER = 0.10


# ================================================================
# 2. UTILITIES
# ================================================================

def now_ist():
    return datetime.now(IST)


def yf_stock(symbol: str) -> str:
    return f"{YF_FIX.get(symbol, symbol)}.NS"


def tv_stock(symbol: str) -> str:
    s = TV_FIX.get(symbol, symbol)
    return (
        "https://www.tradingview.com/chart/?symbol="
        + urllib.parse.quote(f"NSE:{s}")
    )


def safe_float(x, default=0.0):
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def flatten_yf(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    if isinstance(df.columns, pd.MultiIndex):
        if len(df.columns.levels) == 2:
            # Single ticker edge case
            try:
                df.columns = df.columns.get_level_values(0)
            except Exception:
                pass

    return df


# ================================================================
# 3. ROBUST HTTP
# ================================================================

def request_json(
    url: str,
    session: Optional[requests.Session] = None,
    timeout=10,
    retries=2,
):
    s = session or requests.Session()
    s.headers.update(NSE_HEADERS)

    for attempt in range(retries + 1):
        try:
            r = s.get(url, timeout=timeout)
            r.raise_for_status()

            ct = r.headers.get("content-type", "")
            if "json" in ct.lower():
                return r.json()

            return r.json()

        except Exception:
            if attempt == retries:
                return None
            time.sleep(0.5 * (attempt + 1))

    return None


@st.cache_data(ttl=180, show_spinner=False)
def nse_json(path: str):
    """
    NSE cookie/session bootstrap + retry.
    """
    s = requests.Session()
    s.headers.update(NSE_HEADERS)

    try:
        s.get("https://www.nseindia.com/", timeout=8)
    except Exception:
        pass

    return request_json(
        f"https://www.nseindia.com{path}",
        session=s,
        timeout=10,
        retries=2,
    )


# ================================================================
# 4. YAHOO BATCH DOWNLOAD
# ================================================================

@st.cache_data(ttl=120, show_spinner=False)
def yf_batch(
    symbols: Tuple[str, ...],
    period: str,
    interval: str,
) -> Dict[str, pd.DataFrame]:

    if not symbols:
        return {}

    try:
        raw = yf.download(
            list(symbols),
            period=period,
            interval=interval,
            group_by="ticker",
            threads=True,
            auto_adjust=False,
            progress=False,
            timeout=15,
        )
    except Exception:
        return {}

    result = {}

    if raw is None or raw.empty:
        return result

    for symbol in symbols:
        try:
            if isinstance(raw.columns, pd.MultiIndex):
                # group_by=ticker
                if symbol in raw.columns.get_level_values(0):
                    df = raw[symbol].copy()
                else:
                    continue
            else:
                df = raw.copy()

            needed = ["Open", "High", "Low", "Close", "Volume"]
            available = [x for x in needed if x in df.columns]

            if "Close" not in available:
                continue

            df = df[available].dropna(subset=["Close"])

            if not df.empty:
                result[symbol] = df

        except Exception:
            continue

    return result


# ================================================================
# 5. INDICATORS
# ================================================================

def ema(arr: np.ndarray, period: int) -> np.ndarray:
    if len(arr) == 0:
        return arr

    alpha = 2.0 / (period + 1)
    out = np.empty(len(arr), dtype=float)
    out[0] = arr[0]

    for i in range(1, len(arr)):
        out[i] = alpha * arr[i] + (1 - alpha) * out[i - 1]

    return out


def atr_np(h, l, c, period=14):
    n = len(c)

    if n == 0:
        return np.array([])

    tr = np.empty(n)
    tr[0] = h[0] - l[0]

    if n > 1:
        tr[1:] = np.maximum(
            h[1:] - l[1:],
            np.maximum(
                abs(h[1:] - c[:-1]),
                abs(l[1:] - c[:-1]),
            ),
        )

    result = np.empty(n)
    result[0] = tr[0]

    alpha = 1 / period

    for i in range(1, n):
        result[i] = (
            alpha * tr[i]
            + (1 - alpha) * result[i - 1]
        )

    return result


def rsi_value(close: np.ndarray, period=14):
    if len(close) < period + 1:
        return None

    d = np.diff(close)
    gains = np.maximum(d, 0)
    losses = np.maximum(-d, 0)

    ag = gains[-period:].mean()
    al = losses[-period:].mean()

    if al == 0:
        return 100.0

    rs = ag / al
    return 100 - 100 / (1 + rs)


def technical_snapshot(df: pd.DataFrame) -> Dict[str, Any]:
    if df is None or len(df) < 55:
        return {}

    close = df["Close"].to_numpy(float)
    volume = df["Volume"].to_numpy(float)

    e3 = ema(close, 3)
    e5 = ema(close, 5)
    e20 = ema(close, 20)
    e50 = ema(close, 50)

    rsi = rsi_value(close)

    vol_ratio = 0
    if len(volume) >= 21:
        base = np.mean(volume[-21:-1])
        if base > 0:
            vol_ratio = volume[-1] / base

    score = 0.0
    reasons = []

    if e20[-1] > e50[-1]:
        score += 1.2
        reasons.append("EMA20 > EMA50")
    else:
        score -= 1.2
        reasons.append("EMA20 < EMA50")

    if e3[-1] > e5[-1]:
        score += 0.4
        reasons.append("Short momentum bullish")
    else:
        score -= 0.4
        reasons.append("Short momentum bearish")

    if rsi is not None:
        if 52 <= rsi <= 68:
            score += 0.5
        elif 32 <= rsi <= 48:
            score -= 0.5
        elif rsi >= 75:
            score -= 0.3
            reasons.append("RSI stretched")
        elif rsi <= 25:
            score += 0.3
            reasons.append("RSI oversold")

    return {
        "score": score,
        "rsi": rsi,
        "volume_ratio": vol_ratio,
        "price": close[-1],
        "reasons": reasons,
    }


# ================================================================
# 6. CONFIRMED PIVOTS - NO LOOKAHEAD
# ================================================================

def confirmed_pivots(
    highs: np.ndarray,
    lows: np.ndarray,
    left=5,
    right=5,
):
    """
    Signal becomes available only at pivot_index + right.

    This avoids making a historical pivot available before
    the right-side candles actually existed.
    """

    n = len(highs)

    available_high = np.full(n, np.nan)
    available_low = np.full(n, np.nan)

    for pivot_i in range(left, n - right):
        hwin = highs[pivot_i-left:pivot_i+right+1]
        lwin = lows[pivot_i-left:pivot_i+right+1]

        confirmation_i = pivot_i + right

        if (
            highs[pivot_i] == np.max(hwin)
            and np.sum(hwin == highs[pivot_i]) == 1
        ):
            available_high[confirmation_i] = highs[pivot_i]

        if (
            lows[pivot_i] == np.min(lwin)
            and np.sum(lwin == lows[pivot_i]) == 1
        ):
            available_low[confirmation_i] = lows[pivot_i]

    return available_high, available_low


# ================================================================
# 7. D&S ENGINE
# ================================================================

@dataclass
class Zone:
    prox: float
    distal: float
    sl: float
    tp: float
    demand: bool
    score: int
    created_idx: int

    state: str = "Fresh"
    touches: int = 0


def base_overlap_ok(high, low):
    if len(high) <= 1:
        return True

    for i in range(1, len(high)):
        overlap = min(high[i-1], high[i]) - max(low[i-1], low[i])
        min_range = min(
            high[i-1] - low[i-1],
            high[i] - low[i],
        )

        if min_range <= 0:
            return False

        if overlap / min_range < BASE_MIN_OVERLAP:
            return False

    return True


def strong_close(o, h, l, c, bull):
    rng = h - l

    if rng <= 0:
        return False

    if bull:
        return (c - l) / rng >= 0.70

    return (h - c) / rng >= 0.70


def scan_ds(df: pd.DataFrame) -> List[Zone]:
    if df is None or len(df) < 40:
        return []

    o = df["Open"].to_numpy(float)
    h = df["High"].to_numpy(float)
    l = df["Low"].to_numpy(float)
    c = df["Close"].to_numpy(float)
    v = df["Volume"].to_numpy(float)

    n = len(c)

    atr = atr_np(h, l, c, ATR_PERIOD)
    tr = h - l

    bull = c > o
    bear = c < o

    body = abs(c - o)

    body_hi = np.maximum(o, c)
    body_lo = np.minimum(o, c)

    wick = (h - body_hi) + (body_lo - l)

    wick_pct = np.divide(
        wick,
        tr,
        out=np.zeros(n),
        where=tr > 0,
    )

    ph_available, pl_available = confirmed_pivots(h, l)

    last_swing_high = np.nan
    last_swing_low = np.nan

    zones = []

    for i in range(25, n):

        # Confirmed only now
        if not np.isnan(ph_available[i]):
            last_swing_high = ph_available[i]

        if not np.isnan(pl_available[i]):
            last_swing_low = pl_available[i]

        new_zone = None

        for base_count in range(MIN_BASE, MAX_BASE + 1):

            legout = i
            legin = i - base_count - 1

            if legin < 20:
                continue

            bs = slice(legin + 1, legout)

            bh = h[bs]
            bl = l[bs]
            bo = o[bs]
            bc = c[bs]
            bv = v[bs]
            btr = tr[bs]
            batr = atr[bs]

            if len(bh) == 0:
                continue

            # Tight base
            if not np.all(btr <= MAX_BASE_ATR_MULT * batr):
                continue

            # Small bodies
            ranges = bh - bl

            ratios = np.divide(
                abs(bc - bo),
                ranges,
                out=np.zeros(len(ranges)),
                where=ranges > 0,
            )

            if np.any(ratios > BASE_MAX_BODY_RATIO):
                continue

            if not base_overlap_ok(bh, bl):
                continue

            if np.mean(bv) > v[legin]:
                continue

            legout_direction_bull = bull[legout]

            if not strong_close(
                o[legin],
                h[legin],
                l[legin],
                c[legin],
                bull[legin],
            ):
                continue

            if tr[legout] < LEG_OUT_ATR_MULT * atr[legout]:
                continue

            if wick_pct[legout] > MAX_WICK_PCT:
                continue

            if not (
                tr[legout] > tr[legin] > np.max(btr)
            ):
                continue

            avg20 = np.mean(v[max(0, legout-20):legout])

            if avg20 <= 0:
                continue

            if v[legout] < LEG_OUT_VOL_MULT * avg20:
                continue

            base_high = float(np.max(bh))
            base_low = float(np.min(bl))

            if legout_direction_bull:
                bos = c[legout] > max(h[legin], base_high)

                sweep = (
                    not np.isnan(last_swing_low)
                    and min(base_low, l[legin]) < last_swing_low
                )

                if not bos:
                    continue

                prox = base_high
                distal = base_low

            else:
                bos = c[legout] < min(l[legin], base_low)

                sweep = (
                    not np.isnan(last_swing_high)
                    and max(base_high, h[legin]) > last_swing_high
                )

                if not bos:
                    continue

                prox = base_low
                distal = base_high

            score = 50

            if tr[legout] >= HQ_LEG_OUT_ATR * atr[legout]:
                score += 20

            if sweep:
                score += 20

            if base_count <= 2:
                score += 10

            sl = (
                distal - SL_ATR_BUFFER * atr[legout]
                if legout_direction_bull
                else distal + SL_ATR_BUFFER * atr[legout]
            )

            risk = abs(prox - sl)

            tp = (
                prox + TARGET_RR * risk
                if legout_direction_bull
                else prox - TARGET_RR * risk
            )

            duplicate = any(
                z.demand == legout_direction_bull
                and abs(z.prox - prox) < atr[legout] * 0.25
                for z in zones[-10:]
            )

            if not duplicate:
                new_zone = Zone(
                    prox=prox,
                    distal=distal,
                    sl=sl,
                    tp=tp,
                    demand=legout_direction_bull,
                    score=score,
                    created_idx=i,
                )

                zones.append(new_zone)

            break

        # IMPORTANT BUG FIX:
        # only pre-existing zones can be touched on this candle.
        # Newly-created formation candle cannot instantly retest itself.

        for z in zones:

            if z.created_idx >= i:
                continue

            if z.state == "Filled":
                continue

            if z.demand:
                touched = l[i] <= z.prox
                consumed = l[i] <= z.distal
            else:
                touched = h[i] >= z.prox
                consumed = h[i] >= z.distal

            if consumed:
                z.state = "Filled"

            elif touched:
                z.state = "Retest"
                z.touches += 1

    return zones


def nearest_ds_score(df):
    zones = scan_ds(df)

    if not zones:
        return 0.0, None

    price = float(df["Close"].iloc[-1])

    best = None
    best_distance = float("inf")

    for z in zones:

        if z.state == "Filled":
            continue

        distance = abs(price - z.prox) / max(z.prox, 1e-9)

        if distance < best_distance:
            best = z
            best_distance = distance

    if best is None:
        return 0.0, None

    # only relevant within 2%
    if best_distance > 0.02:
        return 0.0, None

    quality = best.score / 100

    # stronger as price approaches zone
    proximity = max(0.3, 1 - best_distance / 0.02)

    score = quality * proximity * 2.5

    if best.demand:
        return score, best

    return -score, best


# ================================================================
# 8. NEWS ENGINE
# ================================================================

POSITIVE_WORDS = {
    "wins", "order", "growth", "profit", "upgrade",
    "approval", "approved", "record", "expansion",
    "acquisition", "dividend", "buyback", "strong",
    "beats", "surge", "partnership", "contract",
}

NEGATIVE_WORDS = {
    "loss", "fraud", "downgrade", "probe", "penalty",
    "default", "weak", "misses", "decline", "fall",
    "lawsuit", "investigation", "warning", "cuts",
    "slump", "fire", "accident",
}


def headline_sentiment(title: str) -> float:
    """
    Conservative lexical event classifier.
    Not pretending to be an LLM sentiment model.
    """

    text = re.sub(r"[^a-zA-Z ]", " ", title.lower())
    words = set(text.split())

    pos = len(words & POSITIVE_WORDS)
    neg = len(words & NEGATIVE_WORDS)

    raw = pos - neg

    return float(np.clip(raw * 0.35, -1.0, 1.0))


@st.cache_data(ttl=900, show_spinner=False)
def stock_news(symbol: str):
    if feedparser is None:
        return []

    query = urllib.parse.quote_plus(
        f'"{symbol}" NSE OR India stock when:1d'
    )

    url = (
        "https://news.google.com/rss/search?"
        f"q={query}&hl=en-IN&gl=IN&ceid=IN:en"
    )

    try:
        r = requests.get(
            url,
            headers=NSE_HEADERS,
            timeout=8,
        )

        feed = feedparser.parse(r.content)

        result = []

        for e in feed.entries[:5]:

            title = e.get("title", "").strip()

            if not title:
                continue

            result.append({
                "title": title,
                "url": e.get("link", ""),
                "sentiment": headline_sentiment(title),
            })

        return result

    except Exception:
        return []


def fetch_all_news(stocks):
    result = {}

    workers = min(12, max(1, len(stocks)))

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=workers
    ) as pool:

        fut = {
            pool.submit(stock_news, s): s
            for s in stocks
        }

        for f in concurrent.futures.as_completed(fut):

            s = fut[f]

            try:
                result[s] = f.result()
            except Exception:
                result[s] = []

    return result


def news_score(news):
    if not news:
        return 0.0

    values = [
        n["sentiment"]
        for n in news[:3]
    ]

    if not values:
        return 0

    # Recent headlines matter but capped
    return float(
        np.clip(
            np.mean(values) * 1.5,
            -1.5,
            1.5,
        )
    )


# ================================================================
# 9. FII/DII
# ================================================================

@st.cache_data(ttl=900, show_spinner=False)
def get_fii_dii():
    data = nse_json("/api/fiidiiTradeReact")

    if not data:
        return {
            "fii": 0.0,
            "dii": 0.0,
            "available": False,
        }

    fii = 0
    dii = 0

    try:
        for row in data:

            category = str(
                row.get("category", "")
            ).upper()

            value = safe_float(
                row.get(
                    "netValue",
                    row.get("netvalue", 0),
                )
            )

            if "FII" in category or "FPI" in category:
                fii = value

            elif "DII" in category:
                dii = value

        return {
            "fii": fii,
            "dii": dii,
            "available": True,
        }

    except Exception:
        return {
            "fii": 0,
            "dii": 0,
            "available": False,
        }


# ================================================================
# 10. OPTION CHAIN
# ================================================================

def parse_option_chain(data):
    if not data:
        return {}

    try:
        records = data["records"]["data"]

        call_total = 0
        put_total = 0

        call_map = {}
        put_map = {}

        call_change = 0
        put_change = 0

        for r in records:

            strike = r.get("strikePrice")

            ce = r.get("CE")
            pe = r.get("PE")

            if ce:
                oi = safe_float(ce.get("openInterest"))
                coi = safe_float(ce.get("changeinOpenInterest"))

                call_total += oi
                call_change += coi

                call_map[strike] = (
                    call_map.get(strike, 0) + oi
                )

            if pe:
                oi = safe_float(pe.get("openInterest"))
                poi = safe_float(pe.get("changeinOpenInterest"))

                put_total += oi
                put_change += poi

                put_map[strike] = (
                    put_map.get(strike, 0) + oi
                )

        pcr = (
            put_total / call_total
            if call_total > 0
            else None
        )

        support = (
            max(put_map, key=put_map.get)
            if put_map else None
        )

        resistance = (
            max(call_map, key=call_map.get)
            if call_map else None
        )

        underlying = safe_float(
            data.get(
                "records",
                {}
            ).get(
                "underlyingValue",
                0
            )
        )

        return {
            "pcr": pcr,
            "call_oi": call_total,
            "put_oi": put_total,
            "call_change_oi": call_change,
            "put_change_oi": put_change,
            "support": support,
            "resistance": resistance,
            "spot": underlying,
        }

    except Exception:
        return {}


@st.cache_data(ttl=120, show_spinner=False)
def nifty_option_chain():
    return parse_option_chain(
        nse_json(
            "/api/option-chain-indices?symbol=NIFTY"
        )
    )


@st.cache_data(ttl=180, show_spinner=False)
def stock_option_chain(symbol):
    data = nse_json(
        f"/api/option-chain-equities?symbol="
        f"{urllib.parse.quote(symbol)}"
    )

    return parse_option_chain(data)


def option_score(oc):
    """
    PCR is context, not a standalone directional certainty.
    Change-in-OI is included to avoid using only absolute OI.
    """

    if not oc:
        return 0.0, []

    score = 0
    reasons = []

    pcr = oc.get("pcr")

    if pcr is not None:

        if 1.05 <= pcr <= 1.6:
            score += 0.7
            reasons.append(f"PCR {pcr:.2f} supportive")

        elif pcr < 0.75:
            score -= 0.7
            reasons.append(f"PCR {pcr:.2f} call-heavy")

        elif pcr > 1.8:
            # Extreme PCR should not automatically be interpreted
            # as strongly bullish.
            reasons.append(f"PCR extreme {pcr:.2f}")

    pcoi = oc.get("put_change_oi", 0)
    ccoi = oc.get("call_change_oi", 0)

    denom = abs(pcoi) + abs(ccoi)

    if denom > 0:
        delta_bias = (pcoi - ccoi) / denom

        score += np.clip(
            delta_bias,
            -0.6,
            0.6,
        )

    return float(score), reasons


# ================================================================
# 11. GLOBAL MACRO
# ================================================================

@st.cache_data(ttl=180, show_spinner=False)
def global_macro():
    symbols = tuple(
        GLOBAL_SYMBOLS.values()
    )

    data = yf_batch(
        symbols,
        "5d",
        "1d",
    )

    result = {}

    for name, ticker in GLOBAL_SYMBOLS.items():

        df = data.get(ticker)

        if df is None or len(df) < 2:
            continue

        last = safe_float(df["Close"].iloc[-1])
        prev = safe_float(df["Close"].iloc[-2])

        pct = (
            (last - prev) / prev * 100
            if prev else 0
        )

        result[name] = {
            "price": last,
            "pct": pct,
        }

    return result


def macro_score_for_stock(
    symbol: str,
    macro: Dict[str, Any],
):
    score = 0.0
    reasons = []

    sp = macro.get("S&P500", {}).get("pct", 0)
    nasdaq = macro.get("NASDAQ", {}).get("pct", 0)
    dxy = macro.get("DXY", {}).get("pct", 0)
    crude = macro.get("Crude", {}).get("pct", 0)
    us10 = macro.get("US10Y", {}).get("pct", 0)
    usdinr = macro.get("USDINR", {}).get("pct", 0)
    copper = macro.get("Copper", {}).get("pct", 0)

    # Broad risk
    if sp > 0.5:
        score += 0.25

    elif sp < -0.5:
        score -= 0.25

    if dxy > 0.4:
        score -= 0.20
        reasons.append("Strong DXY risk")

    elif dxy < -0.4:
        score += 0.15

    if us10 > 1:
        score -= 0.15

    # IT/export
    IT = {
        "TCS", "INFY", "HCLTECH",
        "WIPRO", "TECHM",
    }

    if symbol in IT:

        if nasdaq > 0.5:
            score += 0.45
            reasons.append("NASDAQ positive")

        elif nasdaq < -0.5:
            score -= 0.45
            reasons.append("NASDAQ weak")

        if usdinr > 0.20:
            score += 0.25
            reasons.append("Weak INR supports exporters")

    # Oil
    UPSTREAM = {"ONGC"}
    OMC = {"IOC", "BPCL"}

    if symbol in UPSTREAM:

        if crude > 1:
            score += 0.45

        elif crude < -1:
            score -= 0.45

    if symbol in OMC:

        if crude < -1:
            score += 0.45

        elif crude > 1:
            score -= 0.45

    # Metals
    METALS = {
        "TATASTEEL",
        "JSWSTEEL",
        "HINDALCO",
    }

    if symbol in METALS:

        if copper > 1:
            score += 0.4
            reasons.append("Base metals supportive")

        elif copper < -1:
            score -= 0.4

    return score, reasons


# ================================================================
# 12. INDIA ECONOMIC EVENTS
# ================================================================

@st.cache_data(ttl=1800, show_spinner=False)
def india_events_today():
    """
    ForexFactory public calendar feed is used as one source.
    If unavailable, engine fails neutral instead of inventing data.
    """

    url = (
        "https://nfs.faireconomy.media/"
        "ff_calendar_thisweek.json"
    )

    try:
        r = requests.get(
            url,
            headers=NSE_HEADERS,
            timeout=10,
        )

        r.raise_for_status()

        events = r.json()

    except Exception:
        return []

    today = now_ist().date()

    out = []

    for event in events:

        country = str(
            event.get("country", "")
        ).upper()

        if country not in {"IN", "IND"}:
            continue

        try:
            dt = datetime.fromisoformat(
                event["date"].replace(
                    "Z",
                    "+00:00",
                )
            ).astimezone(IST)

        except Exception:
            continue

        if dt.date() != today:
            continue

        out.append({
            "title": event.get("title", ""),
            "impact": event.get("impact", ""),
            "time": dt.strftime("%H:%M"),
            "forecast": event.get("forecast", ""),
            "previous": event.get("previous", ""),
        })

    return out


def india_event_risk(events):
    """
    Economic-calendar events are treated primarily as uncertainty/risk.
    Without actual-vs-forecast data, assigning bullish/bearish direction
    would be misleading.
    """

    high = sum(
        1 for e in events
        if str(e["impact"]).lower() == "high"
    )

    medium = sum(
        1 for e in events
        if str(e["impact"]).lower() == "medium"
    )

    if high:
        return "HIGH", high, medium

    if medium:
        return "MEDIUM", high, medium

    return "LOW", high, medium


# ================================================================
# 13. TIMEFRAME DATA
# ================================================================

@st.cache_data(ttl=120, show_spinner=False)
def fetch_tf(
    stocks: Tuple[str, ...],
    tf_name: str,
):
    cfg = TIMEFRAMES[tf_name]

    mapping = {
        s: yf_stock(s)
        for s in stocks
    }

    data = yf_batch(
        tuple(mapping.values()),
        cfg["period"],
        cfg["interval"],
    )

    out = {}

    for stock, ticker in mapping.items():

        df = data.get(ticker)

        if df is None or df.empty:
            continue

        df = df.copy()

        if cfg["resample"]:

            try:
                df = df.resample(
                    cfg["resample"]
                ).agg({
                    "Open": "first",
                    "High": "max",
                    "Low": "min",
                    "Close": "last",
                    "Volume": "sum",
                }).dropna()

            except Exception:
                continue

        if len(df) >= 55:
            out[stock] = df

    return out


def fetch_timeframes_parallel(
    stocks,
    timeframes,
):
    results = {}

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(6, len(timeframes))
    ) as ex:

        futures = {
            ex.submit(
                fetch_tf,
                tuple(stocks),
                tf,
            ): tf
            for tf in timeframes
        }

        for fut in concurrent.futures.as_completed(
            futures
        ):

            tf = futures[fut]

            try:
                results[tf] = fut.result()

            except Exception:
                results[tf] = {}

    return results


# ================================================================
# 14. HYPOTHESIS MODEL
# ================================================================

TF_WEIGHT = {
    "5 Min": 0.5,
    "15 Min": 0.8,
    "1 Hour": 1.3,
    "Daily": 1.8,
}


def confidence_from_score(
    score,
    sources,
    event_risk,
):
    """
    Confidence is heuristic calibration, not statistical probability.
    """

    raw = (
        45
        + min(abs(score) * 7, 35)
        + min(sources * 2, 10)
    )

    if event_risk == "HIGH":
        raw -= 8

    elif event_risk == "MEDIUM":
        raw -= 3

    return int(
        np.clip(raw, 35, 92)
    )


def build_hypothesis(
    stocks,
    tf_data,
    all_news,
    stock_oi,
    nifty_oi,
    macro,
    fii_dii,
    india_events,
):
    output = []

    event_risk, high_events, medium_events = (
        india_event_risk(india_events)
    )

    nifty_score, nifty_reasons = (
        option_score(nifty_oi)
    )

    fii_score = 0

    if fii_dii["available"]:

        if fii_dii["fii"] > 500:
            fii_score += 0.35

        elif fii_dii["fii"] < -500:
            fii_score -= 0.35

    for stock in stocks:

        total = 0.0
        bullish = []
        bearish = []
        sources = 0

        # --------------------------------
        # Technical + D&S MTF
        # --------------------------------

        for tf, stock_map in tf_data.items():

            df = stock_map.get(stock)

            if df is None:
                continue

            tech = technical_snapshot(df)

            if tech:
                s = (
                    tech["score"]
                    * TF_WEIGHT.get(tf, 1)
                )

                total += s
                sources += 1

                if s > 0:
                    bullish.append(
                        f"{tf} technical"
                    )

                elif s < 0:
                    bearish.append(
                        f"{tf} technical"
                    )

            # D&S: more expensive; use H1/Daily
            # for hypothesis quality/performance balance.
            if tf in {"1 Hour", "Daily"}:

                ds_s, zone = nearest_ds_score(df)

                ds_s *= TF_WEIGHT[tf]

                total += ds_s

                if zone:
                    sources += 1

                    text = (
                        f"{tf} Demand "
                        if zone.demand
                        else f"{tf} Supply "
                    )

                    text += (
                        f"{zone.prox:.2f}"
                    )

                    if ds_s > 0:
                        bullish.append(text)
                    else:
                        bearish.append(text)

        # --------------------------------
        # Current stock news
        # --------------------------------

        ns = news_score(
            all_news.get(stock, [])
        )

        total += ns

        if all_news.get(stock):
            sources += 1

            if ns > 0.15:
                bullish.append(
                    "Positive current news/event"
                )

            elif ns < -0.15:
                bearish.append(
                    "Negative current news/event"
                )

        # --------------------------------
        # Stock Option OI / PCR
        # --------------------------------

        oi = stock_oi.get(stock, {})

        oi_score, oi_reasons = option_score(oi)

        total += oi_score * 1.2

        if oi:
            sources += 1

        if oi_score > 0.15:
            bullish.extend(oi_reasons[:1])

        elif oi_score < -0.15:
            bearish.extend(oi_reasons[:1])

        # --------------------------------
        # Nifty market OI
        # --------------------------------

        total += nifty_score * 0.45

        if nifty_oi:
            sources += 1

        # --------------------------------
        # FII
        # --------------------------------

        total += fii_score

        if fii_dii["available"]:
            sources += 1

        # --------------------------------
        # Macro - stock sensitive
        # --------------------------------

        ms, mr = macro_score_for_stock(
            stock,
            macro,
        )

        total += ms

        if mr:
            sources += 1

            if ms > 0:
                bullish.extend(mr)

            elif ms < 0:
                bearish.extend(mr)

        # --------------------------------
        # Conflict penalty
        # --------------------------------

        if bullish and bearish:
            total *= 0.92

        if total >= 1:
            side = "BUY"

        elif total <= -1:
            side = "SELL"

        else:
            side = "WAIT"

        confidence = confidence_from_score(
            total,
            sources,
            event_risk,
        )

        oi_pcr = oi.get("pcr")

        headlines = [
            n["title"]
            for n in all_news.get(stock, [])[:2]
        ]

        output.append({
            "Stock": stock,
            "Side": side,
            "Score": round(total, 2),
            "Confidence": confidence,
            "Stock PCR": (
                round(oi_pcr, 2)
                if oi_pcr is not None
                else None
            ),
            "Bull": bullish[:5],
            "Bear": bearish[:5],
            "News": headlines,
            "Chart": tv_stock(stock),
        })

    output.sort(
        key=lambda x: abs(x["Score"]),
        reverse=True,
    )

    return output


# ================================================================
# 15. STOCK OI PARALLEL
# ================================================================

def fetch_stock_oi_parallel(stocks):
    result = {}

    # NSE endpoints often throttle aggressive concurrency.
    # Keep workers conservative.
    workers = min(5, max(1, len(stocks)))

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=workers
    ) as pool:

        futures = {
            pool.submit(
                stock_option_chain,
                stock,
            ): stock
            for stock in stocks
        }

        for fut in concurrent.futures.as_completed(
            futures
        ):

            stock = futures[fut]

            try:
                result[stock] = fut.result()

            except Exception:
                result[stock] = {}

    return result


# ================================================================
# 16. FULL INTELLIGENCE SNAPSHOT
# ================================================================

def create_intelligence_snapshot(
    stocks,
    timeframes,
):

    # Independent workloads run together.

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=5
    ) as pool:

        f_tf = pool.submit(
            fetch_timeframes_parallel,
            stocks,
            timeframes,
        )

        f_news = pool.submit(
            fetch_all_news,
            stocks,
        )

        f_oi = pool.submit(
            fetch_stock_oi_parallel,
            stocks,
        )

        f_macro = pool.submit(
            global_macro,
        )

        f_events = pool.submit(
            india_events_today,
        )

        tf_data = f_tf.result()
        all_news = f_news.result()
        stock_oi = f_oi.result()
        macro = f_macro.result()
        events = f_events.result()

    nifty_oi = nifty_option_chain()
    fii = get_fii_dii()

    hypotheses = build_hypothesis(
        stocks,
        tf_data,
        all_news,
        stock_oi,
        nifty_oi,
        macro,
        fii,
        events,
    )

    return {
        "tf_data": tf_data,
        "news": all_news,
        "stock_oi": stock_oi,
        "macro": macro,
        "events": events,
        "nifty_oi": nifty_oi,
        "fii": fii,
        "hypotheses": hypotheses,
        "timestamp": now_ist(),
    }


# ================================================================
# 17. SIDEBAR + AUTO REFRESH
# ================================================================

st.sidebar.header("⚙️ Engine")

refresh_seconds = st.sidebar.slider(
    "Auto Refresh (seconds)",
    min_value=30,
    max_value=600,
    value=120,
    step=30,
)

if HAS_AUTOREFRESH:
    st_autorefresh(
        interval=refresh_seconds * 1000,
        key="market_auto_refresh",
    )
else:
    st.sidebar.warning(
        "streamlit-autorefresh install नहीं है।"
    )

selected = st.sidebar.multiselect(
    "Stocks",
    WATCHLIST,
    default=WATCHLIST[:20],
)

selected_tfs = st.sidebar.multiselect(
    "Timeframes",
    list(TIMEFRAMES),
    default=[
        "15 Min",
        "1 Hour",
        "Daily",
    ],
)

force = st.sidebar.button(
    "🔄 Force refresh"
)

if force:
    st.cache_data.clear()
    st.rerun()

st.sidebar.caption(
    f"IST: {now_ist():%d-%b-%Y %H:%M:%S}"
)


# ================================================================
# 18. DASHBOARD
# ================================================================

st.title(
    "📈 Institutional Market Intelligence Dashboard"
)

st.caption(
    "Live/near-live technical structure + D&S + "
    "News + F&O OI/PCR + FII/DII + Global Macro "
    "+ India Economic Events"
)

if not selected:
    st.warning(
        "कम से कम एक stock select करें।"
    )
    st.stop()

if not selected_tfs:
    st.warning(
        "कम से कम एक timeframe select करें।"
    )
    st.stop()


# Automatically runs every Streamlit rerun.
with st.spinner(
    "Current market intelligence update हो रहा है..."
):
    try:
        snapshot = create_intelligence_snapshot(
            selected,
            selected_tfs,
        )

    except Exception as exc:
        st.error(
            f"Dashboard update failed: {exc}"
        )
        st.stop()


# ================================================================
# 19. MARKET CONTEXT
# ================================================================

macro = snapshot["macro"]
nifty = snapshot["nifty_oi"]
fii = snapshot["fii"]
events = snapshot["events"]

risk, high_events, med_events = (
    india_event_risk(events)
)

c1, c2, c3, c4, c5 = st.columns(5)

c1.metric(
    "Nifty PCR",
    (
        f"{nifty['pcr']:.2f}"
        if nifty.get("pcr") is not None
        else "N/A"
    )
)

c2.metric(
    "Max Put OI",
    nifty.get(
        "support",
        "N/A",
    )
)

c3.metric(
    "Max Call OI",
    nifty.get(
        "resistance",
        "N/A",
    )
)

c4.metric(
    "FII Net",
    (
        f"₹{fii['fii']:.0f} Cr"
        if fii["available"]
        else "N/A"
    )
)

c5.metric(
    "India Event Risk",
    risk,
)


# ================================================================
# 20. TABS
# ================================================================

tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "🎯 Buy/Sell Hypothesis",
    "📰 Current News",
    "📊 Stock OI / PCR",
    "🌍 Macro",
    "🇮🇳 India Events",
])


# ================================================================
# 21. HYPOTHESIS TAB
# ================================================================

with tab1:

    st.subheader(
        "Current Multi-Factor Buy / Sell Hypothesis"
    )

    st.caption(
        "Confidence एक heuristic confidence score है, "
        "statistical probability नहीं। WAIT का अर्थ है कि "
        "current evidence पर्याप्त directional edge नहीं देता।"
    )

    hypotheses = snapshot["hypotheses"]

    buys = [
        x for x in hypotheses
        if x["Side"] == "BUY"
    ]

    sells = [
        x for x in hypotheses
        if x["Side"] == "SELL"
    ]

    waits = [
        x for x in hypotheses
        if x["Side"] == "WAIT"
    ]

    col_buy, col_sell = st.columns(2)

    with col_buy:

        st.markdown("### 🟢 BUY Candidates")

        if not buys:
            st.info(
                "Current data में strong BUY candidate नहीं।"
            )

        for x in buys[:10]:

            with st.container(border=True):

                st.markdown(
                    f"#### {x['Stock']} — "
                    f"{x['Confidence']}%"
                )

                st.write(
                    f"Score: {x['Score']} | "
                    f"Stock PCR: "
                    f"{x['Stock PCR'] or 'N/A'}"
                )

                if x["Bull"]:
                    st.success(
                        " • ".join(x["Bull"])
                    )

                if x["Bear"]:
                    st.warning(
                        "Counter evidence: "
                        + " • ".join(
                            x["Bear"][:2]
                        )
                    )

                if x["News"]:
                    st.caption(
                        "News: "
                        + " | ".join(x["News"])
                    )

                st.link_button(
                    "📈 Chart",
                    x["Chart"],
                )

    with col_sell:

        st.markdown("### 🔴 SELL Candidates")

        if not sells:
            st.info(
                "Current data में strong SELL candidate नहीं।"
            )

        for x in sells[:10]:

            with st.container(border=True):

                st.markdown(
                    f"#### {x['Stock']} — "
                    f"{x['Confidence']}%"
                )

                st.write(
                    f"Score: {x['Score']} | "
                    f"Stock PCR: "
                    f"{x['Stock PCR'] or 'N/A'}"
                )

                if x["Bear"]:
                    st.error(
                        " • ".join(x["Bear"])
                    )

                if x["Bull"]:
                    st.warning(
                        "Counter evidence: "
                        + " • ".join(
                            x["Bull"][:2]
                        )
                    )

                if x["News"]:
                    st.caption(
                        "News: "
                        + " | ".join(x["News"])
                    )

                st.link_button(
                    "📈 Chart",
                    x["Chart"],
                )

    st.markdown("---")
    st.markdown("### 🟡 WAIT / Mixed")

    if waits:

        wait_df = pd.DataFrame([
            {
                "Stock": x["Stock"],
                "Score": x["Score"],
                "Confidence": x["Confidence"],
                "Stock PCR": x["Stock PCR"],
            }
            for x in waits
        ])

        st.dataframe(
            wait_df,
            use_container_width=True,
            hide_index=True,
        )


# ================================================================
# 22. NEWS
# ================================================================

with tab2:

    st.subheader(
        "Current Stock News / Events"
    )

    for stock in selected:

        news = snapshot["news"].get(
            stock,
            [],
        )

        with st.expander(
            f"{stock} ({len(news)} headlines)"
        ):

            if not news:
                st.caption(
                    "Recent headline उपलब्ध नहीं।"
                )

            for n in news:

                sentiment = n["sentiment"]

                tag = (
                    "🟢"
                    if sentiment > 0
                    else "🔴"
                    if sentiment < 0
                    else "⚪"
                )

                st.markdown(
                    f"{tag} [{n['title']}]"
                    f"({n['url']})"
                )


# ================================================================
# 23. STOCK OPTION OI
# ================================================================

with tab3:

    st.subheader(
        "Stock F&O Open Interest / PCR"
    )

    rows = []

    for stock in selected:

        o = snapshot["stock_oi"].get(
            stock,
            {},
        )

        if not o:
            continue

        rows.append({
            "Stock": stock,
            "PCR": (
                round(o["pcr"], 2)
                if o.get("pcr") is not None
                else None
            ),
            "Put OI": o.get("put_oi"),
            "Call OI": o.get("call_oi"),
            "Put ΔOI": o.get("put_change_oi"),
            "Call ΔOI": o.get("call_change_oi"),
            "Support": o.get("support"),
            "Resistance": o.get("resistance"),
        })

    if rows:

        st.dataframe(
            pd.DataFrame(rows),
            use_container_width=True,
            hide_index=True,
        )

    else:

        st.info(
            "Selected stocks के लिए NSE option-chain "
            "data उपलब्ध नहीं है। ध्यान दें: केवल F&O "
            "eligible stocks का stock PCR मिल सकता है।"
        )


# ================================================================
# 24. GLOBAL MACRO
# ================================================================

with tab4:

    st.subheader(
        "Global Macro Impact"
    )

    macro_rows = []

    for name, x in macro.items():

        macro_rows.append({
            "Asset": name,
            "Price": round(
                x["price"],
                3,
            ),
            "% Change": round(
                x["pct"],
                2,
            ),
        })

    if macro_rows:

        mdf = pd.DataFrame(
            macro_rows
        )

        st.dataframe(
            mdf,
            use_container_width=True,
            hide_index=True,
        )

    st.markdown(
        """
Macro engine stock sensitivity भी लागू करता है:
IT में NASDAQ/USDINR, upstream/OMC में crude,
metals में copper तथा broad market में S&P 500,
DXY और US yields का प्रभाव लिया जाता है।
        """
    )


# ================================================================
# 25. INDIA ECONOMIC EVENTS
# ================================================================

with tab5:

    st.subheader(
        "India Economic Event Risk"
    )

    if not events:

        st.info(
            "आज India economic-calendar event "
            "feed में कोई event उपलब्ध नहीं है।"
        )

    else:

        st.dataframe(
            pd.DataFrame(events),
            use_container_width=True,
            hide_index=True,
        )

    if risk == "HIGH":
        st.warning(
            "High-impact India event मौजूद है। "
            "Hypothesis confidence automatically reduced है। "
            "Actual-vs-forecast data आने से पहले engine event "
            "को bullish/bearish assume नहीं करता।"
        )


# ================================================================
# 26. FOOTER / DATA QUALITY
# ================================================================

st.markdown("---")

st.caption(
    f"Last intelligence build: "
    f"{snapshot['timestamp']:%d-%b-%Y %H:%M:%S IST}. "
    "Yahoo Finance और public/news feeds exchange-grade tick feeds "
    "नहीं हैं; timestamps/delays source के अनुसार अलग हो सकते हैं. "
    "NSE endpoints unavailable/rate-limited होने पर संबंधित factor "
    "neutral/NA रहता है, fabricated value इस्तेमाल नहीं होती।"
)
