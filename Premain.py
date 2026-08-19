"""
ULTIMATE SCALPING + INTRADAY + SWING TRADING DASHBOARD
========================================================
✅ Real Market Data (100% Live)
✅ Real News + Corporate Events (API-driven)
✅ Institutional D&S Zones + Technical Signals
✅ AI Buy/Sell Hypothesis (Live Market + News)
✅ Push Notifications + Desktop Alerts
✅ One-Stop Dashboard (सब कुछ एक जगह)
✅ High-Performance Caching + Parallel Processing
✅ Scalping (1-5 min) + Intraday (15 min-4 hours) + Swing (Daily)
"""

import concurrent.futures
import io
import urllib.parse
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from typing import List, Optional, Tuple, Dict, Any
import logging
import hashlib
import time
from functools import lru_cache, wraps

import numpy as np
import pandas as pd
import requests
import streamlit as st
import yfinance as yf

try:
    import feedparser
    HAS_FEEDPARSER = True
except ImportError:
    HAS_FEEDPARSER = False

try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except ImportError:
    HAS_AUTOREFRESH = False

try:
    from plyer import notification
    HAS_PLYER = True
except ImportError:
    HAS_PLYER = False

import streamlit.components.v1 as components

# ==========================================
# 0. LOGGING SETUP
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def handle_error(func):
    """Error handling decorator"""
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Error in {func.__name__}: {str(e)}")
            return None
    return wrapper

def retry_with_backoff(max_retries=3, backoff_factor=1.2):
    """Retry decorator with exponential backoff"""
    def decorator(func):
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_retries - 1:
                        logger.error(f"{func.__name__} failed: {e}")
                        return None
                    time.sleep(backoff_factor ** attempt)
            return None
        return wrapper
    return decorator

# ==========================================
# 1. INSTITUTIONAL D&S CONFIGURATION
# ==========================================
TARGET_RR = 5.0
SL_BUFFER_ATR = 0.1
ATR_PERIOD = 14
LEG_OUT_ATR_MULT = 1.2
HQ_LEG_OUT_ATR = 2.0
LEG_IN_STRONG_CLOSE_PCT = 0.70
BASE_MAX_BODY_RATIO = 0.35
BASE_MIN_OVERLAP_PCT = 0.50
BASE_VOL_MAX_RATIO = 1.0
LEG_OUT_VOL_LOOKBACK = 20
LEG_OUT_VOL_MULT = 1.5
MIN_PROXIMITY_PCT = 0.005
MAX_PROXIMITY_PCT = 0.010

# ==========================================
# 2. VECTORIZED NUMPY CALCULATIONS
# ==========================================
@handle_error
def calculate_atr_np(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    """Fast ATR calculation"""
    n = len(high)
    if n < 2:
        return np.zeros(n)
    
    tr = np.maximum(high[1:] - low[1:], 
                    np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])))
    tr = np.insert(tr, 0, high[0] - low[0])

    atr = np.empty(n, dtype=np.float64)
    atr[0] = tr[0]
    alpha = 1.0 / period
    for i in range(1, n):
        atr[i] = alpha * tr[i] + (1 - alpha) * atr[i-1]
    return atr

def calculate_pivots_np(highs: np.ndarray, lows: np.ndarray, left: int = 5, right: int = 5):
    """Calculate pivot highs/lows"""
    n = len(highs)
    pivot_h = np.full(n, np.nan)
    pivot_l = np.full(n, np.nan)

    for i in range(left, n - right):
        window_h = highs[i - left : i + right + 1]
        if highs[i] == np.max(window_h) and np.sum(window_h == highs[i]) == 1:
            pivot_h[i] = highs[i]
        
        window_l = lows[i - left : i + right + 1]
        if lows[i] == np.min(window_l) and np.sum(window_l == lows[i]) == 1:
            pivot_l[i] = lows[i]

    return pivot_h, pivot_l

# ==========================================
# 3. ZONE CLASS & D&S SCANNER
# ==========================================
class DemandSupplyZone:
    """Institutional D&S Zone"""
    def __init__(self, prox, dist, sl, tp, is_demand, is_hq, density, idx, tf):
        self.proximity_val = prox
        self.distribution_val = dist
        self.stop_loss = sl
        self.take_profit = tp
        self.is_demand = is_demand
        self.is_hq = is_hq
        self.density_score = density
        self.start_idx = idx
        self.timeframe = tf
        self.state = "Fresh"  # Fresh, Retest, Filled
        self.touch_count = 0
        self.activation_time = datetime.now(timezone.utc)

    def to_dict(self):
        return {
            "Entry": round(self.proximity_val, 2),
            "SL": round(self.stop_loss, 2),
            "TP": round(self.take_profit, 2),
            "Side": "BUY" if self.is_demand else "SELL",
            "Type": "🚀 HQ" if self.is_hq else "⭐ Zone",
            "State": self.state,
            "TF": self.timeframe,
        }

@handle_error
def scan_ds_zones(df: pd.DataFrame, timeframe: str = "1H") -> List[DemandSupplyZone]:
    """Scan institutional D&S zones"""
    if df is None or len(df) < 30:
        return []

    try:
        high = df['High'].to_numpy(dtype=np.float64)
        low = df['Low'].to_numpy(dtype=np.float64)
        close = df['Close'].to_numpy(dtype=np.float64)
        open_p = df['Open'].to_numpy(dtype=np.float64)
        volume = df['Volume'].to_numpy(dtype=np.float64)
        n = len(high)

        atr = calculate_atr_np(high, low, close, ATR_PERIOD)
        pivot_h, pivot_l = calculate_pivots_np(high, low, 5, 5)

        tr = high - low
        is_bull = close > open_p

        all_zones = []
        last_pivot_high = np.nan
        last_pivot_low = np.nan

        for i in range(15, n):
            if not np.isnan(pivot_h[i]):
                last_pivot_high = pivot_h[i]
            if not np.isnan(pivot_l[i]):
                last_pivot_low = pivot_l[i]

            for base_count in range(1, 4):
                leg_out_idx = i
                leg_in_idx = i - base_count - 1

                if leg_in_idx < 0:
                    continue

                leg_out_tr = tr[leg_out_idx]
                leg_in_tr = tr[leg_in_idx]
                leg_out_atr = atr[leg_out_idx]

                # Base candle checks
                base_tr = tr[leg_in_idx + 1 : leg_out_idx]
                if len(base_tr) == 0:
                    continue

                max_base_tr = np.max(base_tr) if len(base_tr) > 0 else 0
                max_base_h = np.max(high[leg_in_idx + 1 : leg_out_idx]) if len(high[leg_in_idx + 1 : leg_out_idx]) > 0 else 0
                min_base_l = np.min(low[leg_in_idx + 1 : leg_out_idx]) if len(low[leg_in_idx + 1 : leg_out_idx]) > 0 else high[leg_in_idx]

                # Validation checks
                valid_tr_hierarchy = (leg_out_tr > leg_in_tr) and (leg_in_tr > max_base_tr)
                valid_leg_out = leg_out_tr >= (LEG_OUT_ATR_MULT * leg_out_atr)
                valid_volume = volume[leg_out_idx] > volume[leg_in_idx]

                if not (valid_tr_hierarchy and valid_leg_out and valid_volume):
                    continue

                # Zone creation
                is_demand = is_bull[leg_out_idx]
                prox_val = max_base_h if is_demand else min_base_l
                dist_val = min_base_l if is_demand else max_base_h

                # HQ score
                density = 25
                if leg_out_tr >= HQ_LEG_OUT_ATR * leg_out_atr:
                    density += 25
                if base_count <= 2 and max_base_tr <= 0.7 * atr[i-1]:
                    density += 25

                is_hq = density >= 75

                # R:R calculation
                curr_atr = leg_out_atr
                sl_val = (dist_val - (SL_BUFFER_ATR * curr_atr)) if is_demand else (dist_val + (SL_BUFFER_ATR * curr_atr))
                risk = abs(prox_val - sl_val)
                tp_val = (prox_val + (risk * TARGET_RR)) if is_demand else (prox_val - (risk * TARGET_RR))

                zone = DemandSupplyZone(prox_val, dist_val, sl_val, tp_val, is_demand, is_hq, density, i, timeframe)
                
                # Duplicate check
                is_dup = any(
                    z.is_demand == is_demand and abs(z.proximity_val - prox_val) < (curr_atr * 0.25)
                    for z in all_zones[-5:]
                )
                if not is_dup:
                    all_zones.append(zone)
                break

        # Update zone states
        curr_price = close[-1]
        for z in all_zones:
            if z.state == "Filled":
                continue
            if z.is_demand:
                touched = curr_price <= z.proximity_val
                filled = curr_price <= z.distribution_val
            else:
                touched = curr_price >= z.proximity_val
                filled = curr_price >= z.distribution_val

            if filled:
                z.state = "Filled"
            elif touched:
                z.touch_count += 1
                z.state = "Retest"

        return all_zones
    except Exception as e:
        logger.error(f"D&S scan error: {e}")
        return []

# ==========================================
# 4. GLOBAL SETUP
# ==========================================
IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

COLOR_POS_BG, COLOR_POS_TEXT = "#d4f8d4", "#0a7d2f"
COLOR_NEG_BG, COLOR_NEG_TEXT = "#f8f8d4", "#c0392b"
COLOR_SPIKE_BG = "#ffe1a8"

STOCKS_LIST = """TCS,HCLTECH,INFY,WIPRO,TECHM,COFORGE,PERSISTENT,M&M,MARUTI,TATAMOTORS,
HEROMOTOCO,BAJAJ_AUTO,EICHERMOT,SBIN,HDFCBANK,ICICIBANK,KOTAKBANK,AXISBANK,AUBANK,
INDUSINDBK,IDFCFIRSTB,CANBK,FEDERALBNK,RELIANCE,BHARTIARTL,ONGC,OIL,BPCL,IOC,
HINDPETRO,POWERGRID,NTPC,JSWENERGY,TATAPOWER,ADANIPORTS,ADANIENT,LT,JSWSTEEL,
TATASTEEL,HINDALCO,VEDL,NATIONALUM,JINDALSTEL,ASIANPAINT,BRITANNIA,ITC,NESTLEIND,
HINDUNILVR,DABUR,MARICO,GODREJCP,SUNPHARMA,CIPLA,DRREDDY,LUPIN,AUROPHARMA,LAURUSLABS,
DIVISLAB,TORNTPHARM,APOLLOHOSP,MAXHEALTH,APLAPOLLO,TITAN,DMART,NAUKRI,PERSISTENT,
HDFCAMC,SBILIFE,HDFCLIFE,ICICIGI,BAJFINANCE,SHRIRAMFIN,MUTHOOTFIN,CHOLAFIN,POLYCAB,
HAVELLS,SIEMENS,CUMMINSIND,BHEL,BEL,COALINDIA,NMDC,RECLTD,PFC,GAIL,MCX"""

WATCHLIST = list(dict.fromkeys([s.strip() for s in STOCKS_LIST.replace("\n", "").split(",") if s.strip()]))

YF_FIX = {"BAJAJ_AUTO": "BAJAJ-AUTO"}
TV_FIX = {"BAJAJ_AUTO": "BAJAJ-AUTO"}

GLOBAL_INSTRUMENTS = [
    ("DXY", "US Dollar Index", "DX-Y.NYB", "TVC:DXY"),
    ("USDINR", "USD / INR", "INR=X", "FX_IDC:USDINR"),
    ("US10Y", "US 10-Yr Treasury", "^TNX", "TVC:US10Y"),
    ("XAUUSD", "Gold", "GC=F", "TVC:GOLD"),
    ("XAGUSD", "Silver", "SI=F", "TVC:SILVER"),
    ("SPOTCRUDE", "WTI Crude", "CL=F", "TVC:USOIL"),
    ("COPPER", "Copper", "HG=F", "COMEX:HG1!"),
    ("US30", "Dow Jones", "^DJI", "TVC:DJI"),
    ("US500", "S&P 500", "^GSPC", "TVC:SPX"),
]

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

TIMEFRAMES_CONFIG = {
    "1m": {"yf": "1m", "period": "7d", "display": "1 Min", "type": "scalping"},
    "3m": {"yf": "1m", "period": "7d", "display": "3 Min", "type": "scalping"},
    "5m": {"yf": "5m", "period": "5d", "display": "5 Min", "type": "scalping"},
    "15m": {"yf": "5m", "period": "5d", "display": "15 Min", "type": "intraday"},
    "30m": {"yf": "30m", "period": "10d", "display": "30 Min", "type": "intraday"},
    "1h": {"yf": "60m", "period": "1mo", "display": "1 Hour", "type": "intraday"},
    "4h": {"yf": "60m", "period": "3mo", "display": "4 Hours", "type": "swing"},
    "1d": {"yf": "1d", "period": "6mo", "display": "Daily", "type": "swing"},
}

# ==========================================
# 5. ADVANCED SMART CACHE
# ==========================================
class SmartCache:
    def __init__(self, ttl_seconds=300):
        self.cache = {}
        self.ttl = ttl_seconds
        self.timestamps = {}
    
    def get_key(self, func_name, args, kwargs):
        key_str = f"{func_name}_{str(args)}_{str(kwargs)}"
        return hashlib.md5(key_str.encode()).hexdigest()
    
    def get(self, func_name, args, kwargs):
        key = self.get_key(func_name, args, kwargs)
        if key in self.cache:
            elapsed = time.time() - self.timestamps.get(key, 0)
            if elapsed < self.ttl:
                return self.cache[key]
        return None
    
    def set(self, func_name, args, kwargs, value):
        key = self.get_key(func_name, args, kwargs)
        self.cache[key] = value
        self.timestamps[key] = time.time()
    
    def clear(self):
        self.cache.clear()
        self.timestamps.clear()

smart_cache = SmartCache(ttl_seconds=240)

def cached_call(func):
    def wrapper(*args, **kwargs):
        result = smart_cache.get(func.__name__, args, kwargs)
        if result is not None:
            return result
        try:
            result = func(*args, **kwargs)
            smart_cache.set(func.__name__, args, kwargs, result)
            return result
        except Exception as e:
            logger.error(f"{func.__name__} error: {e}")
            return None
    return wrapper

# ==========================================
# 6. DATA FETCHING
# ==========================================
@cached_call
@retry_with_backoff(max_retries=3)
def get_stock_data(ticker: str, period: str, interval: str) -> Optional[pd.DataFrame]:
    """Fetch stock data from YFinance"""
    try:
        yf_ticker = f"{YF_FIX.get(ticker, ticker)}.NS"
        df = yf.download(yf_ticker, period=period, interval=interval, progress=False)
        
        if df.empty:
            return None
        
        # Resample if needed
        if interval == "1m" and period in ["5d", "7d"]:
            df_3m = df.resample("3min").agg(
                {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()
            df_15m = df.resample("15min").agg(
                {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()
            
            return df, df_3m, df_15m  # 1m, 3m, 15m
        elif interval == "5m":
            df_15m = df.resample("15min").agg(
                {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()
            return df, df_15m  # 5m, 15m
        elif interval == "30m":
            return df,  # 30m
        else:
            return df,  # Single timeframe

    except Exception as e:
        logger.error(f"Data fetch error for {ticker}: {e}")
        return None

@cached_call
@retry_with_backoff(max_retries=2)
def get_quotes(tickers: List[str]) -> Dict[str, Dict]:
    """Get live quotes"""
    quotes = {}
    for ticker in tickers:
        try:
            yf_ticker = f"{YF_FIX.get(ticker, ticker)}.NS"
            data = yf.download(yf_ticker, period="5d", interval="1d", progress=False)
            
            if not data.empty and len(data) >= 2:
                last = data['Close'].iloc[-1]
                prev = data['Close'].iloc[-2]
                chg = last - prev
                pct = (chg / prev) * 100
                quotes[ticker] = {"price": float(last), "chg": float(chg), "pct": float(pct)}
        except Exception as e:
            logger.warning(f"Quote error for {ticker}: {e}")
            continue
    
    return quotes

def parallel_fetch_data(tickers: List[str], timeframe_key: str):
    """Fetch data for multiple tickers in parallel"""
    tf_cfg = TIMEFRAMES_CONFIG.get(timeframe_key, {})
    yf_interval = tf_cfg.get("yf", "1h")
    period = tf_cfg.get("period", "5d")
    
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(get_stock_data, t, period, yf_interval): t for t in tickers}
        for future in concurrent.futures.as_completed(futures):
            ticker = futures[future]
            try:
                data = future.result()
                if data:
                    results[ticker] = data
            except Exception as e:
                logger.warning(f"Parallel fetch error for {ticker}: {e}")
    
    return results

# ==========================================
# 7. TECHNICAL INDICATORS
# ==========================================
def calc_ema(close: np.ndarray, span: int) -> np.ndarray:
    """Calculate EMA"""
    alpha = 2.0 / (span + 1.0)
    ema = np.zeros_like(close)
    ema[0] = close[0]
    for i in range(1, len(close)):
        ema[i] = alpha * close[i] + (1 - alpha) * ema[i-1]
    return ema

@handle_error
def get_technical_signals(df: pd.DataFrame) -> Dict[str, Any]:
    """Get technical signals"""
    if df is None or len(df) < 20:
        return {}
    
    try:
        close = df['Close'].to_numpy(dtype=np.float64)
        vol = df['Volume'].to_numpy(dtype=np.float64)
        high = df['High'].to_numpy(dtype=np.float64)
        low = df['Low'].to_numpy(dtype=np.float64)
        
        signals = {}
        
        # EMA Crossover (20/50)
        ema20 = calc_ema(close, 20)
        ema50 = calc_ema(close, 50)
        if ema20[-2] <= ema50[-2] and ema20[-1] > ema50[-1]:
            signals["ema_20_50"] = "🟢 EMA20/50 UP"
        elif ema20[-2] >= ema50[-2] and ema20[-1] < ema50[-1]:
            signals["ema_20_50"] = "🔴 EMA20/50 DOWN"
        
        # Volume Spike
        avg_vol = np.mean(vol[-20:-1])
        if avg_vol > 0 and vol[-1] / avg_vol >= 2.0:
            signals["vol_spike"] = f"⚡ Vol {vol[-1]/avg_vol:.1f}x"
        
        # RSI
        diffs = np.diff(close)
        gains = np.where(diffs > 0, diffs, 0)
        losses = np.where(diffs < 0, -diffs, 0)
        avg_gain = np.mean(gains[-14:]) if len(gains) >= 14 else np.mean(gains)
        avg_loss = np.mean(losses[-14:]) if len(losses) >= 14 else np.mean(losses)
        rsi = 100 - (100 / (1 + (avg_gain / avg_loss if avg_loss > 0 else 1)))
        
        if rsi >= 70:
            signals["rsi"] = f"🔥 RSI OB ({rsi:.0f})"
        elif rsi <= 30:
            signals["rsi"] = f"🧊 RSI OS ({rsi:.0f})"
        
        signals["current_price"] = close[-1]
        signals["rsi_value"] = rsi
        
        return signals
    except Exception as e:
        logger.error(f"Technical signal error: {e}")
        return {}

# ==========================================
# 8. REAL NEWS FETCHER
# ==========================================
@cached_call
@retry_with_backoff(max_retries=2)
def fetch_stock_news(stock: str, max_age_hours=24) -> List[Dict]:
    """Fetch real news for stock"""
    if not HAS_FEEDPARSER:
        return []
    
    try:
        query = urllib.parse.quote_plus(f"{stock} NSE India stock when:1d")
        url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
        feed = feedparser.parse(requests.get(url, timeout=10).content)
        
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        news_list = []
        
        for entry in feed.entries[:5]:
            try:
                pub_time = entry.get("published_parsed")
                if pub_time:
                    pub_dt = datetime(*pub_time[:6], tzinfo=timezone.utc)
                    if pub_dt >= cutoff:
                        news_list.append({
                            "title": entry.get("title", ""),
                            "link": entry.get("link", ""),
                            "published": pub_dt.strftime("%H:%M %d-%b"),
                            "source": entry.get("source", {}).get("title", "News"),
                        })
            except Exception:
                continue
        
        return news_list
    except Exception as e:
        logger.warning(f"News fetch error for {stock}: {e}")
        return []

@cached_call
@retry_with_backoff(max_retries=2)
def fetch_nse_announcements() -> List[Dict]:
    """Fetch NSE corporate announcements"""
    try:
        url = "https://www.nseindia.com/api/corporate-announcements?index=equities"
        r = requests.get(url, headers=NSE_HEADERS, timeout=10)
        if r.status_code == 200:
            data = r.json()
            announcements = []
            for item in data[:20]:
                announcements.append({
                    "symbol": item.get("symbol", ""),
                    "subject": item.get("desc", "")[:100],
                    "time": item.get("an_dt", ""),
                })
            return announcements
    except Exception as e:
        logger.warning(f"Announcements fetch error: {e}")
    
    return []

@cached_call
@retry_with_backoff(max_retries=2)
def fetch_market_events() -> List[Dict]:
    """Fetch major market events"""
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        r = requests.get(url, headers=NSE_HEADERS, timeout=10)
        if r.status_code == 200:
            events = r.json()
            today = datetime.now(IST).date()
            today_events = []
            
            for e in events:
                if str(e.get("impact", "")).lower() not in ("high", "medium"):
                    continue
                try:
                    ev_date = datetime.fromisoformat(e.get("date", "").replace("Z", "+00:00")).astimezone(IST).date()
                    if ev_date == today:
                        today_events.append({
                            "country": e.get("country", ""),
                            "event": e.get("name", ""),
                            "impact": e.get("impact", ""),
                            "time": e.get("time", ""),
                        })
                except Exception:
                    continue
            
            return today_events
    except Exception as e:
        logger.warning(f"Market events fetch error: {e}")
    
    return []

@cached_call
@retry_with_backoff(max_retries=2)
def fetch_nifty_pcr() -> Dict:
    """Fetch Nifty PCR and OI data"""
    try:
        url = "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"
        r = requests.get(url, headers=NSE_HEADERS, timeout=10)
        if r.status_code == 200:
            data = r.json()
            records = data["records"]["data"]
            spot = data["records"]["underlyingValue"]
            
            total_call_oi = sum(r["CE"]["openInterest"] for r in records if "CE" in r)
            total_put_oi = sum(r["PE"]["openInterest"] for r in records if "PE" in r)
            pcr = total_put_oi / total_call_oi if total_call_oi > 0 else 1.0
            
            call_oi_map = {r["strikePrice"]: r["CE"]["openInterest"] for r in records if "CE" in r}
            put_oi_map = {r["strikePrice"]: r["PE"]["openInterest"] for r in records if "PE" in r}
            
            return {
                "spot": spot,
                "pcr": round(pcr, 2),
                "call_support": max(call_oi_map, key=call_oi_map.get) if call_oi_map else None,
                "put_support": max(put_oi_map, key=put_oi_map.get) if put_oi_map else None,
            }
    except Exception as e:
        logger.warning(f"PCR fetch error: {e}")
    
    return {}

# ==========================================
# 9. NOTIFICATION SYSTEM
# ==========================================
def send_notification(title: str, message: str):
    """Send desktop notification"""
    if HAS_PLYER:
        try:
            notification.notify(
                title=title,
                message=message,
                timeout=10,
            )
        except Exception as e:
            logger.warning(f"Notification error: {e}")
    
    # Also log to session for display
    if "notifications" not in st.session_state:
        st.session_state.notifications = []
    
    st.session_state.notifications.append({
        "title": title,
        "message": message,
        "time": datetime.now(IST).strftime("%H:%M:%S"),
    })

# ==========================================
# 10. AI HYPOTHESIS ENGINE
# ==========================================
@handle_error
def generate_hypothesis(
    stock: str,
    price: float,
    signals: Dict,
    zones: List[DemandSupplyZone],
    news: List[Dict],
    macro_data: Dict,
) -> Dict:
    """Generate AI hypothesis for a stock"""
    
    hypothesis = {
        "stock": stock,
        "price": price,
        "signals": signals,
        "zones": [z.to_dict() for z in zones if z.state != "Filled"],
        "news": news,
        "recommendation": "HOLD",
        "confidence": 0,
        "reasons": [],
    }
    
    bull_score = 0
    bear_score = 0
    
    # Signal scoring
    if "ema_20_50" in signals:
        if "UP" in signals["ema_20_50"]:
            bull_score += 1.5
            hypothesis["reasons"].append("🟢 EMA20/50 Bullish Cross")
        else:
            bear_score += 1.5
            hypothesis["reasons"].append("🔴 EMA20/50 Bearish Cross")
    
    if "vol_spike" in signals:
        bull_score += 0.5
        hypothesis["reasons"].append(f"⚡ {signals['vol_spike']}")
    
    if "rsi" in signals:
        if "OB" in signals["rsi"]:
            bear_score += 0.7
            hypothesis["reasons"].append(f"🔥 {signals['rsi']}")
        elif "OS" in signals["rsi"]:
            bull_score += 0.7
            hypothesis["reasons"].append(f"🧊 {signals['rsi']}")
    
    # D&S Zone scoring
    demand_zones = [z for z in zones if z.is_demand and z.state != "Filled"]
    supply_zones = [z for z in zones if not z.is_demand and z.state != "Filled"]
    
    hq_demand = [z for z in demand_zones if z.is_hq]
    hq_supply = [z for z in supply_zones if z.is_hq]
    
    if hq_demand:
        bull_score += 2.0 * len(hq_demand)
        hypothesis["reasons"].append(f"🚀 {len(hq_demand)} HQ Demand Zone(s)")
    elif demand_zones:
        bull_score += 1.0 * len(demand_zones)
        hypothesis["reasons"].append(f"⭐ {len(demand_zones)} Demand Zone(s)")
    
    if hq_supply:
        bear_score += 2.0 * len(hq_supply)
        hypothesis["reasons"].append(f"🚀 {len(hq_supply)} HQ Supply Zone(s)")
    elif supply_zones:
        bear_score += 1.0 * len(supply_zones)
        hypothesis["reasons"].append(f"⭐ {len(supply_zones)} Supply Zone(s)")
    
    # News impact
    if news:
        bull_score += 0.5
        hypothesis["reasons"].append(f"📰 {len(news)} recent news item(s)")
    
    # Macro impact
    if macro_data.get("sp500_pct", 0) > 0.5:
        bull_score += 0.3
        hypothesis["reasons"].append("🌍 Global bullish (S&P500+)")
    elif macro_data.get("sp500_pct", 0) < -0.5:
        bear_score += 0.3
        hypothesis["reasons"].append("🌍 Global bearish (S&P500-)")
    
    # PCR impact
    pcr = macro_data.get("pcr", 1.0)
    if pcr > 1.2:
        bull_score += 0.3
        hypothesis["reasons"].append("📊 PCR bullish (Put writers)")
    elif pcr < 0.8:
        bear_score += 0.3
        hypothesis["reasons"].append("📊 PCR bearish (Call writers)")
    
    # Final recommendation
    net_score = bull_score - bear_score
    confidence = min(99, int(abs(net_score) * 20))
    
    if net_score > 1.0:
        hypothesis["recommendation"] = "🟢 BUY"
    elif net_score < -1.0:
        hypothesis["recommendation"] = "🔴 SELL"
    else:
        hypothesis["recommendation"] = "🟡 HOLD"
    
    hypothesis["confidence"] = confidence
    hypothesis["net_score"] = round(net_score, 2)
    
    return hypothesis

# ==========================================
# 11. PAGE SETUP
# ==========================================
st.set_page_config(
    page_title="Ultimate Trading Dashboard",
    layout="wide",
    page_icon="📈",
    initial_sidebar_state="collapsed"
)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');
* { font-family: 'Inter', sans-serif; }
[data-testid="stMetricValue"] { font-size: 1.3rem; font-weight: 700; }
[data-testid="stMetric"] { background: #f8f9fa; border-radius: 10px; padding: 15px; border: 1px solid #e0e0e0; }
h1, h2, h3 { color: #0a0e27; font-weight: 700; }
.stAlert { border-radius: 10px; }
</style>
""", unsafe_allow_html=True)

# ==========================================
# 12. SESSION STATE
# ==========================================
if "alerts" not in st.session_state:
    st.session_state.alerts = []
if "notifications" not in st.session_state:
    st.session_state.notifications = []
if "last_refresh" not in st.session_state:
    st.session_state.last_refresh = datetime.now(IST)

# ==========================================
# 13. SIDEBAR
# ==========================================
st.sidebar.header("⚙️ Trading Dashboard Settings")

refresh_seconds = st.sidebar.slider("Auto-Refresh (seconds)", 5, 120, 15, 5)

if HAS_AUTOREFRESH:
    st_autorefresh(interval=int(refresh_seconds * 1000), key="dash_refresh")

st.sidebar.markdown(f"🕒 **IST:** {datetime.now(IST).strftime('%H:%M:%S %d-%b')}")

is_market_open = MARKET_OPEN <= datetime.now(IST).time() <= MARKET_CLOSE and datetime.now(IST).weekday() < 5
st.sidebar.markdown(f"{'🟢 मार्केट खुला' if is_market_open else '🔴 मार्केट बंद'}")

selected_stocks = st.sidebar.multiselect("📊 Stock Watchlist", WATCHLIST, default=WATCHLIST[:15])

st.sidebar.markdown("---")
st.sidebar.subheader("🎯 Scan Settings")

trade_types = st.sidebar.multiselect(
    "Trading Types",
    ["Scalping (1-5m)", "Intraday (15m-4h)", "Swing (Daily)"],
    default=["Intraday (15m-4h)"]
)

show_signals = st.sidebar.checkbox("Show Technical Signals", value=True)
show_zones = st.sidebar.checkbox("Show D&S Zones", value=True)
show_news = st.sidebar.checkbox("Show Real News", value=True)

if st.sidebar.button("🔄 Clear Cache"):
    smart_cache.clear()
    st.rerun()

# ==========================================
# 14. MAIN DASHBOARD
# ==========================================

# Determine timeframes to scan based on trading type
timeframes_to_scan = []
if "Scalping (1-5m)" in trade_types:
    timeframes_to_scan.extend(["1m", "3m", "5m"])
if "Intraday (15m-4h)" in trade_types:
    timeframes_to_scan.extend(["15m", "30m", "1h", "4h"])
if "Swing (Daily)" in trade_types:
    timeframes_to_scan.extend(["1d"])

if not timeframes_to_scan:
    st.warning("कृपया कम से कम एक trading type सलेक्ट करें।")
    st.stop()

# ==========================================
# TAB 1: LIVE ALERTS & SIGNALS
# ==========================================
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "🔔 Live Alerts & Signals",
    "🎯 AI Hypothesis",
    "📰 News & Events",
    "💰 Global Macro",
    "📊 Detailed Analysis"
])

with tab1:
    st.subheader("🔔 Real-Time Signals & D&S Zones")
    
    # Display notifications
    if st.session_state.notifications:
        st.info(f"📢 **Latest Notifications:** {len(st.session_state.notifications)}")
        for notif in st.session_state.notifications[-5:]:
            st.caption(f"🔔 {notif['time']} - {notif['title']}: {notif['message']}")
    
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    signal_rows = []
    total_stocks = len(selected_stocks)
    
    with st.spinner("⚡ Scanning all stocks for signals..."):
        data = parallel_fetch_data(selected_stocks, timeframes_to_scan[0] if timeframes_to_scan else "15m")
        
        for idx, stock in enumerate(selected_stocks):
            progress_bar.progress((idx + 1) / total_stocks)
            status_text.text(f"Processing: {stock} ({idx+1}/{total_stocks})")
            
            if stock not in data:
                continue
            
            stock_data = data[stock]
            
            # Handle tuple returns (multiple timeframes)
            if isinstance(stock_data, tuple):
                dfs_dict = {}
                if len(stock_data) == 3:  # 1m data returns (1m, 3m, 15m)
                    dfs_dict["1m"] = stock_data[0]
                    dfs_dict["3m"] = stock_data[1]
                    dfs_dict["15m"] = stock_data[2]
                elif len(stock_data) == 2:  # 5m data returns (5m, 15m)
                    dfs_dict["5m"] = stock_data[0]
                    dfs_dict["15m"] = stock_data[1]
                else:
                    dfs_dict[timeframes_to_scan[0]] = stock_data[0]
            else:
                dfs_dict = {timeframes_to_scan[0]: stock_data}
            
            for tf_key, df in dfs_dict.items():
                if df is None or df.empty or len(df) < 15:
                    continue
                
                # Get signals
                signals = get_technical_signals(df)
                if not signals:
                    continue
                
                # Get D&S zones
                zones = scan_ds_zones(df, tf_key)
                
                # Build signal string
                signal_parts = []
                if show_signals:
                    if "ema_20_50" in signals:
                        signal_parts.append(signals["ema_20_50"])
                    if "vol_spike" in signals:
                        signal_parts.append(signals["vol_spike"])
                    if "rsi" in signals:
                        signal_parts.append(signals["rsi"])
                
                if show_zones and zones:
                    hq_zones = [z for z in zones if z.is_hq and z.state != "Filled"]
                    if hq_zones:
                        signal_parts.append(f"🚀 {len(hq_zones)} HQ Zone(s)")
                    else:
                        zone_count = sum(1 for z in zones if z.state != "Filled")
                        if zone_count > 0:
                            signal_parts.append(f"⭐ {zone_count} Zone(s)")
                
                if not signal_parts:
                    continue
                
                signal_text = " | ".join(signal_parts)
                price = signals.get("current_price", df['Close'].iloc[-1])
                
                # Determine signal quality
                has_hq = any("🚀" in p for p in signal_parts)
                has_multiple = len(signal_parts) >= 2
                
                if has_hq:
                    stars = "🚀 HQ"
                    color = "🟢" if "UP" in signal_text or "DEMAND" in signal_text else "🔴"
                elif has_multiple:
                    stars = "⭐⭐ Strong"
                    color = "🟢" if "UP" in signal_text or "DEMAND" in signal_text else "🔴"
                else:
                    stars = "⭐ Signal"
                    color = ""
                
                signal_rows.append({
                    "Signal": stars,
                    "Stock": stock,
                    "TF": TIMEFRAMES_CONFIG[tf_key]["display"],
                    "Type": signal_text,
                    "Price": round(price, 2),
                    "Time": df.index[-1].strftime("%H:%M"),
                    "Chart": f"[Open](https://www.tradingview.com/chart/?symbol=NSE:{TV_FIX.get(stock, stock)})",
                })
                
                # Send notification for HQ signals
                if has_hq:
                    send_notification(
                        f"🚀 HQ Signal: {stock}",
                        f"{signal_text} @ {price:.2f} ({tf_key})"
                    )
    
    progress_bar.empty()
    status_text.empty()
    
    if not signal_rows:
        st.success("✅ अभी कोई सिग्नल नहीं मिला।")
    else:
        sig_df = pd.DataFrame(signal_rows)
        
        # Sort by signal quality
        rank = {"🚀 HQ": 3, "⭐⭐ Strong": 2, "⭐ Signal": 1}
        sig_df["_rank"] = sig_df["Signal"].map(rank)
        sig_df = sig_df.sort_values(["_rank", "Time"], ascending=[False, False]).drop("_rank", axis=1)
        
        # Style dataframe
        def style_signal(row):
            colors = {
                "🚀 HQ": "background-color: #d1e7dd; font-weight: bold;",
                "⭐⭐ Strong": "background-color: #e8f4f8;",
                "⭐ Signal": "background-color: #fff3cd;",
            }
            base = colors.get(row["Signal"], "")
            return [base] * len(row)
        
        st.dataframe(
            sig_df.style.apply(style_signal, axis=1),
            use_container_width=True,
            hide_index=True,
            column_config={"Chart": st.column_config.LinkColumn("Chart", display_text="📈 Open")}
        )

with tab2:
    st.subheader("🎯 AI Buy/Sell Hypothesis — Real Market Data से")
    
    if st.button("▶️ Generate AI Hypothesis", key="gen_hyp"):
        hypothesis_rows = []
        
        with st.spinner("🤖 Analyzing all stocks..."):
            # Get macro data
            quotes = get_quotes(["^GSPC", "CL=F", "DX-Y.NYB"])
            sp500_pct = quotes.get("^GSPC", {}).get("pct", 0)
            crude_pct = quotes.get("CL=F", {}).get("pct", 0)
            dxy_pct = quotes.get("DX-Y.NYB", {}).get("pct", 0)
            
            nifty_data = fetch_nifty_pcr()
            pcr = nifty_data.get("pcr", 1.0)
            
            macro_data = {
                "sp500_pct": sp500_pct,
                "crude_pct": crude_pct,
                "dxy_pct": dxy_pct,
                "pcr": pcr,
            }
            
            # Fetch data and generate hypothesis
            data = parallel_fetch_data(selected_stocks, timeframes_to_scan[0] if timeframes_to_scan else "1h")
            
            for stock in selected_stocks:
                if stock not in data:
                    continue
                
                stock_data = data[stock]
                if isinstance(stock_data, tuple):
                    df = stock_data[0]  # Use first dataframe
                else:
                    df = stock_data
                
                if df is None or df.empty or len(df) < 15:
                    continue
                
                signals = get_technical_signals(df)
                zones = scan_ds_zones(df)
                news = fetch_stock_news(stock)
                
                price = signals.get("current_price", df['Close'].iloc[-1])
                
                hyp = generate_hypothesis(stock, price, signals, zones, news, macro_data)
                
                hypothesis_rows.append(hyp)
        
        # Display results
        if not hypothesis_rows:
            st.warning("कोई hypothesis नहीं बना सका।")
        else:
            # Split by recommendation
            buy_hyp = [h for h in hypothesis_rows if "BUY" in h["recommendation"]]
            sell_hyp = [h for h in hypothesis_rows if "SELL" in h["recommendation"]]
            
            col_buy, col_sell = st.columns(2)
            
            with col_buy:
                st.markdown("### 🟢 BUY HYPOTHESIS")
                buy_hyp.sort(key=lambda x: x["confidence"], reverse=True)
                
                if not buy_hyp:
                    st.info("कोई BUY setup नहीं मिला।")
                else:
                    for h in buy_hyp[:5]:
                        st.markdown(
                            f"""
                            <div style="background-color: #e6f4ea; border-radius: 10px; padding: 15px; margin-bottom: 10px; border: 2px solid #34a853;">
                            <b style="font-size: 15px;">{h['stock']}</b> @ ₹{h['price']:.2f}
                            <br><span style="font-size: 12px;">📊 Confidence: <b>{h['confidence']}%</b></span>
                            <br><span style="font-size: 12px;">Score: {h['net_score']}</span>
                            <br><small>{'<br>'.join(h['reasons'][:4])}</small>
                            <br><small>Zones: {len(h['zones'])} | News: {len(h['news']) if 'news' in h else 0}</small>
                            </div>
                            """,
                            unsafe_allow_html=True
                        )
            
            with col_sell:
                st.markdown("### 🔴 SELL HYPOTHESIS")
                sell_hyp.sort(key=lambda x: x["confidence"], reverse=True)
                
                if not sell_hyp:
                    st.info("कोई SELL setup नहीं मिला।")
                else:
                    for h in sell_hyp[:5]:
                        st.markdown(
                            f"""
                            <div style="background-color: #fce8e6; border-radius: 10px; padding: 15px; margin-bottom: 10px; border: 2px solid #ea4335;">
                            <b style="font-size: 15px;">{h['stock']}</b> @ ₹{h['price']:.2f}
                            <br><span style="font-size: 12px;">📊 Confidence: <b>{h['confidence']}%</b></span>
                            <br><span style="font-size: 12px;">Score: {h['net_score']}</span>
                            <br><small>{'<br>'.join(h['reasons'][:4])}</small>
                            <br><small>Zones: {len(h['zones'])} | News: {len(h['news']) if 'news' in h else 0}</small>
                            </div>
                            """,
                            unsafe_allow_html=True
                        )
    else:
        st.info("💡 **Generate AI Hypothesis** बटन पर क्लिक करके real market data से BUY/SELL setup देखें।")

with tab3:
    st.subheader("📰 Real News & Market Events")
    
    # Get announcements
    announcements = fetch_nse_announcements()
    
    # Get market events
    events = fetch_market_events()
    
    col_news, col_events = st.columns(2)
    
    with col_news:
        st.markdown("### 📢 Corporate Announcements")
        if not announcements:
            st.info("अभी कोई announcement नहीं।")
        else:
            for ann in announcements[:10]:
                if ann["symbol"] in selected_stocks:
                    st.markdown(
                        f"""
                        <div style="background-color: #f8f9fa; border-radius: 8px; padding: 10px; margin-bottom: 8px; border-left: 4px solid #0066cc;">
                        <b style="color: #0066cc;">{ann['symbol']}</b> — {ann['time']}
                        <br><small>{ann['subject']}</small>
                        </div>
                        """,
                        unsafe_allow_html=True
                    )
    
    with col_events:
        st.markdown("### 🌍 Market Events (Today)")
        if not events:
            st.info("कोई high-impact event नहीं।")
        else:
            for evt in events[:10]:
                impact_color = {"High": "#ea4335", "Medium": "#fbbc04"}.get(.get["impact"], "#555")
                st.markdown(
                    f"""
                    <div style="background-color: #fef7e0; border-radius: 8px; padding: 10px; margin-bottom: 8px; border-left: 4px solid {impact_color};">
                    <b style="color: {impact_color};">{evt['country']}</b> — {evt['time']}
                    <br><small>{evt['event']}</small>
                    </div>
                    """,
                    unsafe_allow_html=True
                )
    
    st.markdown("---")
    st.markdown("### 📰 Stock-Specific News")
    
    for stock in selected_stocks[:10]:
        news = fetch_stock_news(stock)
        if news:
            with st.expander(f"📰 {stock} News"):
                for n in news:
                    st.markdown(
                        f"[{n['title']}]({n['link']}) — {n['published']}"
                    )

with tab4:
    st.subheader("💰 Global Macro Data")
    
    # Fetch quotes
    quotes = get_quotes([g[2] for g in GLOBAL_INSTRUMENTS if g[2]])
    
    # Nifty data
    nifty_data = fetch_nifty_pcr()
    
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        sp500 = quotes.get("^GSPC", {})
        st.metric("S&P 500", f"{sp500.get('price', 'N/A'):.0f}", f"{sp500.get('pct', 0):+.2f}%")
    
    with col2:
        crude = quotes.get("CL=F", {})
        st.metric("Crude Oil", f"${crude.get('price', 'N/A'):.2f}", f"{crude.get('pct', 0):+.2f}%")
    
    with col3:
        dxy = quotes.get("DX-Y.NYB", {})
        st.metric("DXY", f"{dxy.get('price', 'N/A'):.2f}", f"{dxy.get('pct', 0):+.2f}%")
    
    with col4:
        usdinr = quotes.get("INR=X", {})
        st.metric("USD/INR", f"{usdinr.get('price', 'N/A'):.2f}", f"{usdinr.get('pct', 0):+.2f}%")
    
    st.markdown("---")
    
    col_nifty, col_macro = st.columns(2)
    
    with col_nifty:
        st.markdown("### 🎯 Nifty Option Data")
        st.metric("Spot", f"{nifty_data.get('spot', 'N/A'):.0f}")
        st.metric("PCR (OI)", f"{nifty_data.get('pcr', 'N/A')}")
        st.metric("Call Support", nifty_data.get('call_support', 'N/A'))
        st.metric("Put Support", nifty_data.get('put_support', 'N/A'))
    
    with col_macro:
        st.markdown("### 🌍 Market Interpretation")
        
        sp500_pct = quotes.get("^GSPC", {}).get("pct", 0)
        crude_pct = quotes.get("CL=F", {}).get("pct", 0)
        dxy_pct = quotes.get("DX-Y.NYB", {}).get("pct", 0)
        pcr = nifty_data.get("pcr", 1.0)
        
        interpretation = []
        
        if sp500_pct > 0.5:
            interpretation.append("🟢 Global bullish (US stocks strong)")
        elif sp500_pct < -0.5:
            interpretation.append("🔴 Global bearish (US stocks weak)")
        
        if crude_pct < -1:
            interpretation.append("🟢 Crude down (Good for importers)")
        elif crude_pct > 1:
            interpretation.append("🔴 Crude up (Bad for importers)")
        
        if dxy_pct > 0.3:
            interpretation.append("🔴 Rupee under pressure (Dollar strong)")
        elif dxy_pct < -0.3:
            interpretation.append("🟢 Rupee strength (Dollar weak)")
        
        if pcr > 1.2:
            interpretation.append("🟢 PCR High (Put writers bullish)")
        elif pcr < 0.8:
            interpretation.append("🔴 PCR Low (Call writers bearish)")
        
        if interpretation:
            for item in interpretation:
                st.info(item)
        else:
            st.info("🟡 Macro data neutral — no strong signals")

with tab5:
    st.subheader("📊 Detailed Stock Analysis")
    
    selected_for_detail = st.selectbox("Select Stock for Analysis", selected_stocks)
    
    if st.button("▶️ Analyze"):
        with st.spinner(f"Analyzing {selected_for_detail}..."):
            # Fetch data for all timeframes
            tf_data = {}
            for tf_key in timeframes_to_scan:
                tf_cfg = TIMEFRAMES_CONFIG[tf_key]
                result = get_stock_data(selected_for_detail, tf_cfg["period"], tf_cfg["yf"])
                
                if result:
                    if isinstance(result, tuple):
                        # For 1m with 3m/15m resampling
                        tf_data[tf_key] = result[0]  # Use base timeframe
                    else:
                        tf_data[tf_key] = result
            
            if not tf_data:
                st.error(f"कोई data नहीं मिला {selected_for_detail} के लिए।")
            else:
                # Display analysis for each timeframe
                for tf_key in timeframes_to_scan:
                    if tf_key not in tf_data:
                        continue
                    
                    df = tf_data[tf_key]
                    tf_display = TIMEFRAMES_CONFIG[tf_key]["display"]
                    
                    with st.expander(f"📊 {tf_display} Analysis"):
                        col_price, col_signal, col_zone = st.columns(3)
                        
                        with col_price:
                            st.metric("Current Price", f"₹{df['Close'].iloc[-1]:.2f}")
                            chg = df['Close'].iloc[-1] - df['Close'].iloc[-2]
                            pct = (chg / df['Close'].iloc[-2]) * 100
                            st.metric("Change", f"{chg:+.2f} ({pct:+.2f}%)")
                        
                        with col_signal:
                            signals = get_technical_signals(df)
                            st.metric("EMA 20/50", signals.get("ema_20_50", "—"))
                            st.metric("RSI (14)", f"{signals.get('rsi_value', 0):.0f}")
                        
                        with col_zone:
                            zones = scan_ds_zones(df, tf_key)
                            active_zones = [z for z in zones if z.state != "Filled"]
                            
                            if active_zones:
                                st.metric("Active Zones", len(active_zones))
                                hq = sum(1 for z in active_zones if z.is_hq)
                                st.metric("HQ Zones", hq)
                            else:
                                st.info("No active zones")
                        
                        # Display zones
                        if active_zones:
                            st.markdown("**Demand/Supply Zones:**")
                            for z in active_zones:
                                zone_data = z.to_dict()
                                badge = "🚀 HQ" if z.is_hq else "⭐"
                                side_emoji = "🟢" if z.is_demand else "🔴"
                                st.caption(
                                    f"{side_emoji} {badge} {zone_data['Side']} @ {zone_data['Entry']} "
                                    f"| SL: {zone_data['SL']} | TP: {zone_data['TP']} | {zone_data['State']}"
                                )

# ==========================================
# FOOTER
# ==========================================
st.markdown("---")
st.caption(
    f"🤖 **Ultimate Trading Dashboard** | "
    f"⚡ Auto-Refresh: {refresh_seconds}s | "
    f"📊 Stocks: {len(selected_stocks)} | "
    f"🔔 Alerts: {len(st.session_state.alerts)} | "
    f"⏰ Last Update: {st.session_state.last_refresh.strftime('%H:%M:%S')}"
)

st.session_state.last_refresh = datetime.now(IST)
