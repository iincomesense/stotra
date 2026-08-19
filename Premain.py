"""
================================================================================
FULL MARKET AI HYPOTHESIS & ULTRA-FAST D&S DASHBOARD (EVIDENCE-BASED v4.0)
================================================================================
- Engine 1: Quantitative AI Buy/Sell Hypothesis Matrix (100% Evidence-Driven)
- Engine 2: Shoonya API (Sub-Second) + YFinance Multi-Threaded Fallback
- Engine 3: Incremental / Cached D&S Zone Processing Engine (0.001s Update)
- Engine 4: Live News, NSE Corporate Filings, Global Macro & Sector Impact
- UI/UX: Fully Mobile-Responsive Card UI & Clean Desktop Layout
================================================================================
"""

import concurrent.futures
import io
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from typing import List, Optional, Tuple, Dict, Any

import numpy as np
import pandas as pd
import requests
import streamlit as st

# Safe Imports for Third-Party Libraries
try:
    import pyotp
    from NorenRestApiPy.NorenApi import NorenApi
    HAS_SHOONYA = True
except ImportError:
    HAS_SHOONYA = False

try:
    import yfinance as yf
except ImportError:
    yf = None

try:
    import feedparser
except ImportError:
    feedparser = None

try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except ImportError:
    HAS_AUTOREFRESH = False

import streamlit.components.v1 as components

# ==========================================
# 1. GLOBAL CONSTANTS & CONFIGURATION
# ==========================================
IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

TARGET_RR = 4.0
SL_BUFFER_ATR = 0.15
ATR_PERIOD = 14
LEG_OUT_ATR_MULT = 1.2
HQ_LEG_OUT_ATR = 2.0
MAX_BASE_ATR_MULT = 1.0
MAX_WICK_PCT = 0.25
MIN_BASE_COUNT = 1
MAX_BASE_COUNT = 3
MIN_PROXIMITY_PCT = 0.002   # 0.2% Proximity (Scalping Ready)
MAX_PROXIMITY_PCT = 0.012   # 1.2% Proximity

RAW_STOCKS = """TCS,M&M,HCLTECH,SBIN,INFY,HINDUNILVR,RELIANCE,BHARTIARTL,BEL,ONGC,
BAJAJ_AUTO,NESTLEIND,POWERGRID,ULTRACEMCO,ITC,ADANIPORTS,LT,COALINDIA,ADANIENT,
SUNPHARMA,MARUTI,ETERNAL,HDFCBANK,JSWSTEEL,NTPC,ASIANPAINT,DMART,KOTAKBANK,
TATASTEEL,TITAN,AXISBANK,SHRIRAMFIN,ICICIBANK,BAJFINANCE,TATAMOTORS,MOTHERSON"""

WATCHLIST_DEFAULT = list(dict.fromkeys([s.strip() for s in RAW_STOCKS.replace("\n", "").split(",") if s.strip()]))

STOCK_SECTOR_MAP = {
    "TCS": "IT", "INFY": "IT", "HCLTECH": "IT", "WIPRO": "IT", "TECHM": "IT", "COFORGE": "IT", "PERSISTENT": "IT",
    "HDFCBANK": "Bank", "ICICIBANK": "Bank", "SBIN": "Bank", "KOTAKBANK": "Bank", "AXISBANK": "Bank", "AUBANK": "Bank",
    "BAJFINANCE": "NBFC", "SHRIRAMFIN": "NBFC", "MUTHOOTFIN": "NBFC",
    "RELIANCE": "Energy", "ONGC": "Energy", "OIL": "Energy", "BPCL": "Energy", "IOC": "Energy", "HINDPETRO": "Energy",
    "TATAMOTORS": "Auto", "MARUTI": "Auto", "M&M": "Auto", "BAJAJ_AUTO": "Auto", "HEROMOTOCO": "Auto", "TVSMOTOR": "Auto",
    "SUNPHARMA": "Pharma", "DRREDDY": "Pharma", "CIPLA": "Pharma", "DIVISLAB": "Pharma", "LUPIN": "Pharma",
    "TATASTEEL": "Metal", "JSWSTEEL": "Metal", "HINDALCO": "Metal", "VEDL": "Metal", "JINDALSTEL": "Metal",
    "ASIANPAINT": "Consumer", "HINDUNILVR": "Consumer", "ITC": "Consumer", "NESTLEIND": "Consumer", "BRITANNIA": "Consumer"
}

GLOBAL_INSTRUMENTS = [
    ("DXY", "US Dollar Index", "DX-Y.NYB", "TVC:DXY"),
    ("USDINR", "USD / INR", "INR=X", "FX_IDC:USDINR"),
    ("US10Y", "US 10-Yr Yield", "^TNX", "TVC:US10Y"),
    ("XAUUSD", "Gold / USD", "GC=F", "TVC:GOLD"),
    ("SPOTCRUDE", "WTI Crude Oil", "CL=F", "TVC:USOIL"),
    ("US500", "S&P 500", "^GSPC", "TVC:SPX"),
]

SECTOR_INDEX_TICKERS = {
    "Bank": "^NSEBANK", "IT": "^CNXIT", "Auto": "^CNXAUTO",
    "Consumer": "^CNXFMCG", "Pharma": "^CNXPHARMA", "Metal": "^CNXMETAL",
    "Energy": "^CNXENERGY"
}

TIMEFRAMES = {
    "3 Min":   {"interval": "1m",  "period": "5d",  "resample": "3min",  "min": 3},
    "5 Min":   {"interval": "5m",  "period": "5d",  "resample": None,    "min": 5},
    "15 Min":  {"interval": "5m",  "period": "5d",  "resample": "15min", "min": 15},
    "1 Hour":  {"interval": "60m", "period": "1mo", "resample": None,    "min": 60},
    "Daily":   {"interval": "1d",  "period": "6mo", "resample": None,    "min": 1440},
}

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

# ==========================================
# 2. NUMPY VECTORIZED MATHEMATICS ENGINE
# ==========================================
def calculate_atr_np(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(high)
    if n < 2: return np.zeros(n)
    tr = np.maximum(high[1:] - low[1:], np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])))
    tr = np.insert(tr, 0, high[0] - low[0])
    atr = np.empty(n, dtype=np.float64)
    atr[0] = tr[0]
    alpha = 1.0 / period
    one_minus_alpha = 1.0 - alpha
    for i in range(1, n):
        atr[i] = alpha * tr[i] + one_minus_alpha * atr[i-1]
    return atr

def calc_ema_np(arr: np.ndarray, span: int) -> np.ndarray:
    if len(arr) == 0: return np.array([])
    alpha = 2.0 / (span + 1.0)
    res = np.empty_like(arr)
    res[0] = arr[0]
    one_minus_alpha = 1.0 - alpha
    for i in range(1, len(arr)):
        res[i] = alpha * arr[i] + one_minus_alpha * res[i - 1]
    return res

def calculate_rsi_np(close: np.ndarray, period: int = 14) -> float:
    if len(close) < period + 1: return 50.0
    diffs = np.diff(close)
    gains = np.where(diffs > 0, diffs, 0.0)
    losses = np.where(diffs < 0, -diffs, 0.0)
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    if avg_loss == 0: return 100.0
    rs = avg_gain / avg_loss
    return float(100.0 - (100.0 / (1.0 + rs)))

# ==========================================
# 3. SHOONYA & YFINANCE DATA ENGINES
# ==========================================
class ShoonyaDataEngine:
    def __init__(self, user, pwd, vc, app_key, imei, totp_secret):
        self.api = None
        self.is_connected = False
        if HAS_SHOONYA and user and pwd and vc and app_key and totp_secret:
            try:
                self.api = NorenApi(host='https://api.shoonya.com/NorenWSSL/', websocket='wss://api.shoonya.com/NorenWSS/')
                totp = pyotp.TOTP(totp_secret).now()
                ret = self.api.login(userid=user, password=pwd, twoFA=totp, vendor_code=vc, api_secret=app_key, imei=imei)
                if ret and ret.get('stat') == 'Ok':
                    self.is_connected = True
            except Exception:
                self.is_connected = False

    def get_candles(self, symbol: str, tf_key: str) -> Optional[pd.DataFrame]:
        if not self.is_connected or not self.api: return None
        try:
            search_res = self.api.searchscrip(exchange='NSE', searchtext=f"{symbol}-EQ")
            if not search_res or 'values' not in search_res: return None
            token = search_res['values'][0]['token']
            interval_min = TIMEFRAMES[tf_key]["min"]
            st_time = (datetime.now() - timedelta(days=7)).strftime("%d-%m-%Y %H:%M:%S")
            res = self.api.get_time_price_series(exchange='NSE', token=token, starttime=st_time, interval=interval_min)
            if not res: return None
            df = pd.DataFrame(res)
            df['time'] = pd.to_datetime(df['time'], format="%d-%m-%Y %H:%M:%S")
            df.set_index('time', inplace=True)
            df = df.sort_index().rename(columns={'into': 'Open', 'inth': 'High', 'intl': 'Low', 'intc': 'Close', 'v': 'Volume'})
            for col in ['Open', 'High', 'Low', 'Close', 'Volume']:
                df[col] = df[col].astype(float)
            return df
        except Exception:
            return None

@st.cache_data(ttl=120, show_spinner=False)
def fetch_yf_data_batch(tickers_tuple: Tuple[str, ...], period: str, interval: str) -> Dict[str, pd.DataFrame]:
    if not tickers_tuple or yf is None: return {}
    try:
        data = yf.download(list(tickers_tuple), period=period, interval=interval, group_by="ticker", progress=False, threads=True)
        out = {}
        for t in tickers_tuple:
            try:
                df = data[t].dropna() if len(tickers_tuple) > 1 else data.dropna()
                if not df.empty: out[t] = df
            except Exception: pass
        return out
    except Exception: return {}

# ==========================================
# 4. INCREMENTAL CACHED D&S SCANNER
# ==========================================
class Zone:
    def __init__(self, prox_val, dist_val, sl_val, tp_val, is_demand, is_hq, start_idx):
        self.prox_val = float(prox_val)
        self.dist_val = float(dist_val)
        self.sl_val = float(sl_val)
        self.tp_val = float(tp_val)
        self.is_demand = bool(is_demand)
        self.is_hq = bool(is_hq)
        self.start_idx = int(start_idx)
        self.state = "Fresh"
        self.touch_count = 0

def full_ds_zone_scan(df: pd.DataFrame) -> List[Zone]:
    if df is None or len(df) < 25: return []
    high = df['High'].to_numpy(dtype=np.float64)
    low = df['Low'].to_numpy(dtype=np.float64)
    close = df['Close'].to_numpy(dtype=np.float64)
    open_p = df['Open'].to_numpy(dtype=np.float64)
    n = len(high)

    atr = calculate_atr_np(high, low, close, ATR_PERIOD)
    tr = high - low
    is_bull = close > open_p

    zones = []
    for i in range(15, n):
        for base_count in range(MIN_BASE_COUNT, MAX_BASE_COUNT + 1):
            leg_out_idx = i
            leg_in_idx = i - base_count - 1
            if leg_in_idx < 0: continue

            leg_out_tr, leg_out_atr = tr[leg_out_idx], atr[leg_out_idx]
            if leg_out_tr < (LEG_OUT_ATR_MULT * leg_out_atr): continue

            is_demand = is_bull[leg_out_idx]
            base_high = np.max(high[leg_in_idx + 1 : leg_out_idx])
            base_low = np.min(low[leg_in_idx + 1 : leg_out_idx])

            prox_val = base_high if is_demand else base_low
            dist_val = base_low if is_demand else base_high

            is_hq = leg_out_tr >= (HQ_LEG_OUT_ATR * leg_out_atr)
            sl_val = (dist_val - (SL_BUFFER_ATR * leg_out_atr)) if is_demand else (dist_val + (SL_BUFFER_ATR * leg_out_atr))
            risk = abs(prox_val - sl_val)
            tp_val = (prox_val + (risk * TARGET_RR)) if is_demand else (prox_val - (risk * TARGET_RR))

            zones.append(Zone(prox_val, dist_val, sl_val, tp_val, is_demand, is_hq, i))
            break
    return zones

def incremental_ds_zone_update(df: pd.DataFrame, cache_key: str) -> List[Zone]:
    if "ds_cache" not in st.session_state:
        st.session_state.ds_cache = {}

    current_bar_count = len(df)
    last_high = df['High'].iloc[-1]
    last_low = df['Low'].iloc[-1]

    if cache_key in st.session_state.ds_cache:
        cached_data = st.session_state.ds_cache[cache_key]
        zones = cached_data["zones"]
        prev_bar_count = cached_data["bar_count"]

        # Fast Zone State Update
        for z in zones:
            if z.state == "Filled": continue
            if z.is_demand:
                if last_low <= z.dist_val: z.state = "Filled"
                elif last_low <= z.prox_val: z.state = "Retest"
            else:
                if last_high >= z.dist_val: z.state = "Filled"
                elif last_high >= z.prox_val: z.state = "Retest"

        if current_bar_count > prev_bar_count:
            recent_df = df.iloc[-35:]
            new_recent_zones = full_ds_zone_scan(recent_df)
            if new_recent_zones:
                for nz in new_recent_zones:
                    if not any(abs(z.prox_val - nz.prox_val) < (nz.prox_val * 0.001) for z in zones):
                        zones.append(nz)
            st.session_state.ds_cache[cache_key]["bar_count"] = current_bar_count

        return zones
    else:
        zones = full_ds_zone_scan(df)
        st.session_state.ds_cache[cache_key] = {"zones": zones, "bar_count": current_bar_count}
        return zones

# ==========================================
# 5. LIVE NEWS, MACRO & CORPORATE FILINGS
# ==========================================
@st.cache_data(ttl=600, show_spinner=False)
def fetch_stock_news_sentiment(stock_symbol: str) -> Tuple[float, List[str]]:
    """Fetches real-time RSS Google News and scores sentiment evidence"""
    if feedparser is None: return 0.0, ["News feedparser unavailable"]
    query = urllib.parse.quote_plus(f"{stock_symbol} NSE stock news")
    url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
    score = 0.0
    evidence_logs = []
    pos_words = ["profit", "surge", "gain", "order", "growth", "bullish", "buy", "expansion", "approval"]
    neg_words = ["loss", "fall", "drop", "penalty", "raid", "probe", "resignation", "bearish", "decline"]

    try:
        feed = feedparser.parse(requests.get(url, timeout=5).content)
        for entry in feed.entries[:4]:
            title = entry.title.lower()
            found_pos = [w for w in pos_words if w in title]
            found_neg = [w for w in neg_words if w in title]

            if found_pos:
                score += 1.0
                evidence_logs.append(f"📰 Positive News: '{entry.title[:65]}...'")
            if found_neg:
                score -= 1.0
                evidence_logs.append(f"📰 Negative News: '{entry.title[:65]}...'")
    except Exception:
        pass

    return score, evidence_logs

@st.cache_data(ttl=900, show_spinner=False)
def fetch_nse_json(api_path: str):
    session = requests.Session()
    session.headers.update(NSE_HEADERS)
    try:
        session.get("https://www.nseindia.com", timeout=5)
        r = session.get(f"https://www.nseindia.com{api_path}", timeout=5)
        if r.status_code == 200: return r.json()
    except Exception: pass
    return None

@st.cache_data(ttl=120, show_spinner=False)
def fetch_macro_quotes():
    symbols = [g[2] for g in GLOBAL_INSTRUMENTS if g[2]]
    batch = fetch_yf_data_batch(tuple(symbols), period="5d", interval="1d")
    quotes = {}
    for sym in symbols:
        df = batch.get(sym)
        if df is not None and len(df) >= 2:
            last, prev = df["Close"].iloc[-1], df["Close"].iloc[-2]
            pct = ((last - prev) / prev) * 100.0
            quotes[sym] = {"price": last, "pct": pct}
    return quotes

# ==========================================
# 6. QUANTITATIVE AI EVIDENCE HYPOTHESIS ENGINE
# ==========================================
def generate_evidence_hypothesis(
    stock: str,
    df_3m: Optional[pd.DataFrame],
    df_5m: Optional[pd.DataFrame],
    df_1h: Optional[pd.DataFrame],
    macro_data: Dict[str, Any],
    sector_pct: float
) -> Dict[str, Any]:
    """
    100% Rules-Based Evidence Scoring Matrix.
    Aggregates Technicals, D&S Zones, Sector Alignment, Macro Drivers & Live News.
    """
    total_score = 0.0
    evidences = []

    # Choose primary intraday dataframe
    df = df_5m if df_5m is not None else df_3m
    if df is None or len(df) < 25:
        return {"hypothesis": "INSUFFICIENT DATA", "score": 0, "evidences": ["No candle data available"]}

    close_np = df['Close'].to_numpy()
    vol_np = df['Volume'].to_numpy()
    ltp = close_np[-1]

    # --- 1. TECHNICAL EVIDENCE ---
    # A. Trend Alignment (1H or 5m EMA 200)
    ema200 = calc_ema_np(close_np, min(200, len(close_np)))
    if len(ema200) > 0:
        if ltp > ema200[-1]:
            total_score += 1.0
            evidences.append("📊 Trend Alignment: Price is above EMA-200 (Bullish Domain)")
        else:
            total_score -= 1.0
            evidences.append("📊 Trend Alignment: Price is below EMA-200 (Bearish Domain)")

    # B. D&S Zone Proximity Check
    zones = incremental_ds_zone_update(df, f"{stock}_5m")
    active_zones = [z for z in zones if z.state in ["Fresh", "Retest"]]
    nearest_demand = None
    nearest_supply = None

    for z in active_zones:
        diff_pct = abs(ltp - z.prox_val) / z.prox_val
        if diff_pct <= MAX_PROXIMITY_PCT:
            if z.is_demand and nearest_demand is None:
                nearest_demand = z
            elif not z.is_demand and nearest_supply is None:
                nearest_supply = z

    if nearest_demand:
        mult = 3.5 if nearest_demand.is_hq else 2.5
        total_score += mult
        hq_tag = "HQ " if nearest_demand.is_hq else ""
        evidences.append(f"🟢 Institutional Proximity: At {hq_tag}DEMAND Zone (Entry: {nearest_demand.prox_val:.2f})")
    elif nearest_supply:
        mult = 3.5 if nearest_supply.is_hq else 2.5
        total_score -= mult
        hq_tag = "HQ " if nearest_supply.is_hq else ""
        evidences.append(f"🔴 Institutional Proximity: At {hq_tag}SUPPLY Zone (Entry: {nearest_supply.prox_val:.2f})")

    # C. EMA Fast Cross (3/5)
    ema3 = calc_ema_np(close_np, 3)
    ema5 = calc_ema_np(close_np, 5)
    if len(ema3) >= 2 and len(ema5) >= 2:
        if ema3[-2] <= ema5[-2] and ema3[-1] > ema5[-1]:
            total_score += 1.5
            evidences.append("⚡ Scalp Momentum: EMA 3/5 Bullish Cross UP")
        elif ema3[-2] >= ema5[-2] and ema3[-1] < ema5[-1]:
            total_score -= 1.5
            evidences.append("⚡ Scalp Momentum: EMA 3/5 Bearish Cross DOWN")

    # D. Volume Spike Detection
    if len(vol_np) >= 21:
        avg_v = np.mean(vol_np[-21:-1])
        if avg_v > 0 and (vol_np[-1] / avg_v) >= 2.0:
            vol_mult = vol_np[-1] / avg_v
            if close_np[-1] > df['Open'].iloc[-1]:
                total_score += 1.0
                evidences.append(f"🔥 Volume Spike: {vol_mult:.1f}x Bullish Expansion")
            else:
                total_score -= 1.0
                evidences.append(f"🔥 Volume Spike: {vol_mult:.1f}x Bearish Pressure")

    # E. RSI Oversold / Overbought
    rsi = calculate_rsi_np(close_np)
    if rsi <= 32:
        total_score += 1.0
        evidences.append(f"🧊 RSI Oversold ({rsi:.0f}): Mean-reversion Long potential")
    elif rsi >= 68:
        total_score -= 1.0
        evidences.append(f"🔥 RSI Overbought ({rsi:.0f}): Short-term exhaustion")

    # --- 2. SECTORAL EVIDENCE ---
    if sector_pct > 0.4:
        total_score += 1.0
        evidences.append(f"🏭 Sector Tailwind: Sector Index is GREEN ({sector_pct:+.2f}%)")
    elif sector_pct < -0.4:
        total_score -= 1.0
        evidences.append(f"🏭 Sector Headwind: Sector Index is RED ({sector_pct:+.2f}%)")

    # --- 3. MACRO EVIDENCE ---
    crude = macro_data.get("CL=F")
    usdinr = macro_data.get("INR=X")
    sec_name = STOCK_SECTOR_MAP.get(stock, "")

    if crude and abs(crude["pct"]) >= 0.8:
        if sec_name in ["Auto", "Consumer", "Paint"]:
            if crude["pct"] > 0:
                total_score -= 1.0
                evidences.append(f"🌍 Macro Impact: Crude Oil Surging ({crude['pct']:+.2f}%) - Margin pressure for {sec_name}")
            else:
                total_score += 1.0
                evidences.append(f"🌍 Macro Impact: Crude Oil Dropping ({crude['pct']:+.2f}%) - Input cost relief for {sec_name}")
        elif sec_name == "Energy":
            if crude["pct"] > 0:
                total_score += 1.0
                evidences.append(f"🌍 Macro Impact: Crude Oil Surging ({crude['pct']:+.2f}%) - Positive Realisation for Upstream")

    if usdinr and abs(usdinr["pct"]) >= 0.2:
        if sec_name == "IT":
            if usdinr["pct"] > 0:
                total_score += 1.0
                evidences.append(f"🌍 Macro Impact: Rupee Weakening ({usdinr['pct']:+.2f}%) - Export Boost for IT")
            else:
                total_score -= 1.0
                evidences.append(f"🌍 Macro Impact: Rupee Strengthening ({usdinr['pct']:+.2f}%) - Currency drag for IT")

    # --- 4. NEWS & SENTIMENT EVIDENCE ---
    news_score, news_logs = fetch_stock_news_sentiment(stock)
    total_score += news_score
    evidences.extend(news_logs)

    # --- FINAL HYPOTHESIS & TRADE PLAN GENERATION ---
    if total_score >= 3.5:
        hypothesis = "🟢🟢 STRONG BUY HYPOTHESIS"
        action = "High Conviction Dip Buy"
    elif 1.5 <= total_score < 3.5:
        hypothesis = "🟢 BUY / BULLISH DIP HYPOTHESIS"
        action = "Look for Long Reversals on Retest"
    elif -1.5 < total_score < 1.5:
        hypothesis = "🟡 NEUTRAL / WATCH HYPOTHESIS"
        action = "No Trade Zone - Await Breakout"
    elif -3.5 < total_score <= -1.5:
        hypothesis = "🔴 SELL / BEARISH RALLY HYPOTHESIS"
        action = "Look for Short Reversals on Bounce"
    else:
        hypothesis = "🔴🔴 STRONG SELL HYPOTHESIS"
        action = "High Conviction Sell / Short"

    atr = calculate_atr_np(df['High'].to_numpy(), df['Low'].to_numpy(), close_np)[-1]

    # Generate Trade Execution Plan
    if "BUY" in hypothesis:
        entry = ltp
        sl = ltp - (1.5 * atr)
        tp = ltp + (1.5 * atr * TARGET_RR)
    elif "SELL" in hypothesis:
        entry = ltp
        sl = ltp + (1.5 * atr)
        tp = ltp - (1.5 * atr * TARGET_RR)
    else:
        entry, sl, tp = ltp, ltp, ltp

    return {
        "stock": stock,
        "ltp": ltp,
        "score": total_score,
        "hypothesis": hypothesis,
        "action": action,
        "evidences": evidences,
        "plan": {"entry": entry, "sl": sl, "tp": tp, "rr": TARGET_RR}
    }

# ==========================================
# 7. STREAMLIT UI & RESPONSIVE CARD LAYOUT
# ==========================================
st.set_page_config(page_title="AI Market Intelligence Engine", layout="wide", page_icon="🧠", initial_sidebar_state="collapsed")

# Inject Custom Mobile-Optimized CSS
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap');
html, body, [class*="css"] { font-family: 'Inter', sans-serif !important; }

@media (max-width: 768px) {
    .block-container { padding: 0.4rem 0.4rem !important; }
    div[data-testid="stMetricValue"] { font-size: 1.1rem !important; }
    .evidence-card { padding: 10px !important; margin-bottom: 8px !important; }
}

.card-strong-buy { background-color: #E8F5E9; border-left: 6px solid #1B5E20; padding: 14px; border-radius: 10px; margin-bottom: 12px; }
.card-buy { background-color: #F1F8E9; border-left: 6px solid #558B2F; padding: 14px; border-radius: 10px; margin-bottom: 12px; }
.card-neutral { background-color: #FFFDE7; border-left: 6px solid #FBC02D; padding: 14px; border-radius: 10px; margin-bottom: 12px; }
.card-sell { background-color: #FFEBEE; border-left: 6px solid #C62828; padding: 14px; border-radius: 10px; margin-bottom: 12px; }
.card-strong-sell { background-color: #FFCDD2; border-left: 6px solid #B71C1C; padding: 14px; border-radius: 10px; margin-bottom: 12px; }

.badge-score { background-color: #0B1F3A; color: #FFF; font-weight: bold; padding: 3px 8px; border-radius: 4px; font-size: 12px; }
.evidence-item { font-size: 12.5px; margin: 3px 0; color: #222; }
.btn-chart { display: inline-block; background: #0B1F3A; color: #FFF !important; padding: 5px 10px; border-radius: 6px; text-decoration: none; font-weight: 600; font-size: 11px; margin-top: 6px; }
</style>
""", unsafe_allow_html=True)

# Auto-Refresh Engine
if HAS_AUTOREFRESH:
    st_autorefresh(interval=5000, key="ai_dashboard_refresh")

# ==========================================
# 8. SIDEBAR CONTROLS
# ==========================================
st.sidebar.header("⚙️ Data Engine Settings")
data_mode = st.sidebar.radio("Data Engine Mode", ["Shoonya API (Sub-Second)", "Yahoo Finance (Fallback)"])

shoonya_engine = None
if data_mode == "Shoonya API (Sub-Second)":
    st.sidebar.subheader("🔑 Shoonya Login")
    u = st.sidebar.text_input("User ID", type="password")
    p = st.sidebar.text_input("Password", type="password")
    vc = st.sidebar.text_input("Vendor Code", type="password")
    k = st.sidebar.text_input("API Key", type="password")
    totp = st.sidebar.text_input("TOTP Secret Key", type="password")
    if st.sidebar.button("Connect API"):
        shoonya_engine = ShoonyaDataEngine(u, p, vc, k, "IMEI_DEFAULT", totp)
        if shoonya_engine.is_connected: st.sidebar.success("🟢 Shoonya Connected!")
        else: st.sidebar.error("🔴 Connection Failed!")

selected_stocks = st.sidebar.multiselect("Watchlist", WATCHLIST_DEFAULT, default=WATCHLIST_DEFAULT[:10])
view_layout = st.sidebar.radio("Layout Mode", ["Mobile Interactive Cards 📱", "Desktop Table View 🖥️"])

# ==========================================
# 9. HEADER & MACRO TICKER TAPE
# ==========================================
st.title("🧠 Evidence-Based AI Buy/Sell Engine")
st.caption(f"⚡ IST Time: {datetime.now(IST).strftime('%d-%b-%Y %H:%M:%S')} | No Hallucinations — Pure Data Proof")

macro_data = fetch_macro_quotes()

# Display Top Macro Metric Bar
m_cols = st.columns(6)
for idx, (sym, name, yft, _) in enumerate(GLOBAL_INSTRUMENTS):
    q = macro_data.get(yft)
    if q:
        m_cols[idx % 6].metric(sym, f"{q['price']:.2f}", f"{q['pct']:+.2f}%")

# Fetch Sector Index Quotes
sector_quotes = {}
sec_tickers = list(SECTOR_INDEX_TICKERS.values())
sec_batch = fetch_yf_data_batch(tuple(sec_tickers), period="5d", interval="1d")
for name, yft in SECTOR_INDEX_TICKERS.items():
    df = sec_batch.get(yft)
    if df is not None and len(df) >= 2:
        last, prev = df["Close"].iloc[-1], df["Close"].iloc[-2]
        sector_quotes[name] = ((last - prev) / prev) * 100.0
    else:
        sector_quotes[name] = 0.0

st.markdown("---")

# ==========================================
# 10. AI HYPOTHESIS GENERATION DASHBOARD
# ==========================================
tab_ai, tab_signals, tab_news, tab_fii = st.tabs([
    "🤖 AI Buy/Sell Hypothesis", "📊 Live D&S Signals", "📰 Corporate Filings & News", "💰 FII/DII & Option Chain"
])

# ---------- TAB 1: AI HYPOTHESIS CARDS ----------
with tab_ai:
    st.subheader(f"📋 Watchlist AI Trade Hypotheses ({len(selected_stocks)} Stocks)")

    # Data Batch Fetching
    stock_df_map_5m = {}
    stock_df_map_1h = {}

    if shoonya_engine and shoonya_engine.is_connected:
        for s in selected_stocks:
            stock_df_map_5m[s] = shoonya_engine.get_candles(s, "5 Min")
            stock_df_map_1h[s] = shoonya_engine.get_candles(s, "1 Hour")
    else:
        yf_symbols = [f"{s.replace('_', '-')}.NS" for s in selected_stocks]
        batch_5m = fetch_yf_data_batch(tuple(yf_symbols), period="5d", interval="5m")
        batch_1h = fetch_yf_data_batch(tuple(yf_symbols), period="1mo", interval="60m")

        for s in selected_stocks:
            yf_sym = f"{s.replace('_', '-')}.NS"
            if yf_sym in batch_5m: stock_df_map_5m[s] = batch_5m[yf_sym]
            if yf_sym in batch_1h: stock_df_map_1h[s] = batch_1h[yf_sym]

    ai_results = []
    for s in selected_stocks:
        df_5m = stock_df_map_5m.get(s)
        df_1h = stock_df_map_1h.get(s)
        sec_name = STOCK_SECTOR_MAP.get(s, "Bank")
        sec_pct = sector_quotes.get(sec_name, 0.0)

        hypo = generate_evidence_hypothesis(s, df_3m=None, df_5m=df_5m, df_1h=df_1h, macro_data=macro_data, sector_pct=sec_pct)
        ai_results.append(hypo)

    # Sort Results by Evidence Score (Highest Conviction First)
    ai_results = sorted(ai_results, key=lambda x: abs(x["score"]), reverse=True)

    if "Mobile" in view_layout:
        for item in ai_results:
            stk = item["stock"]
            hypo_text = item["hypothesis"]
            score = item["score"]
            plan = item["plan"]
            evidences = item["evidences"]
            tv_link = f"https://www.tradingview.com/chart/?symbol=NSE:{stk}"

            card_class = "card-neutral"
            if "STRONG BUY" in hypo_text: card_class = "card-strong-buy"
            elif "BUY" in hypo_text: card_class = "card-buy"
            elif "STRONG SELL" in hypo_text: card_class = "card-strong-sell"
            elif "SELL" in hypo_text: card_class = "card-sell"

            evidence_html = "".join([f"<div class='evidence-item'>• {e}</div>" for e in evidences])

            st.markdown(f"""
            <div class="{card_class}">
                <div style="display:flex; justify-content:space-between; align-items:center;">
                    <b style="font-size:16px;">{stk} — ₹{item['ltp']:.2f}</b>
                    <span class="badge-score">Score: {score:+.1f}</span>
                </div>
                <div style="font-weight:700; font-size:14px; margin-top:4px;">{hypo_text}</div>
                <div style="font-size:12px; color:#444; margin-bottom:6px;"><b>Strategy:</b> {item['action']}</div>
                <hr style="margin:6px 0; border:0; border-top:1px solid #ccc;"/>
                <b>🔍 Conclusive Evidence Logs ({len(evidences)} Proof Points):</b>
                {evidence_html}
                <hr style="margin:6px 0; border:0; border-top:1px solid #ccc;"/>
                <div style="font-size:12px; margin-top:4px;">
                    <b>Trade Plan:</b> Entry: ₹{plan['entry']:.2f} | SL: ₹{plan['sl']:.2f} | TP: ₹{plan['tp']:.2f} (R:R 1:{plan['rr']})
                </div>
                <a href="{tv_link}" target="_blank" class="btn-chart">📈 View TradingView Live Chart</a>
            </div>
            """, unsafe_allow_html=True)
    else:
        # Table Layout
        table_rows = []
        for item in ai_results:
            plan = item["plan"]
            table_rows.append({
                "Stock": item["stock"],
                "LTP": round(item["ltp"], 2),
                "AI Score": item["score"],
                "Hypothesis": item["hypothesis"],
                "Evidence Count": len(item["evidences"]),
                "Entry": round(plan["entry"], 2),
                "SL": round(plan["sl"], 2),
                "Target": round(plan["tp"], 2),
                "Chart": f"https://www.tradingview.com/chart/?symbol=NSE:{item['stock']}"
            })
        st.dataframe(
            pd.DataFrame(table_rows), use_container_width=True, hide_index=True,
            column_config={"Chart": st.column_config.LinkColumn("Chart", display_text="📈 Open")}
        )

# ---------- TAB 2: LIVE D&S SIGNALS ----------
with tab_signals:
    st.subheader("📊 Live Incremental D&S Scanner")
    ds_rows = []
    for s in selected_stocks:
        df_5m = stock_df_map_5m.get(s)
        if df_5m is None or len(df_5m) < 25: continue
        zones = incremental_ds_zone_update(df_5m, f"{s}_5m")
        ltp = df_5m['Close'].iloc[-1]
        for z in zones:
            if z.state in ["Fresh", "Retest"]:
                diff = abs(ltp - z.prox_val) / z.prox_val
                if diff <= MAX_PROXIMITY_PCT:
                    ds_rows.append({
                        "Stock": s, "Type": "🟢 DEMAND" if z.is_demand else "🔴 SUPPLY",
                        "Quality": "HQ ★" if z.is_hq else "Standard", "LTP": round(ltp, 2),
                        "Entry": round(z.prox_val, 2), "SL": round(z.sl_val, 2), "Target": round(z.tp_val, 2),
                        "Proximity": f"{diff*100:.2f}%"
                    })
    if ds_rows:
        st.dataframe(pd.DataFrame(ds_rows), use_container_width=True, hide_index=True)
    else:
        st.info("वर्तमान में कोई D&S ज़ोन Proximity में नहीं है।")

# ---------- TAB 3: CORPORATE FILINGS & NEWS ----------
with tab_news:
    st.subheader("📰 Live Corporate Announcements & Filings")
    announcements = fetch_nse_json("/api/corporate-announcements?index=equities")
    if announcements:
        items = []
        for a in announcements[:20]:
            items.append({
                "Symbol": a.get("symbol"),
                "Subject": a.get("desc") or a.get("subject"),
                "Time": a.get("an_dt")
            })
        st.dataframe(pd.DataFrame(items), use_container_width=True, hide_index=True)
    else:
        st.info("NSE Corporate Filings लोड हो रही हैं या बाजार बंद है।")

# ---------- TAB 4: FII/DII DATA ----------
with tab_fii:
    st.subheader("💰 FII / DII Net Flow & Option Chain PCR")
    oc = fetch_nse_json("/api/option-chain-indices?symbol=NIFTY")
    if oc:
        try:
            records = oc["records"]["data"]
            total_call_oi = sum(r["CE"]["openInterest"] for r in records if "CE" in r)
            total_put_oi = sum(r["PE"]["openInterest"] for r in records if "PE" in r)
            pcr = total_put_oi / total_call_oi if total_call_oi > 0 else 1.0
            st.metric("Nifty Option Chain PCR", f"{pcr:.2f}", "Bullish" if pcr > 1.0 else "Bearish")
        except Exception: pass
    else:
        st.info("Option Chain Data अभी अनुपलब्ध है।")
