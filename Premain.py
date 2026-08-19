"""
app.py — Full Market Dashboard (Streamlit) + Incremental D&S Cache + AI Hypothesis + Market Pulse
+ 🆕 Persistent Daily Data Store (News/Events/FII-DII) with Auto-Clear at 11 PM IST
=====================================================================================
Institutional D&S Zones (incremental/cached scan, sensitivity-mode) | EMA/Volume/RSI Signals |
Global Markets | Sector Impact | Watchlist | Economic Calendar | FII/DII + Nifty OI |
Delivery% + Bulk/Block Deals | Gainers/Losers | Evidence-Based AI Buy/Sell Hypothesis |
🌅 Market Pulse (Pre-Market / Live Snapshot: GIFT Nifty, Nifty/BankNifty, Sectors, Global,
OI/PCR, FII/DII, Heavyweights, News, Overall Bias) | Mobile-Friendly UI |
🗄️ Daily Data Log (दिनभर जमा News/Events/FII-DII, evidence-based वॉचलिस्ट इम्पैक्ट, रात 11 बजे ऑटो-क्लियर)

⚠️ EDUCATIONAL / INFORMATIONAL TOOL — SEBI-registered निवेश सलाह नहीं है।
"""

import concurrent.futures
import io
import urllib.parse
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from typing import List, Optional, Tuple, Dict, Any
from dataclasses import dataclass, field

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

import streamlit.components.v1 as components

# ==========================================
# 1. INSTITUTIONAL D&S CONFIGURATION
# ==========================================
TARGET_RR = 5.0
SL_BUFFER_ATR = 0.1
ATR_PERIOD = 14
LEG_OUT_ATR_MULT = 1.2
HQ_LEG_OUT_ATR = 2.0
MAX_BASE_ATR_MULT = 1.0
MAX_WICK_PCT = 0.25
USE_SWEEP_FILTER = True
USE_IMBALANCE = True
MIN_BASE_COUNT = 1
MAX_BASE_COUNT = 3
REQ_LEG_IN_VOL = True

MIN_PROXIMITY_PCT = 0.005
MAX_PROXIMITY_PCT = 0.010

LEG_IN_STRONG_CLOSE_PCT = 0.70
BASE_MAX_BODY_RATIO     = 0.35
BASE_MIN_OVERLAP_PCT    = 0.50
BASE_VOL_MAX_RATIO      = 1.0
LEG_OUT_VOL_LOOKBACK    = 20
LEG_OUT_VOL_MULT        = 1.5
MTF_BUFFER_ATR_MULT     = 0.15

# 🆕 Incremental Zone-Scan Cache Config
# (पहले यह 40 था — इतना छोटा buffer ATR/swing-context को हर refresh पर तोड़ देता था
#  जिससे sweep-filter झूठा false हो जाता था और नए zones आना बंद हो जाते थे)
CONTEXT_BUFFER = 300

# 🆕 D&S Sensitivity Mode — sidebar से override होता है (default यहां रखा है ताकि
# किसी भी हालत में NameError न आए)
ds_mode = "Balanced"

# ==========================================
# 2. VECTORIZED NUMPY CALCULATIONS
# ==========================================
def calculate_atr_np(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(high)
    if n < 2:
        return np.zeros(n)
    tr = np.maximum(high[1:] - low[1:], np.maximum(np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1])))
    tr = np.insert(tr, 0, high[0] - low[0])
    atr = np.empty(n, dtype=np.float64)
    atr[0] = tr[0]
    alpha = 1.0 / period
    one_minus_alpha = 1.0 - alpha
    for i in range(1, n):
        atr[i] = alpha * tr[i] + one_minus_alpha * atr[i-1]
    return atr

def calculate_pivots_np(highs: np.ndarray, lows: np.ndarray, left: int = 5, right: int = 5) -> Tuple[np.ndarray, np.ndarray]:
    n = len(highs)
    pivot_highs = np.full(n, np.nan)
    pivot_lows = np.full(n, np.nan)
    for i in range(left, n - right):
        window_highs = highs[i - left : i + right + 1]
        if highs[i] == np.max(window_highs) and np.sum(window_highs == highs[i]) == 1:
            pivot_highs[i] = highs[i]
        window_lows = lows[i - left : i + right + 1]
        if lows[i] == np.min(window_lows) and np.sum(window_lows == lows[i]) == 1:
            pivot_lows[i] = lows[i]
    return pivot_highs, pivot_lows

def _leg_in_strong_close_ok(open_v, high_v, low_v, close_v, is_bull) -> bool:
    rng = high_v - low_v
    if rng <= 0:
        return False
    if is_bull:
        return ((close_v - low_v) / rng) >= LEG_IN_STRONG_CLOSE_PCT
    return ((high_v - close_v) / rng) >= LEG_IN_STRONG_CLOSE_PCT

def _base_body_ratio_ok(o_arr, h_arr, l_arr, c_arr) -> bool:
    rng = h_arr - l_arr
    body = np.abs(c_arr - o_arr)
    safe_rng = np.where(rng == 0, 1e-9, rng)
    ratios = body / safe_rng
    return bool(np.all(np.where(rng == 0, 0.0, ratios) <= BASE_MAX_BODY_RATIO))

def _base_overlap_ok(base_high_arr, base_low_arr) -> bool:
    if len(base_high_arr) < 2:
        return True
    for k in range(1, len(base_high_arr)):
        h1, l1 = base_high_arr[k-1], base_low_arr[k-1]
        h2, l2 = base_high_arr[k], base_low_arr[k]
        overlap = min(h1, h2) - max(l1, l2)
        min_rng = min(h1 - l1, h2 - l2)
        if min_rng <= 0 or (overlap / min_rng) < BASE_MIN_OVERLAP_PCT:
            return False
    return True

def validate_mtf_no_break(lower_df, leg_in_time, leg_out_time, boundary_val, is_demand, atr_val,
                           buffer_mult=MTF_BUFFER_ATR_MULT) -> bool:
    if lower_df is None or lower_df.empty:
        return True
    try:
        window = lower_df[(lower_df.index >= leg_in_time) & (lower_df.index <= leg_out_time)]
    except Exception:
        return True
    if window.empty or len(window) < 2:
        return True
    buffer = buffer_mult * atr_val if atr_val > 0 else 0.0
    if is_demand:
        return float(window['Low'].min()) >= (boundary_val - buffer)
    return float(window['High'].max()) <= (boundary_val + buffer)


class Zone:
    def __init__(self, prox_val, dist_val, sl_val, tp_val, is_demand, is_hq, density_score,
                 start_idx, mtf_confirmed: bool = True):
        self.prox_val = prox_val
        self.dist_val = dist_val
        self.sl_val = sl_val
        self.tp_val = tp_val
        self.is_demand = is_demand
        self.is_hq = is_hq
        self.density_score = density_score
        self.state = "Fresh"
        self.touch_count = 0
        self.mtf_confirmed = mtf_confirmed
        self.start_idx = start_idx


def scan_institutional_ds_zones(df: pd.DataFrame, lower_tf_df: Optional[pd.DataFrame] = None,
                                 use_mtf: bool = False) -> List[Zone]:
    """Core (non-incremental) zone-detection engine — किसी भी दिए गए df-slice पर पूरा scan करता है।
    Incremental caching wrapper (नीचे) इसे सिर्फ नए bars के छोटे slice पर call करता है, इसलिए
    यह function खुद नहीं बदला — बस इसे कम बार, छोटे data पर बुलाया जाता है।
    ⚠️ NOTE: USE_SWEEP_FILTER, USE_IMBALANCE, LEG_OUT_ATR_MULT, MAX_WICK_PCT, LEG_OUT_VOL_MULT
    ये सारे module-level globals हैं और sidebar के "D&S Sensitivity Mode" से runtime पर override
    होते हैं — इसलिए यह function उन्हें हमेशा current (ताज़ा) value के साथ पढ़ेगा।"""
    if df is None or len(df) < 30:
        return []

    high = df['High'].to_numpy(dtype=np.float64)
    low = df['Low'].to_numpy(dtype=np.float64)
    close = df['Close'].to_numpy(dtype=np.float64)
    open_p = df['Open'].to_numpy(dtype=np.float64)
    volume = df['Volume'].to_numpy(dtype=np.float64)
    idx = df.index
    n = len(high)

    atr = calculate_atr_np(high, low, close, ATR_PERIOD)
    pivot_h, pivot_l = calculate_pivots_np(high, low, 5, 5)

    tr = high - low
    is_bull = close > open_p
    is_bear = open_p > close

    body_max = np.maximum(open_p, close)
    body_min = np.minimum(open_p, close)
    wicks = (high - body_max) + (body_min - low)
    wick_pct = np.where(tr == 0, 0.0, wicks / tr)

    all_zones: List[Zone] = []
    last_swing_high = np.nan
    last_swing_low = np.nan

    for i in range(15, n):
        if not np.isnan(pivot_h[i]):
            last_swing_high = pivot_h[i]
        if not np.isnan(pivot_l[i]):
            last_swing_low = pivot_l[i]

        zone_found = False

        for base_count in range(MIN_BASE_COUNT, MAX_BASE_COUNT + 1):
            if zone_found:
                break

            leg_out_idx = i
            leg_in_idx = i - base_count - 1
            if leg_in_idx < 0:
                continue

            leg_out_tr = tr[leg_out_idx]
            leg_in_tr = tr[leg_in_idx]
            leg_out_atr = atr[leg_out_idx]
            leg_in_atr = atr[leg_in_idx]

            valid_leg_in = True
            if REQ_LEG_IN_VOL:
                valid_leg_in = (volume[leg_in_idx] >= volume[leg_in_idx - 1] * 0.8) and \
                               (leg_in_tr >= 0.8 * leg_in_atr)

            passes_volume = volume[leg_out_idx] > volume[leg_in_idx]
            is_leg_out_explosive = leg_out_tr >= (LEG_OUT_ATR_MULT * leg_out_atr)
            is_leg_out_wick_valid = wick_pct[leg_out_idx] <= MAX_WICK_PCT

            is_demand_leg_out = is_bull[leg_out_idx]
            is_supply_leg_out = is_bear[leg_out_idx]

            base_open_slice = open_p[leg_in_idx + 1 : leg_out_idx]
            base_close_slice = close[leg_in_idx + 1 : leg_out_idx]
            base_tr_slice = tr[leg_in_idx + 1 : leg_out_idx]
            base_atr_slice = atr[leg_in_idx + 1 : leg_out_idx]
            base_high_slice = high[leg_in_idx + 1 : leg_out_idx]
            base_low_slice = low[leg_in_idx + 1 : leg_out_idx]
            base_vol_slice = volume[leg_in_idx + 1 : leg_out_idx]

            max_base_tr = np.max(base_tr_slice)
            max_base_high = np.max(base_high_slice)
            min_base_low = np.min(base_low_slice)

            all_base_valid = np.all(base_tr_slice <= (MAX_BASE_ATR_MULT * base_atr_slice))
            body_ratio_ok = _base_body_ratio_ok(base_open_slice, base_high_slice, base_low_slice, base_close_slice)

            base_vol_ok = True
            if len(base_vol_slice) > 0:
                base_vol_ok = np.mean(base_vol_slice) <= (BASE_VOL_MAX_RATIO * volume[leg_in_idx])

            overlap_ok = _base_overlap_ok(base_high_slice, base_low_slice)
            leg_in_strong_close_ok = _leg_in_strong_close_ok(
                open_p[leg_in_idx], high[leg_in_idx], low[leg_in_idx], close[leg_in_idx], is_bull[leg_in_idx]
            )

            lookback_start = max(0, leg_out_idx - LEG_OUT_VOL_LOOKBACK)
            avg_vol_20 = np.mean(volume[lookback_start:leg_out_idx]) if leg_out_idx > lookback_start else 0.0
            passes_leg_out_vol_climax = avg_vol_20 > 0 and (volume[leg_out_idx] >= LEG_OUT_VOL_MULT * avg_vol_20)

            passes_tr_hierarchy = (leg_out_tr > leg_in_tr) and (leg_in_tr > max_base_tr)

            is_rbr = is_bull[leg_in_idx] and is_demand_leg_out
            is_dbr = is_bear[leg_in_idx] and is_demand_leg_out
            is_dbd = is_bear[leg_in_idx] and is_supply_leg_out
            is_rbd = is_bull[leg_in_idx] and is_supply_leg_out

            has_bos = False
            if is_demand_leg_out:
                has_bos = close[leg_out_idx] > max(high[leg_in_idx], max_base_high)
            elif is_supply_leg_out:
                has_bos = close[leg_out_idx] < min(low[leg_in_idx], min_base_low)

            has_imbalance = True
            if USE_IMBALANCE:
                if is_demand_leg_out:
                    has_imbalance = (low[leg_out_idx] > max_base_high) or (close[leg_out_idx] > high[leg_in_idx])
                elif is_supply_leg_out:
                    has_imbalance = (high[leg_out_idx] < min_base_low) or (close[leg_out_idx] < low[leg_in_idx])

            swept_liquidity = False
            if is_demand_leg_out and not np.isnan(last_swing_low):
                swept_liquidity = (min_base_low < last_swing_low) or (low[leg_in_idx] < last_swing_low)
            elif is_supply_leg_out and not np.isnan(last_swing_high):
                swept_liquidity = (max_base_high > last_swing_high) or (high[leg_in_idx] > last_swing_high)

            passes_sweep_check = swept_liquidity if USE_SWEEP_FILTER else True

            prox_val = max_base_high if is_demand_leg_out else min_base_low
            dist_val = min_base_low if is_demand_leg_out else max_base_high

            mtf_ok = True
            if use_mtf and lower_tf_df is not None:
                try:
                    leg_in_time = idx[leg_in_idx]
                    end_pos = leg_out_idx + 1 if (leg_out_idx + 1) < n else leg_out_idx
                    leg_out_time = idx[end_pos]
                    mtf_ok = validate_mtf_no_break(lower_tf_df, leg_in_time, leg_out_time, dist_val,
                                                    is_demand_leg_out, leg_out_atr)
                except Exception:
                    mtf_ok = True

            is_valid = (is_rbr or is_dbr or is_dbd or is_rbd) and all_base_valid and valid_leg_in and \
                       is_leg_out_explosive and is_leg_out_wick_valid and passes_tr_hierarchy and \
                       has_bos and passes_volume and has_imbalance and passes_sweep_check and \
                       body_ratio_ok and base_vol_ok and overlap_ok and leg_in_strong_close_ok and \
                       passes_leg_out_vol_climax and mtf_ok

            if is_valid:
                zone_found = True
                density_score = 25
                if leg_out_tr >= HQ_LEG_OUT_ATR * leg_out_atr:
                    density_score += 25
                if swept_liquidity:
                    density_score += 25
                if base_count <= 2 and max_base_tr <= 0.7 * atr[i-1]:
                    density_score += 25
                is_hq = density_score >= 75

                curr_atr = leg_out_atr
                sl_val = (dist_val - (SL_BUFFER_ATR * curr_atr)) if is_demand_leg_out else (dist_val + (SL_BUFFER_ATR * curr_atr))
                risk_per_share = abs(prox_val - sl_val)
                tp_val = (prox_val + (risk_per_share * TARGET_RR)) if is_demand_leg_out else (prox_val - (risk_per_share * TARGET_RR))

                is_duplicate = False
                for existing in all_zones[-10:]:
                    if existing.is_demand == is_demand_leg_out and abs(existing.prox_val - prox_val) < (curr_atr * 0.25):
                        is_duplicate = True
                        break

                if not is_duplicate:
                    all_zones.append(Zone(prox_val, dist_val, sl_val, tp_val, is_demand_leg_out, is_hq,
                                           density_score, i, mtf_confirmed=mtf_ok))

        curr_high = high[i]
        curr_low = low[i]
        for z in all_zones:
            if z.state == "Filled":
                continue
            if z.is_demand:
                touched = curr_low <= z.prox_val
                fully_filled = curr_low <= z.dist_val
            else:
                touched = curr_high >= z.prox_val
                fully_filled = curr_high >= z.dist_val
            if fully_filled:
                z.state = "Filled"
            elif touched:
                z.touch_count += 1
                z.state = "Retest"

    return all_zones


# ==========================================
# 🆕 2c. INCREMENTAL / CACHED D&S ZONE SCANNER
# ==========================================
def _ds_cache_key(symbol: str, tf_key: str, use_mtf: bool) -> str:
    # 🆕 ds_mode भी key में जोड़ा — ताकि sensitivity mode बदलने पर पुराना (गलत) cache इस्तेमाल न हो
    return f"{symbol}::{tf_key}::mtf{int(use_mtf)}::{ds_mode}"

def scan_institutional_ds_zones_incremental(df: pd.DataFrame, symbol: str, tf_key: str,
                                             lower_tf_df: Optional[pd.DataFrame] = None,
                                             use_mtf: bool = False) -> List[Zone]:
    """
    हर refresh पर पूरा इतिहास दोबारा scan करने की बजाय:
      1. अगर कोई नया bar ही नहीं आया -> cache से सीधे लौटाओ (ZERO compute)
      2. पहली बार -> पूरा history scan (एक बार ही)
      3. बाद में -> सिर्फ (timestamp-आधारित) नए bars + CONTEXT_BUFFER पर scan,
         पुराने zones की state सिर्फ नए bars पर update होती है (index-shift-safe,
         क्योंकि touch/fill check हमेशा price-level पर होता है, position पर नहीं)।
      🆕 अगर CONTEXT_BUFFER, history की शुरुआत तक पहुंच जाए (यानी scan_start==0),
         तो ATR/swing-pivot warm-up context खोने से बचने के लिए पूरा rescan किया जाता है
         (यह सस्ता ही रहता है क्योंकि ऐसा सिर्फ शुरुआती bars के लिए होता है)।
    """
    if df is None or df.empty:
        return []

    if "ds_zone_cache" not in st.session_state:
        st.session_state.ds_zone_cache = {}

    key = _ds_cache_key(symbol, tf_key, use_mtf)
    cache = st.session_state.ds_zone_cache.get(key)
    last_bar_time = df.index[-1]

    # ---- Case 1: कुछ भी नया नहीं आया ----
    if cache is not None and cache["last_ts"] == last_bar_time:
        return cache["zones"]

    # ---- Case 2: पहली बार (कोई cache नहीं) ----
    if cache is None:
        zones = scan_institutional_ds_zones(df, lower_tf_df=lower_tf_df, use_mtf=use_mtf)
        st.session_state.ds_zone_cache[key] = {"zones": zones, "last_ts": last_bar_time}
        return zones

    # ---- Case 3: Incremental — timestamp आधारित नए bars निकालो ----
    new_mask = df.index > cache["last_ts"]
    new_count = int(new_mask.sum())

    if new_count == 0:
        # bar-time technically अलग है (जैसे resample shift) पर कोई genuinely नया bar नहीं
        st.session_state.ds_zone_cache[key]["last_ts"] = last_bar_time
        return cache["zones"]

    new_positions = np.where(new_mask)[0]
    first_new_pos = int(new_positions[0])
    scan_start = max(0, first_new_pos - CONTEXT_BUFFER)

    # 🆕 अगर buffer history की शुरुआत तक पहुंच जाए तो ATR/swing-context सही रखने के लिए
    # पूरा rescan करें (यही असली bug था — छोटे slice में ATR/swing warm-up गलत आता था)
    if scan_start == 0:
        zones = scan_institutional_ds_zones(df, lower_tf_df=lower_tf_df, use_mtf=use_mtf)
        st.session_state.ds_zone_cache[key] = {"zones": zones, "last_ts": last_bar_time}
        return zones

    sub_df = df.iloc[scan_start:]

    # नए candidates सिर्फ छोटे sub_df पर (सस्ता compute)
    new_zone_candidates = scan_institutional_ds_zones(sub_df, lower_tf_df=lower_tf_df, use_mtf=use_mtf)

    old_zones = cache["zones"]
    merged = list(old_zones)
    for nz in new_zone_candidates:
        is_dup = any(
            oz.is_demand == nz.is_demand and abs(oz.prox_val - nz.prox_val) < max(nz.prox_val * 0.002, 0.01)
            for oz in old_zones[-15:]
        )
        if not is_dup:
            merged.append(nz)

    # पुराने zones की state सिर्फ genuinely-नए bars पर update करो (price-level आधारित, index-safe)
    new_high = df["High"].to_numpy(dtype=np.float64)[new_mask]
    new_low = df["Low"].to_numpy(dtype=np.float64)[new_mask]

    for z in old_zones:
        if z.state == "Filled":
            continue
        for h, l in zip(new_high, new_low):
            if z.is_demand:
                touched, filled = l <= z.prox_val, l <= z.dist_val
            else:
                touched, filled = h >= z.prox_val, h >= z.dist_val
            if filled:
                z.state = "Filled"
                break
            elif touched:
                z.touch_count += 1
                z.state = "Retest"

    st.session_state.ds_zone_cache[key] = {"zones": merged, "last_ts": last_bar_time}
    return merged

# ==========================================
# 3. GLOBAL MASTER LIST & MARKET TIME SETUP
# ==========================================
IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

COLOR_POS_BG, COLOR_POS_TEXT = "#d4f8d4", "#0a7d2f"
COLOR_NEG_BG, COLOR_NEG_TEXT = "#f8f8d4", "#c0392b"
COLOR_FLAT_TEXT = "#555555"
COLOR_SPIKE_BG = "#ffe1a8"

RAW_STOCKS = """TCS,M&M,HCLTECH,SBIN,INFY,HINDUNILVR,RELIANCE,BHARTIARTL,BEL,ONGC,
BAJAJ_AUTO,NESTLEIND,POWERGRID,ULTRACEMCO,ITC,ADANIPORTS,LT,COALINDIA,ADANIENT,
SUNPHARMA,MARUTI,ETERNAL,HDFCBANK,JSWSTEEL,NTPC,ASIANPAINT,DMART,KOTAKBANK,
TATASTEEL,TITAN,AXISBANK,SHRIRAMFIN,ICICIBANK,BAJFINANCE,TATAMOTORS,MOTHERSON,
BRITANNIA,HEROMOTOCO,TVSMOTOR,PERSISTENT,TECHM,MCX,OIL,RECLTD,AUROPHARMA,COFORGE,
BSE,LAURUSLABS,EICHERMOT,LUPIN,CUMMINSIND,MUTHOOTFIN,INDUSTOWER,MAXHEALTH,
HINDALCO,JSWENERGY,BHARATFORG,WIPRO,HAVELLS,APLAPOLLO,TMPV,OBEROIRLTY,MARICO,
KEI,SBILIFE,DABUR,TATAPOWER,INDIGO,MFSL,DIXON,SBICARD,SRF,VBL,PFC,GODREJCP,
ASTRAL,UNITDSPR,GMRAIRPORT,IOC,HDFCAMC,TATACONSUM,HINDPETRO,LODHA,GRASIM,
TIINDIA,TORNTPHARM,UPL,HDFCLIFE,CANBK,SIEMENS,CGPOWER,APOLLOHOSP,VEDL,PNB,
FEDERALBNK,POLYCAB,PHOENIXLTD,AUBANK,INDUSINDBK,NAUKRI,ASHOKLEY,DIVISLAB,
NATIONALUM,DRREDDY,CIPLA,JINDALSTEL,POLICYBZR,AMBUJACEM,INDHOTEL,BPCL,
PIDILITIND,IDFCFIRSTB,ICICIGI,BANKBARODA,TMCV,JIOFIN,NMDC,CHOLAFIN,GAIL,TRENT"""

WATCHLIST_DEFAULT = list(dict.fromkeys(
    [s.strip() for s in RAW_STOCKS.replace("\n", "").split(",") if s.strip()]
))

YF_FIX = {"BAJAJ_AUTO": "BAJAJ-AUTO"}
TV_FIX = {"BAJAJ_AUTO": "BAJAJ-AUTO"}

GLOBAL_INSTRUMENTS = [
    ("DXY", "US Dollar Index", "DX-Y.NYB", "TVC:DXY"),
    ("USDINR", "USD / INR", "INR=X", "FX_IDC:USDINR"),
    ("US10Y", "US 10-Yr Treasury Yield", "^TNX", "TVC:US10Y"),
    ("TLT", "20+ Yr Treasury Bond ETF", "TLT", "NASDAQ:TLT"),
    ("XAUUSD", "Gold / USD", "GC=F", "TVC:GOLD"),
    ("XAGUSD", "Silver / USD", "SI=F", "TVC:SILVER"),
    ("SPOTCRUDE", "WTI Crude Oil", "CL=F", "TVC:USOIL"),
    ("COPPER", "Copper", "HG=F", "COMEX:HG1!"),
    ("NATGAS", "Natural Gas", "NG=F", "NYMEX:NG1!"),
    ("US30", "Dow Jones Industrial Avg", "^DJI", "TVC:DJI"),
    ("US500", "S&P 500", "^GSPC", "TVC:SPX"),
    ("000001", "Shanghai Composite (China)", "000001.SS", "SSE:000001"),
    ("JP225", "Nikkei 225 (Japan)", "^N225", "TVC:NI225"),
    ("FTSE100", "FTSE 100 (UK)", "^FTSE", "TVC:UKX"),
]

SECTOR_INDEX_TICKERS = {
    "Nifty Bank": "^NSEBANK", "Nifty IT": "^CNXIT", "Nifty Auto": "^CNXAUTO",
    "Nifty FMCG": "^CNXFMCG", "Nifty Pharma": "^CNXPHARMA", "Nifty Metal": "^CNXMETAL",
    "Nifty Energy": "^CNXENERGY", "Nifty Realty": "^CNXREALTY",
    "Nifty PSU Bank": "^CNXPSUBANK", "Nifty Financial Services": "^CNXFIN",
}

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}

# ============================== PAGE SETUP ==============================
st.set_page_config(page_title="Full Market Dashboard & D&S Scanner", layout="wide",
                    page_icon="📈", initial_sidebar_state="collapsed")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
:root {
  --dh-bg: #F5F7FA; --dh-card: #FFFFFF; --dh-border: #E6E9F0;
  --dh-text: #14151A; --dh-muted: #70758A;
}
html, body, [class^="css"], [class*=" css"] { font-family: 'Inter', sans-serif !important; }
[data-testid="stAppViewContainer"], [data-testid="stHeader"], .main, section.main { background: var(--dh-bg) !important; }
[data-testid="stHeader"] { background: transparent !important; }
h1, h2, h3, h4 { color: var(--dh-text) !important; font-weight: 700 !important; }
[data-testid="stCaptionContainer"] { color: var(--dh-muted) !important; font-size: 12.5px !important; }
div[data-testid="stTabs"] div[role="tablist"] {
  gap: 4px !important; background: var(--dh-card) !important; padding: 6px !important;
  border-radius: 14px !important; border: 1px solid var(--dh-border) !important; overflow-x: auto;
}
div[data-testid="stTabs"] button[role="tab"] {
  border-radius: 10px !important; padding: 9px 16px !important; font-weight: 600 !important;
  font-size: 13.5px !important; color: var(--dh-muted) !important; background: transparent !important;
}
div[data-testid="stTabs"] button[aria-selected="true"] { background: #0B1F3A !important; color: #FFFFFF !important; }
[data-testid="stDataFrame"] { border-radius: 12px !important; overflow: hidden; border: 1px solid var(--dh-border) !important; }
div[data-testid="stMetric"] { background: var(--dh-card); border: 1px solid var(--dh-border); border-radius: 12px; padding: 14px 16px; }
div[data-testid="stMetricValue"] { color: var(--dh-text) !important; font-weight: 800 !important; }
[data-testid="stAlert"] { border-radius: 12px !important; }

/* 🆕 MOBILE-FRIENDLY IMPROVEMENTS */
@media (max-width: 768px) {
    .block-container {padding-left: 0.5rem; padding-right: 0.5rem; padding-top: 0.6rem;}
    div[data-testid="stMetricValue"] {font-size: 1.0rem;}
    h1 {font-size: 1.3rem !important;} h2, h3 {font-size: 1.05rem !important;}
    div[data-testid="stTabs"] button[role="tab"] { padding: 7px 10px !important; font-size: 12px !important; }
    div[data-testid="stDataFrame"] { font-size: 11px !important; }
}
.signal-card {
  background: #fff; border-radius: 10px; padding: 10px 12px; margin-bottom: 8px;
  border-left: 4px solid #ccc; box-shadow: 0 1px 3px rgba(0,0,0,0.08);
}
.signal-card.demand { border-left-color: #0a7d2f; background: #f2fbf2; }
.signal-card.supply { border-left-color: #c0392b; background: #fdf2f2; }
.signal-card.hq { border-left-color: #ffb400; background: #fff8e6; }
.signal-title { font-weight: 700; font-size: 13.5px; color:#14151A; }
.signal-sub { font-size: 12px; color: #666; margin-top: 2px; }
.sticky-bar {
  position: sticky; top: 0; z-index: 999; background: #0B1F3A; color: white;
  padding: 8px 14px; border-radius: 8px; margin-bottom: 10px; font-size: 12.5px;
  display: flex; justify-content: space-between; flex-wrap: wrap; gap: 6px;
}
</style>
""", unsafe_allow_html=True)


# ============================== GENERAL HELPERS ==============================
def now_ist():
    return datetime.now(IST)

def is_market_hours():
    t = now_ist().time()
    return MARKET_OPEN <= t <= MARKET_CLOSE and now_ist().weekday() < 5

def tv_link(symbol):
    return f"https://www.tradingview.com/chart/?symbol={urllib.parse.quote(symbol)}"

def tv_symbol_for_stock(stock):
    return f"NSE:{TV_FIX.get(stock, stock)}"

def yf_ticker_for_stock(stock):
    return f"{YF_FIX.get(stock, stock)}.NS"

def _parse_pct(val):
    if val is None: return None
    if isinstance(val, (int, float)): return float(val)
    s = str(val).strip()
    if s in ("", "—", "-", "None", "nan"): return None
    import re
    m = re.search(r"\(([-+]?\d+\.?\d*)%\)", s)
    if m:
        try: return float(m.group(1))
        except Exception: pass
    s = s.replace("%", "").replace("+", "").replace("▲", "").replace("▼", "").replace("●", "").strip()
    try: return float(s)
    except Exception: return None

def pct_bg_style(val):
    v = _parse_pct(val)
    if v is None: return ""
    if v > 0: return f"background-color:{COLOR_POS_BG}; color:{COLOR_POS_TEXT}; font-weight:600;"
    if v < 0: return f"background-color:{COLOR_NEG_BG}; color:{COLOR_NEG_TEXT}; font-weight:600;"
    return f"color:{COLOR_FLAT_TEXT};"

def _styler_apply_map(styler, fn, subset):
    if hasattr(styler, "map"):
        try: return styler.map(fn, subset=subset)
        except Exception: pass
    return styler.applymap(fn, subset=subset)

def style_pct_columns(obj, cols):
    if isinstance(obj, pd.DataFrame): styler, available_cols = obj.style, obj.columns
    else: styler, available_cols = obj, obj.data.columns
    valid_cols = [c for c in cols if c in available_cols]
    if not valid_cols: return styler
    return _styler_apply_map(styler, pct_bg_style, valid_cols)

def fmt_change(chg, pct):
    if chg is None or pct is None: return "—"
    arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "●")
    return f"{chg:+.2f} ({pct:+.2f}%) {arrow}"


# ============================== TIMEFRAMES ==============================
# 🆕 2 Hours और 6 Hours टाइमफ्रेम जोड़े गए
TIMEFRAMES = {
    "3 Min":   {"interval": "1m",  "period": "5d",  "resample": "3min",  "intraday": True},
    "5 Min":   {"interval": "5m",  "period": "5d",  "resample": None,    "intraday": True},
    "15 Min":  {"interval": "5m",  "period": "5d",  "resample": "15min", "intraday": True},
    "30 Min":  {"interval": "15m", "period": "1mo", "resample": "30min", "intraday": True},
    "1 Hour":  {"interval": "60m", "period": "1mo", "resample": None,    "intraday": True},
    "2 Hours": {"interval": "60m", "period": "2mo", "resample": "120min", "intraday": True},  # 🆕
    "4 Hours": {"interval": "60m", "period": "3mo", "resample": "240min", "intraday": True},
    "6 Hours": {"interval": "60m", "period": "3mo", "resample": "360min", "intraday": True},  # 🆕
    "Daily":   {"interval": "1d",  "period": "6mo", "resample": None,     "intraday": False},
}

# 🆕 नए टाइमफ्रेम के साथ अपडेटेड
TF_MINUTES = {"3 Min": 3, "5 Min": 5, "15 Min": 15, "30 Min": 30, "1 Hour": 60,
              "2 Hours": 120, "4 Hours": 240, "6 Hours": 360, "Daily": 1440}

def _build_lower_tf_map() -> Dict[str, Optional[str]]:
    ordered = sorted(TF_MINUTES.items(), key=lambda kv: kv[1])
    mapping: Dict[str, Optional[str]] = {}
    for pos, (tf_name, _) in enumerate(ordered):
        mapping[tf_name] = ordered[pos - 1][0] if pos > 0 else None
    return mapping

LOWER_TF_MAP: Dict[str, Optional[str]] = _build_lower_tf_map()


def calc_ema_np(arr: np.ndarray, span: int) -> np.ndarray:
    alpha = 2.0 / (span + 1.0)
    res = np.empty_like(arr)
    res[0] = arr[0]
    one_minus_alpha = 1.0 - alpha
    for i in range(1, len(arr)):
        res[i] = alpha * arr[i] + one_minus_alpha * res[i - 1]
    return res

def check_ema_cross_generic(close: np.ndarray, fast: int = 20, slow: int = 50, label: str = ""):
    if len(close) < slow:
        return None
    ema_fast = calc_ema_np(close, fast)
    ema_slow = calc_ema_np(close, slow)
    tag = label or f"EMA {fast}/{slow}"
    if ema_fast[-2] <= ema_slow[-2] and ema_fast[-1] > ema_slow[-1]:
        return f"🟢 {tag} UP"
    if ema_fast[-2] >= ema_slow[-2] and ema_fast[-1] < ema_slow[-1]:
        return f"🔴 {tag} DOWN"
    return None

def check_ema_cross_fast(close: np.ndarray):
    return check_ema_cross_generic(close, 20, 50, "EMA20/50")

def check_ema_cross_3_5(close: np.ndarray):
    return check_ema_cross_generic(close, 3, 5, "EMA3/5")

def check_volume_spike_fast(vol: np.ndarray, mult=2.0):
    if len(vol) < 21: return None
    avg_vol = np.mean(vol[-21:-1])
    curr_vol = vol[-1]
    if avg_vol > 0 and (curr_vol / avg_vol) >= mult:
        return f"⚡ Vol {curr_vol / avg_vol:.1f}x"
    return None

def check_rsi_fast(close: np.ndarray, period=14):
    if len(close) < period + 1: return None
    diffs = np.diff(close)
    gains = np.where(diffs > 0, diffs, 0.0)
    losses = np.where(diffs < 0, -diffs, 0.0)
    if len(gains) < period: return None
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    if avg_loss == 0:
        rsi = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))
    if rsi >= 70: return f"🔥 RSI OB ({rsi:.0f})"
    if rsi <= 30: return f"🧊 RSI OS ({rsi:.0f})"
    return None

def check_ds_zones(df: pd.DataFrame, symbol: str, tf_key: str,
                    lower_tf_df: Optional[pd.DataFrame] = None, use_mtf: bool = False):
    """🆕 अब incremental/cached scanner इस्तेमाल करता है — हर refresh पर पूरा इतिहास दोबारा scan नहीं होता।
    Return: (signal_text, is_hq_bool, best_zone_detail_dict_or_None)"""
    zones = scan_institutional_ds_zones_incremental(df, symbol, tf_key, lower_tf_df=lower_tf_df, use_mtf=use_mtf)
    if not zones:
        return None, False, None
    current_price = df['Close'].iloc[-1]
    active_zones = [z for z in zones if z.state in ("Fresh", "Retest")]

    signals = []
    is_hq_signal = False
    best_zone_detail = None
    best_priority = -1

    for z in active_zones:
        mtf_tag = "🔬MTF✓ " if (use_mtf and z.mtf_confirmed) else ""
        retest_tag = f"(Retest#{z.touch_count}) " if z.state == "Retest" else ""
        if z.is_demand:
            diff_pct = (current_price - z.prox_val) / z.prox_val
            if MIN_PROXIMITY_PCT <= diff_pct <= MAX_PROXIMITY_PCT:
                hq_tag = "★ HQ " if z.is_hq else ""
                if z.is_hq: is_hq_signal = True
                signals.append(f"🟢 DEMAND ZONE ({hq_tag}{mtf_tag}{retest_tag}Entry: {z.prox_val:.2f}, SL: {z.sl_val:.2f}, TP: {z.tp_val:.2f}, {diff_pct*100:.2f}% away)")
                priority = 2 if z.is_hq else 1
                if priority > best_priority:
                    best_priority = priority
                    best_zone_detail = {"entry": z.prox_val, "sl": z.sl_val, "tp": z.tp_val, "is_demand": True, "is_hq": z.is_hq}
        else:
            diff_pct = (z.prox_val - current_price) / z.prox_val
            if MIN_PROXIMITY_PCT <= diff_pct <= MAX_PROXIMITY_PCT:
                hq_tag = "★ HQ " if z.is_hq else ""
                if z.is_hq: is_hq_signal = True
                signals.append(f"🔴 SUPPLY ZONE ({hq_tag}{mtf_tag}{retest_tag}Entry: {z.prox_val:.2f}, SL: {z.sl_val:.2f}, TP: {z.tp_val:.2f}, {diff_pct*100:.2f}% away)")
                priority = 2 if z.is_hq else 1
                if priority > best_priority:
                    best_priority = priority
                    best_zone_detail = {"entry": z.prox_val, "sl": z.sl_val, "tp": z.tp_val, "is_demand": False, "is_hq": z.is_hq}

    if signals:
        return " | ".join(signals), is_hq_signal, best_zone_detail
    return None, False, None


# ==========================================
# 🆕 3c. PERSISTENT DAILY DATA STORE (News/Events/FII-DII)
#      — दिनभर जमा होता रहता है, रात 11 बजे (IST) ऑटो-क्लियर
# ==========================================
DAILY_CLEAR_HOUR_IST = 23  # रात 11 बजे — इस समय के बाद पूरा दिनभर का जमा डेटा साफ हो जाएगा

def _empty_daily_store() -> Dict[str, Any]:
    return {
        "market_news": [],        # पूरे मार्केट की headlines (dedup by link)
        "stock_news": {},         # {stock: [news_items,...]}
        "corp_announcements": [], # NSE corporate announcements (dedup)
        "fii_dii_log": [],        # [{time, fii_net, dii_net, source}, ...] — दिनभर का trail
        "seen_news_links": set(), # dedup helper
    }

def get_daily_store() -> Dict[str, Any]:
    """सेशन-लेवल डेली डेटा स्टोर — पूरे दिन में मिलने वाली news/events/FII-DII डेटा यहां
    जमा होते रहते हैं (ताकि बार-बार भारी fetch न करना पड़े और 'लेटेस्ट न्यूज' सर्च हल्का रहे),
    और रात 11 बजे (नीचे maybe_clear_all_daily_data) अपने-आप खाली हो जाते हैं।"""
    if "daily_store" not in st.session_state:
        st.session_state.daily_store = _empty_daily_store()
    return st.session_state.daily_store

def maybe_clear_all_daily_data():
    """🆕 रात 11 बजे (IST) के बाद — दिन में पहली बार आने पर — सारा जमा डेटा (News, Corporate
    Announcements, FII/DII trail, D&S Zone Cache, Alerts, Signal Map) रीसेट कर देता है ताकि
    अगले दिन सुबह से बिल्कुल ताज़ा (fresh) शुरुआत हो और पुराने दिन का institutional context
    नए दिन के signals को गलत तरीके से प्रभावित न करे।"""
    if "daily_clear_date" not in st.session_state:
        st.session_state.daily_clear_date = None
    today = now_ist().date()
    if now_ist().time() >= dtime(DAILY_CLEAR_HOUR_IST, 0):
        if st.session_state.daily_clear_date != today:
            st.session_state.daily_store = _empty_daily_store()
            st.session_state.ds_zone_cache = {}
            st.session_state.alerts = []
            st.session_state.all_tf_signals_map = {}
            st.cache_data.clear()
            st.session_state.daily_clear_date = today

def update_and_get_daily_market_news(max_items: int = 10) -> List[Dict[str, Any]]:
    """नई news नेटवर्क से केवल हर ~10 मिनट में एक बार आती है (नीचे fetch_market_wide_news
    पहले से @st.cache_data(ttl=600) से cached है) — यहां सिर्फ उसे दिनभर के संचित स्टोर में
    dedup करके जोड़ा जाता है, इसलिए 'latest news' ढूंढना हमेशा हल्का रहता है।"""
    store = get_daily_store()
    fresh_items = fetch_market_wide_news(max_items=max_items)
    for item in fresh_items:
        link = item.get("link")
        if link and link not in store["seen_news_links"]:
            store["seen_news_links"].add(link)
            store["market_news"].append(item)
    store["market_news"].sort(
        key=lambda x: x.get("published") or datetime.min.replace(tzinfo=timezone.utc), reverse=True
    )
    return store["market_news"]

def update_and_get_stock_news(stock: str, max_items: int = 6) -> List[Dict[str, Any]]:
    """किसी एक स्टॉक की news भी दिनभर संचित होती है — पुरानी महत्वपूर्ण खबर भी evidence में बनी रहती है।"""
    store = get_daily_store()
    fresh_items = fetch_stock_news_items_full(stock, max_items=max_items)
    bucket = store["stock_news"].setdefault(stock, [])
    existing_links = {it.get("link") for it in bucket}
    for item in fresh_items:
        link = item.get("link")
        if link and link not in existing_links:
            bucket.append(item)
            existing_links.add(link)
    bucket.sort(key=lambda x: x.get("published") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return bucket

def update_and_get_corp_announcements() -> List[Dict[str, Any]]:
    store = get_daily_store()
    fresh = fetch_nse_corporate_announcements()
    existing_keys = {(a.get("symbol"), a.get("subject"), str(a.get("time"))) for a in store["corp_announcements"]}
    for a in fresh:
        k = (a.get("symbol"), a.get("subject"), str(a.get("time")))
        if k not in existing_keys:
            store["corp_announcements"].append(a)
            existing_keys.add(k)
    return store["corp_announcements"]

def update_and_get_fii_dii_log() -> Tuple[Optional[pd.DataFrame], Optional[str], List[Dict[str, Any]]]:
    """FII/DII का हर बदलाव समय के साथ लॉग होता है ताकि दिनभर का trend evidence के तौर पर दिखे।"""
    store = get_daily_store()
    fii_df, source = fetch_fii_dii()
    if fii_df is not None:
        try:
            net_col = [c for c in fii_df.columns if "net" in c.lower()][0]
            cat_col = [c for c in fii_df.columns if "cat" in c.lower()][0]
            fii_net = dii_net = None
            for _, r in fii_df.iterrows():
                if "FII" in str(r[cat_col]).upper(): fii_net = float(r[net_col])
                elif "DII" in str(r[cat_col]).upper(): dii_net = float(r[net_col])
            ts = now_ist().strftime("%H:%M")
            last_entry = store["fii_dii_log"][-1] if store["fii_dii_log"] else None
            if last_entry is None or last_entry.get("fii_net") != fii_net or last_entry.get("dii_net") != dii_net:
                store["fii_dii_log"].append({"time": ts, "fii_net": fii_net, "dii_net": dii_net, "source": source})
        except Exception:
            pass
    return fii_df, source, store["fii_dii_log"]

def collect_daily_market_mentions_evidence(stock: str) -> List["EvidenceItem"]:
    """🆕 दिनभर जमा हुई मार्केट-वाइड news + corporate announcements में स्टॉक के नाम का
    ज़िक्र ढूंढकर evidence-based bullish/bearish असर निकालता है — बिना किसी नए network-call के
    (डेटा पहले से daily_store में मौजूद है, इसलिए ज़ीरो अतिरिक्त लोड)।"""
    store = get_daily_store()
    evid: List[EvidenceItem] = []
    stock_lower = stock.lower()
    for item in store["market_news"]:
        title = item.get("title", "")
        if stock_lower in title.lower():
            res = score_text_sentiment(title)
            if res["direction"] != "neutral":
                conf = _freshness_confidence(item.get("published"))
                evid.append(EvidenceItem("संचित Market News (आज)", f"'{title[:90]}'",
                                          res["direction"], W_NEWS_KEYWORD, conf, item.get("published_str", "")))
    for ann in store["corp_announcements"]:
        if str(ann.get("symbol", "")).strip().upper() != stock.upper():
            continue
        subj = ann.get("subject", "")
        if not subj:
            continue
        res = score_text_sentiment(subj)
        if res["direction"] != "neutral":
            evid.append(EvidenceItem("संचित Corporate Announcement (आज)", f"'{subj[:90]}'",
                                      res["direction"], W_CORP_ANNOUNCE, 0.9, str(ann.get("time", ""))))
    return evid


# ==========================================
# 4. AI EVIDENCE-BASED BUY/SELL HYPOTHESIS ENGINE
# ==========================================
Direction = str  # "bullish" | "bearish" | "neutral"

@dataclass
class EvidenceItem:
    source: str
    detail: str
    direction: Direction
    weight: float
    confidence: float = 1.0
    timestamp: str = ""

    @property
    def signed_score(self) -> float:
        sign = 1.0 if self.direction == "bullish" else (-1.0 if self.direction == "bearish" else 0.0)
        return sign * self.weight * self.confidence


@dataclass
class Hypothesis:
    stock: str
    price: Optional[float]
    label: str
    score: float
    confidence_label: str
    bullish_count: int
    bearish_count: int
    evidence: List[EvidenceItem] = field(default_factory=list)
    suggested_entry: Optional[float] = None
    suggested_sl: Optional[float] = None
    suggested_tp: Optional[float] = None
    zone_source_tf: Optional[str] = None
    generated_at: str = field(default_factory=lambda: datetime.now(IST).strftime("%H:%M:%S"))


W_DS_HQ_ZONE, W_DS_NORMAL_ZONE, W_DS_MTF_BONUS = 2.2, 1.3, 0.6
W_EMA_20_50, W_EMA_3_5, W_RSI_EXTREME, W_VOL_SPIKE, W_MTF_ALIGN_BONUS = 1.0, 0.4, 0.5, 0.6, 1.0
W_NEWS_KEYWORD, W_CORP_ANNOUNCE, NEWS_STALE_HOURS = 0.8, 0.6, 24
W_MACRO_STOCK_SPEC, W_MACRO_BROAD, W_SECTOR_MOVE, W_FII_DII, W_OPTIONS_PCR = 0.7, 0.3, 0.5, 0.3, 0.4
MIN_EVIDENCE_FOR_STRONG, MIN_EVIDENCE_FOR_ANY_CALL = 4, 2
SCORE_STRONG, SCORE_MODERATE = 3.0, 1.3

STOCK_SECTOR_MAP: Dict[str, str] = {
    "TCS": "Nifty IT", "HCLTECH": "Nifty IT", "INFY": "Nifty IT", "WIPRO": "Nifty IT",
    "TECHM": "Nifty IT", "PERSISTENT": "Nifty IT", "COFORGE": "Nifty IT",
    "M&M": "Nifty Auto", "BAJAJ_AUTO": "Nifty Auto", "MARUTI": "Nifty Auto",
    "TATAMOTORS": "Nifty Auto", "EICHERMOT": "Nifty Auto", "TVSMOTOR": "Nifty Auto",
    "HEROMOTOCO": "Nifty Auto", "MOTHERSON": "Nifty Auto", "TIINDIA": "Nifty Auto",
    "ASHOKLEY": "Nifty Auto", "BHARATFORG": "Nifty Auto", "TMPV": "Nifty Auto", "TMCV": "Nifty Auto",
    "HDFCBANK": "Nifty Bank", "ICICIBANK": "Nifty Bank", "KOTAKBANK": "Nifty Bank",
    "AXISBANK": "Nifty Bank", "INDUSINDBK": "Nifty Bank", "FEDERALBNK": "Nifty Bank",
    "IDFCFIRSTB": "Nifty Bank", "AUBANK": "Nifty Bank",
    "SBIN": "Nifty PSU Bank", "PNB": "Nifty PSU Bank", "CANBK": "Nifty PSU Bank", "BANKBARODA": "Nifty PSU Bank",
    "HINDUNILVR": "Nifty FMCG", "NESTLEIND": "Nifty FMCG", "ITC": "Nifty FMCG",
    "BRITANNIA": "Nifty FMCG", "DABUR": "Nifty FMCG", "MARICO": "Nifty FMCG",
    "GODREJCP": "Nifty FMCG", "TATACONSUM": "Nifty FMCG", "VBL": "Nifty FMCG", "UNITDSPR": "Nifty FMCG",
    "SUNPHARMA": "Nifty Pharma", "AUROPHARMA": "Nifty Pharma", "LUPIN": "Nifty Pharma",
    "CIPLA": "Nifty Pharma", "DRREDDY": "Nifty Pharma", "DIVISLAB": "Nifty Pharma",
    "TORNTPHARM": "Nifty Pharma", "LAURUSLABS": "Nifty Pharma",
    "TATASTEEL": "Nifty Metal", "JSWSTEEL": "Nifty Metal", "HINDALCO": "Nifty Metal",
    "VEDL": "Nifty Metal", "NATIONALUM": "Nifty Metal", "JINDALSTEL": "Nifty Metal",
    "COALINDIA": "Nifty Metal", "NMDC": "Nifty Metal",
    "RELIANCE": "Nifty Energy", "ONGC": "Nifty Energy", "BPCL": "Nifty Energy",
    "IOC": "Nifty Energy", "HINDPETRO": "Nifty Energy", "OIL": "Nifty Energy",
    "GAIL": "Nifty Energy", "NTPC": "Nifty Energy", "POWERGRID": "Nifty Energy",
    "TATAPOWER": "Nifty Energy", "JSWENERGY": "Nifty Energy", "ADANIENT": "Nifty Energy",
    "OBEROIRLTY": "Nifty Realty", "LODHA": "Nifty Realty", "PHOENIXLTD": "Nifty Realty", "GMRAIRPORT": "Nifty Realty",
    "BAJFINANCE": "Nifty Financial Services", "SHRIRAMFIN": "Nifty Financial Services",
    "MUTHOOTFIN": "Nifty Financial Services", "SBILIFE": "Nifty Financial Services",
    "HDFCLIFE": "Nifty Financial Services", "SBICARD": "Nifty Financial Services",
    "MFSL": "Nifty Financial Services", "CHOLAFIN": "Nifty Financial Services",
    "ICICIGI": "Nifty Financial Services", "HDFCAMC": "Nifty Financial Services",
    "PFC": "Nifty Financial Services", "RECLTD": "Nifty Financial Services",
    "JIOFIN": "Nifty Financial Services", "POLICYBZR": "Nifty Financial Services",
}

IT_STOCKS = {"TCS", "HCLTECH", "INFY", "WIPRO", "TECHM", "PERSISTENT", "COFORGE"}
OMC_STOCKS = {"BPCL", "IOC", "HINDPETRO"}
UPSTREAM_OIL_STOCKS = {"ONGC", "OIL"}
AVIATION_PAINT_STOCKS = {"INDIGO", "ASIANPAINT"}
BANK_NBFC_STOCKS = {"HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK", "BAJFINANCE",
                     "INDUSINDBK", "FEDERALBNK", "PNB", "CANBK", "BANKBARODA", "IDFCFIRSTB", "AUBANK"}
METAL_STOCKS = {"HINDALCO", "VEDL", "NATIONALUM", "TATASTEEL", "JSWSTEEL", "JINDALSTEL"}
NIFTY_HEAVYWEIGHTS = {"HDFCBANK", "RELIANCE", "ICICIBANK", "INFY", "ITC", "TCS",
                       "LT", "AXISBANK", "SBIN", "BHARTIARTL", "KOTAKBANK"}

POSITIVE_KEYWORDS = [
    "record profit", "profit jumps", "profit surges", "profit rises", "net profit up",
    "order win", "wins order", "bags order", "bags contract", "big order",
    "upgrade", "outperform", "buy rating", "rating upgrade", "target price raised",
    "buyback", "special dividend", "interim dividend", "bonus issue", "stock split",
    "capacity expansion", "new plant", "new facility", "commissions plant",
    "strong guidance", "beats estimates", "beats street estimates", "robust growth",
    "market share gain", "stake acquisition", "acquires", "acquisition completed",
    "partnership", "strategic tie-up", "collaboration", "joint venture",
    "product launch", "regulatory approval", "usfda approval", "patent granted",
    "block deal buy", "bulk deal buy", "insider buying", "promoter increases stake",
    "qip fully subscribed", "strong q1", "strong q2", "strong q3", "strong q4",
    "raises guidance", "record revenue", "highest ever",
]
NEGATIVE_KEYWORDS = [
    "profit falls", "profit declines", "profit drops", "net loss", "loss widens",
    "downgrade", "rating cut", "sell rating", "target price cut",
    "sebi probe", "sebi action", "sebi bars", "fraud", "scam", "accounting irregularities",
    "resignation", "ceo quits", "cfo resigns", "auditor resigns", "independent director quits",
    "raid", "cbi raid", "ed raid", "income tax raid", "penalty imposed", "fine imposed",
    "strike", "labour unrest", "plant shutdown", "production halted", "recall",
    "debt default", "default on payment", "credit rating downgrade", "rating downgraded",
    "block deal sell", "bulk deal sell", "insider selling", "promoter pledge",
    "promoter stake sale", "stake sale by promoter", "weak guidance", "misses estimates",
    "margin pressure", "cost pressure", "regulatory action", "show cause notice",
    "goes into loss", "warns of", "profit warning",
]

def score_text_sentiment(text: str) -> Dict[str, Any]:
    t = text.lower()
    pos_hits = [kw for kw in POSITIVE_KEYWORDS if kw in t]
    neg_hits = [kw for kw in NEGATIVE_KEYWORDS if kw in t]
    if pos_hits and not neg_hits:
        return {"direction": "bullish", "matched": pos_hits}
    if neg_hits and not pos_hits:
        return {"direction": "bearish", "matched": neg_hits}
    if pos_hits and neg_hits:
        return {"direction": "neutral", "matched": pos_hits + neg_hits}
    return {"direction": "neutral", "matched": []}

def _freshness_confidence(published_dt: Optional[datetime]) -> float:
    if published_dt is None:
        return 0.5
    age_hours = (datetime.now(timezone.utc) - published_dt).total_seconds() / 3600.0
    if age_hours <= 2: return 1.0
    if age_hours <= 6: return 0.85
    if age_hours <= NEWS_STALE_HOURS: return 0.6
    return 0.3

def collect_technical_evidence(stock: str, tf_signals: Dict[str, Dict[str, Any]]) -> List[EvidenceItem]:
    evid: List[EvidenceItem] = []
    votes = {"bullish": 0, "bearish": 0}
    for tf_key, sig in tf_signals.items():
        if not sig: continue
        ds_sig = sig.get("ds_signal")
        if ds_sig:
            is_demand = "DEMAND" in ds_sig
            is_hq = sig.get("is_hq_zone", False)
            mtf_ok = sig.get("mtf_confirmed", False)
            w = W_DS_HQ_ZONE if is_hq else W_DS_NORMAL_ZONE
            direction = "bullish" if is_demand else "bearish"
            evid.append(EvidenceItem("Technical - D&S Zone", f"[{tf_key}] {ds_sig}",
                                      direction, w, min(1.0 + (0.15 if mtf_ok else 0.0), 1.3),
                                      sig.get("bar_time", "")))
            votes[direction] += 1
            if mtf_ok:
                evid.append(EvidenceItem("Technical - MTF Validation",
                                          f"[{tf_key}] निचले टाइमफ्रेम पर zone confirm हुआ",
                                          direction, W_DS_MTF_BONUS, 1.0, sig.get("bar_time", "")))
        ema_sig = sig.get("ema_cross")
        if ema_sig:
            direction = "bullish" if "UP" in ema_sig else "bearish"
            evid.append(EvidenceItem("Technical - EMA 20/50", f"[{tf_key}] {ema_sig}",
                                      direction, W_EMA_20_50, 1.0, sig.get("bar_time", "")))
            votes[direction] += 1
        ema35_sig = sig.get("ema_cross_35")
        if ema35_sig:
            direction = "bullish" if "UP" in ema35_sig else "bearish"
            evid.append(EvidenceItem("Technical - EMA 3/5 (Scalp)",
                                      f"[{tf_key}] {ema35_sig} — सिर्फ momentum trigger",
                                      direction, W_EMA_3_5, 0.8, sig.get("bar_time", "")))
        rsi_sig = sig.get("rsi_signal")
        if rsi_sig:
            direction = "bearish" if "OB" in rsi_sig else "bullish"
            evid.append(EvidenceItem("Technical - RSI(14)", f"[{tf_key}] {rsi_sig}",
                                      direction, W_RSI_EXTREME, 0.7, sig.get("bar_time", "")))
        vol_sig = sig.get("vol_spike")
        if vol_sig and sig.get("candle_bullish") is not None:
            direction = "bullish" if sig["candle_bullish"] else "bearish"
            evid.append(EvidenceItem("Technical - Volume Spike",
                                      f"[{tf_key}] {vol_sig} + {'bullish' if sig['candle_bullish'] else 'bearish'} candle",
                                      direction, W_VOL_SPIKE, 0.9, sig.get("bar_time", "")))
    if votes["bullish"] >= 2 and votes["bearish"] == 0:
        evid.append(EvidenceItem("Technical - MTF Confluence",
                                  f"{votes['bullish']} अलग टाइमफ्रेम पर बुलिश सिग्नल्स सहमत", "bullish", W_MTF_ALIGN_BONUS, 1.0))
    elif votes["bearish"] >= 2 and votes["bullish"] == 0:
        evid.append(EvidenceItem("Technical - MTF Confluence",
                                  f"{votes['bearish']} अलग टाइमफ्रेम पर बेयरिश सिग्नल्स सहमत", "bearish", W_MTF_ALIGN_BONUS, 1.0))
    return evid

def collect_news_evidence(stock: str, news_items: List[Dict[str, Any]],
                           corp_announcements: List[Dict[str, Any]]) -> List[EvidenceItem]:
    evid: List[EvidenceItem] = []
    for item in news_items[:8]:
        title = item.get("title", "")
        if not title: continue
        result = score_text_sentiment(title)
        if result["direction"] == "neutral": continue
        conf = _freshness_confidence(item.get("published"))
        evid.append(EvidenceItem("News", f"'{title[:90]}' → matched: {', '.join(result['matched'][:3])}",
                                  result["direction"], W_NEWS_KEYWORD, conf, item.get("published_str", "")))
    for ann in corp_announcements:
        if str(ann.get("symbol", "")).strip().upper() != stock.upper(): continue
        subj = ann.get("subject", "")
        if not subj: continue
        result = score_text_sentiment(subj)
        if result["direction"] == "neutral": continue
        evid.append(EvidenceItem("Corporate Announcement (NSE)",
                                  f"'{subj[:90]}' → matched: {', '.join(result['matched'][:3])}",
                                  result["direction"], W_CORP_ANNOUNCE, 0.9, str(ann.get("time", ""))))
    return evid

def collect_macro_evidence(stock: str, macro_quotes: Dict[str, Dict[str, Any]]) -> List[EvidenceItem]:
    evid: List[EvidenceItem] = []
    def pct_of(yft):
        q = macro_quotes.get(yft)
        return q["pct"] if q else None
    usdinr_pct, crude_pct = pct_of("INR=X"), pct_of("CL=F")
    us10y_pct, sp500_pct, copper_pct = pct_of("^TNX"), pct_of("^GSPC"), pct_of("HG=F")

    if stock in IT_STOCKS and usdinr_pct is not None and abs(usdinr_pct) >= 0.15:
        direction = "bullish" if usdinr_pct > 0 else "bearish"
        evid.append(EvidenceItem("Macro - USD/INR", f"USD/INR {usdinr_pct:+.2f}% — IT export revenue पर असर",
                                  direction, W_MACRO_STOCK_SPEC, 1.0))
    if stock in OMC_STOCKS and crude_pct is not None and abs(crude_pct) >= 0.5:
        direction = "bearish" if crude_pct > 0 else "bullish"
        evid.append(EvidenceItem("Macro - Crude Oil", f"WTI Crude {crude_pct:+.2f}% — OMC इनपुट कॉस्ट पर असर",
                                  direction, W_MACRO_STOCK_SPEC, 1.0))
    if stock in UPSTREAM_OIL_STOCKS and crude_pct is not None and abs(crude_pct) >= 0.5:
        direction = "bullish" if crude_pct > 0 else "bearish"
        evid.append(EvidenceItem("Macro - Crude Oil", f"WTI Crude {crude_pct:+.2f}% — upstream realisation पर असर",
                                  direction, W_MACRO_STOCK_SPEC, 1.0))
    if stock in AVIATION_PAINT_STOCKS and crude_pct is not None and abs(crude_pct) >= 0.5:
        direction = "bearish" if crude_pct > 0 else "bullish"
        evid.append(EvidenceItem("Macro - Crude Oil", f"WTI Crude {crude_pct:+.2f}% — ATF/इनपुट कॉस्ट पर असर",
                                  direction, W_MACRO_STOCK_SPEC, 1.0))
    if stock in BANK_NBFC_STOCKS and us10y_pct is not None and abs(us10y_pct) >= 1.0:
        direction = "bearish" if us10y_pct > 0 else "bullish"
        evid.append(EvidenceItem("Macro - US 10Y Yield", f"US 10Y yield {us10y_pct:+.2f}% — FII flow पर असर",
                                  direction, W_MACRO_STOCK_SPEC, 0.8))
    if stock in METAL_STOCKS and copper_pct is not None and abs(copper_pct) >= 0.5:
        direction = "bullish" if copper_pct > 0 else "bearish"
        evid.append(EvidenceItem("Macro - Copper", f"Copper {copper_pct:+.2f}% — base-metal sentiment",
                                  direction, W_MACRO_STOCK_SPEC, 0.85))
    if sp500_pct is not None and abs(sp500_pct) >= 0.4:
        direction = "bullish" if sp500_pct > 0 else "bearish"
        evid.append(EvidenceItem("Macro - Global (S&P500)", f"US S&P500 {sp500_pct:+.2f}% — broad global sentiment",
                                  direction, W_MACRO_BROAD, 0.7))
    return evid

def collect_sector_evidence(stock: str, sector_quotes: Dict[str, Optional[float]]) -> List[EvidenceItem]:
    sector_name = STOCK_SECTOR_MAP.get(stock)
    if not sector_name: return []
    pct = sector_quotes.get(sector_name)
    if pct is None or abs(pct) < 0.3: return []
    direction = "bullish" if pct > 0 else "bearish"
    return [EvidenceItem("Sector", f"{sector_name} इंडेक्स {pct:+.2f}% — सेक्टर-वाइड मोमेंटम", direction, W_SECTOR_MOVE, 0.85)]

def collect_fii_dii_evidence(fii_net, dii_net) -> List[EvidenceItem]:
    evid = []
    if fii_net is not None and abs(fii_net) >= 300:
        direction = "bullish" if fii_net > 0 else "bearish"
        evid.append(EvidenceItem("FII/DII Flow", f"FII Net (कल EOD): ₹{fii_net:+.0f} Cr", direction, W_FII_DII, 0.7))
    if dii_net is not None and abs(dii_net) >= 300:
        direction = "bullish" if dii_net > 0 else "bearish"
        evid.append(EvidenceItem("FII/DII Flow", f"DII Net (कल EOD): ₹{dii_net:+.0f} Cr", direction, W_FII_DII, 0.7))
    return evid

def collect_options_evidence(stock: str, pcr: Optional[float]) -> List[EvidenceItem]:
    if stock not in NIFTY_HEAVYWEIGHTS or pcr is None: return []
    if pcr > 1.15:
        return [EvidenceItem("Options - Nifty PCR", f"Nifty PCR {pcr} (>1.15) — put-heavy bullish bias",
                              "bullish", W_OPTIONS_PCR, 0.6)]
    if pcr < 0.80:
        return [EvidenceItem("Options - Nifty PCR", f"Nifty PCR {pcr} (<0.80) — call-heavy bearish bias",
                              "bearish", W_OPTIONS_PCR, 0.6)]
    return []

def classify_score(score: float, evidence_count: int) -> str:
    if evidence_count < MIN_EVIDENCE_FOR_ANY_CALL: return "NEUTRAL"
    if score >= SCORE_STRONG and evidence_count >= MIN_EVIDENCE_FOR_STRONG: return "STRONG BUY"
    if score >= SCORE_MODERATE: return "BUY"
    if score <= -SCORE_STRONG and evidence_count >= MIN_EVIDENCE_FOR_STRONG: return "STRONG SELL"
    if score <= -SCORE_MODERATE: return "SELL"
    return "NEUTRAL"

def confidence_label(bullish: int, bearish: int) -> str:
    total = bullish + bearish
    if total == 0: return "Low"
    ratio = max(bullish, bearish) / total
    if total >= 5 and ratio >= 0.75: return "High"
    if total >= 3 and ratio >= 0.6: return "Medium"
    return "Low"

def build_hypothesis(stock, price, tf_signals, news_items=None, corp_announcements=None,
                      macro_quotes=None, sector_quotes=None, fii_net=None, dii_net=None,
                      nifty_pcr=None) -> Hypothesis:
    all_evidence: List[EvidenceItem] = []
    all_evidence += collect_technical_evidence(stock, tf_signals or {})
    all_evidence += collect_news_evidence(stock, news_items or [], corp_announcements or [])
    all_evidence += collect_daily_market_mentions_evidence(stock)  # 🆕 दिनभर जमा संचित News/Announcements
    all_evidence += collect_macro_evidence(stock, macro_quotes or {})
    all_evidence += collect_sector_evidence(stock, sector_quotes or {})
    all_evidence += collect_fii_dii_evidence(fii_net, dii_net)
    all_evidence += collect_options_evidence(stock, nifty_pcr)

    total_score = sum(e.signed_score for e in all_evidence)
    bullish_count = sum(1 for e in all_evidence if e.direction == "bullish")
    bearish_count = sum(1 for e in all_evidence if e.direction == "bearish")
    evidence_count = bullish_count + bearish_count

    label = classify_score(total_score, evidence_count)
    conf = confidence_label(bullish_count, bearish_count)

    entry = sl = tp = zone_tf = None
    for tf_key, sig in (tf_signals or {}).items():
        if sig and sig.get("ds_signal") and sig.get("zone_entry") is not None:
            if sig.get("is_hq_zone") or entry is None:
                entry, sl, tp, zone_tf = sig.get("zone_entry"), sig.get("zone_sl"), sig.get("zone_tp"), tf_key
                if sig.get("is_hq_zone"): break

    all_evidence.sort(key=lambda e: abs(e.signed_score), reverse=True)
    return Hypothesis(stock, price, label, round(total_score, 2), conf, bullish_count,
                       bearish_count, all_evidence, entry, sl, tp, zone_tf)

def fetch_stock_news_items_full(stock_name: str, max_items: int = 6) -> List[Dict[str, Any]]:
    """🆕 अब ttl=600 (10 मिनट) cache के साथ — बार-बार एक ही स्टॉक की news भारी fetch नहीं करेगा,
    ऊपर update_and_get_stock_news() इसे दिनभर के संचित स्टोर में जोड़ता है।"""
    return _fetch_stock_news_items_full_cached(stock_name, max_items)

@st.cache_data(ttl=600, show_spinner=False)
def _fetch_stock_news_items_full_cached(stock_name: str, max_items: int = 6) -> List[Dict[str, Any]]:
    if feedparser is None: return []
    query = urllib.parse.quote_plus(f"{stock_name} NSE when:1d")
    url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
    items = []
    try:
        resp = requests.get(url, timeout=8, headers=NSE_HEADERS)
        feed = feedparser.parse(resp.content)
        for e in feed.entries[:max_items]:
            pub = e.get("published_parsed")
            pub_dt = datetime(*pub[:6], tzinfo=timezone.utc) if pub else None
            items.append({
                "title": e.get("title", ""), "link": e.get("link", ""), "published": pub_dt,
                "published_str": pub_dt.astimezone(IST).strftime("%H:%M %d-%b") if pub_dt else "",
            })
    except Exception: pass
    return items


# ==========================================
# 🆕 4b. MARKET-WIDE OVERALL BIAS (Market Pulse के लिए) — evidence-engine reuse
# ==========================================
def build_market_overall_bias(idx_quotes: Dict[str, Dict[str, Any]],
                               sector_quotes_raw: Dict[str, Dict[str, Any]],
                               global_quotes: Dict[str, Dict[str, Any]],
                               pcr_value: Optional[float],
                               fii_net: Optional[float], dii_net: Optional[float],
                               market_news: List[Dict[str, Any]]) -> Hypothesis:
    """पूरे मार्केट (Nifty/BankNifty/Sectors/Global/OI/FII/News) को मिलाकर
    एक overall evidence-based bias बनाता है — बिल्कुल उसी engine से जो single-stock
    hypothesis के लिए इस्तेमाल होता है, इसलिए तर्क में consistency रहती है।"""
    evid: List[EvidenceItem] = []

    n = idx_quotes.get("^NSEI")
    if n and n.get("pct") is not None and abs(n["pct"]) >= 0.15:
        d = "bullish" if n["pct"] > 0 else "bearish"
        evid.append(EvidenceItem("Nifty 50", f"Nifty {n['pct']:+.2f}%", d, 1.5, 1.0))

    bn = idx_quotes.get("^NSEBANK")
    if bn and bn.get("pct") is not None and abs(bn["pct"]) >= 0.15:
        d = "bullish" if bn["pct"] > 0 else "bearish"
        evid.append(EvidenceItem("Bank Nifty", f"Bank Nifty {bn['pct']:+.2f}%", d, 1.2, 1.0))

    for name, yft in SECTOR_INDEX_TICKERS.items():
        q = sector_quotes_raw.get(yft)
        if q and q.get("pct") is not None and abs(q["pct"]) >= 0.4:
            d = "bullish" if q["pct"] > 0 else "bearish"
            evid.append(EvidenceItem("Sector", f"{name} {q['pct']:+.2f}%", d, W_SECTOR_MOVE, 0.8))

    for label, yft in [("Global - S&P500", "^GSPC"), ("Global - Dow", "^DJI"),
                        ("Global - Nikkei", "^N225"), ("USD/INR", "INR=X"),
                        ("US 10Y Yield", "^TNX"), ("Crude Oil", "CL=F")]:
        q = global_quotes.get(yft)
        if q and q.get("pct") is not None and abs(q["pct"]) >= 0.3:
            # USD/INR ऊपर जाना रुपये के कमज़ोर होने का संकेत — import-heavy sectors के लिए सामान्यतः negative
            if yft == "INR=X":
                d = "bearish" if q["pct"] > 0 else "bullish"
            else:
                d = "bullish" if q["pct"] > 0 else "bearish"
            evid.append(EvidenceItem(label, f"{q['pct']:+.2f}%", d, W_MACRO_BROAD, 0.7))

    evid += collect_fii_dii_evidence(fii_net, dii_net)

    if pcr_value is not None:
        if pcr_value > 1.15:
            evid.append(EvidenceItem("Options - Nifty PCR", f"PCR {pcr_value} (>1.15) — put-heavy bullish bias",
                                      "bullish", W_OPTIONS_PCR, 0.6))
        elif pcr_value < 0.80:
            evid.append(EvidenceItem("Options - Nifty PCR", f"PCR {pcr_value} (<0.80) — call-heavy bearish bias",
                                      "bearish", W_OPTIONS_PCR, 0.6))

    for item in (market_news or [])[:15]:
        res = score_text_sentiment(item.get("title", ""))
        if res["direction"] != "neutral":
            conf = _freshness_confidence(item.get("published"))
            evid.append(EvidenceItem("Market News", item.get("title", "")[:90], res["direction"], W_NEWS_KEYWORD, conf))

    score = sum(e.signed_score for e in evid)
    bull = sum(1 for e in evid if e.direction == "bullish")
    bear = sum(1 for e in evid if e.direction == "bearish")
    label = classify_score(score, bull + bear)
    conf = confidence_label(bull, bear)
    evid.sort(key=lambda e: abs(e.signed_score), reverse=True)
    return Hypothesis("NIFTY / Overall Market", None, label, round(score, 2), conf, bull, bear, evid)


# ==========================================
# 5. SIDEBAR SETTINGS
# ==========================================
st.sidebar.header("⚙️ Settings")
is_mobile_view = st.sidebar.checkbox("📱 Mobile Compact View", value=False)
refresh_min = st.sidebar.slider("Auto-Refresh हर (मिनट)", 0.5, 15.0, 2.0, 0.5)
if HAS_AUTOREFRESH:
    st_autorefresh(interval=int(refresh_min * 60 * 1000), key="auto_refresh")

st.sidebar.markdown(f"🕒 IST: **{now_ist().strftime('%d-%b-%Y %H:%M:%S')}**")
st.sidebar.markdown("🟢 भारतीय बाज़ार खुला" if is_market_hours() else "🔴 भारतीय बाज़ार बंद")
st.sidebar.caption(f"🗑️ रात {DAILY_CLEAR_HOUR_IST}:00 बजे (IST) दिनभर का जमा डेटा "
                    "(News, Announcements, D&S Cache, Alerts) ऑटो-क्लियर होगा — अगला दिन ताज़ा शुरू होगा।")
if st.sidebar.button("🔄 अभी Refresh करें"):
    st.cache_data.clear()
    st.session_state.ds_zone_cache = {}  # जानबूझकर पूरा clear — user खुद चाहता है fresh scan
    st.rerun()

selected_stocks = st.sidebar.multiselect("Indian Stock Watchlist", WATCHLIST_DEFAULT, default=WATCHLIST_DEFAULT[:40])

st.sidebar.markdown("---")
st.sidebar.subheader("📊 Signal & Scan Settings")
scan_scope = st.sidebar.multiselect("Scan Scope", ["Indian Watchlist", "Global Markets"],
                                     default=["Indian Watchlist", "Global Markets"])
tf_options = ["ALL"] + list(TIMEFRAMES.keys())
# 🆕 डिफ़ॉल्ट टाइमफ्रेम अब: 15 मिनट, 30 मिनट, 1, 2, 4 घंटे और Daily
selected_tf_raw = st.sidebar.multiselect(
    "Signal Scan Timeframes", tf_options,
    default=["15 Min", "30 Min", "1 Hour", "2 Hours", "4 Hours", "Daily"]
)
signal_timeframes = list(TIMEFRAMES.keys()) if "ALL" in selected_tf_raw else selected_tf_raw

selected_indicators = st.sidebar.multiselect(
    "इंडिकेटर चुनें",
    ["Institutional D&S Zones (Demand/Supply)", "EMA Crossover (20/50)", "EMA Crossover (3/5)", "Volume Spike", "RSI (14)"],
    default=["Institutional D&S Zones (Demand/Supply)", "EMA Crossover (20/50)", "Volume Spike"]
)
vol_mult = st.sidebar.slider("Volume Spike Multiplier", 1.5, 5.0, 2.0, 0.5)

st.sidebar.markdown("---")
use_mtf_validation = st.sidebar.checkbox("🔬 Multi-Timeframe No-Break Validation (सख्त, धीमा)", value=False)

# ==========================================
# 🆕 D&S ZONE SENSITIVITY MODE
# ==========================================
st.sidebar.markdown("---")
st.sidebar.subheader("🎚️ D&S Zone Sensitivity")
ds_mode = st.sidebar.radio(
    "Detection Mode",
    ["Strict (Institutional)", "Balanced", "Relaxed (ज़्यादा सिग्नल)"],
    index=1
)

if ds_mode == "Strict (Institutional)":
    USE_SWEEP_FILTER, USE_IMBALANCE = True, True
    LEG_OUT_ATR_MULT, MAX_WICK_PCT, LEG_OUT_VOL_MULT = 1.2, 0.25, 1.5
elif ds_mode == "Balanced":
    USE_SWEEP_FILTER, USE_IMBALANCE = False, True
    LEG_OUT_ATR_MULT, MAX_WICK_PCT, LEG_OUT_VOL_MULT = 1.0, 0.30, 1.2
else:  # Relaxed
    USE_SWEEP_FILTER, USE_IMBALANCE = False, False
    LEG_OUT_ATR_MULT, MAX_WICK_PCT, LEG_OUT_VOL_MULT = 0.8, 0.35, 1.0

st.sidebar.caption(f"मौजूदा mode: **{ds_mode}** — ज़ोन ज़्यादा/कम मिलने का सीधा असर इसी पर है।"
                    " Strict = कम पर उच्च-गुणवत्ता वाले zones, Relaxed = ज़्यादा zones पर कम सख्त।")

with st.sidebar.expander("🐞 D&S Debug Info"):
    st.write(f"Mode: {ds_mode}")
    st.write(f"USE_SWEEP_FILTER={USE_SWEEP_FILTER}, USE_IMBALANCE={USE_IMBALANCE}")
    st.write(f"LEG_OUT_ATR_MULT={LEG_OUT_ATR_MULT}, MAX_WICK_PCT={MAX_WICK_PCT}, LEG_OUT_VOL_MULT={LEG_OUT_VOL_MULT}")
    st.write(f"CONTEXT_BUFFER={CONTEXT_BUFFER}")

# ==========================================
# 🆕 GIFT Nifty (Manual Input — free auto-fetch उपलब्ध नहीं है)
# ==========================================
st.sidebar.markdown("---")
st.sidebar.subheader("🌅 GIFT Nifty (Manual)")
st.sidebar.caption("GIFT Nifty का कोई free/official yfinance ticker उपलब्ध नहीं — कृपया मैन्युअली डालें (optional)।")
manual_gift_pct = st.sidebar.text_input("GIFT Nifty % change (जैसे +0.35)", value="")
if manual_gift_pct.strip():
    st.session_state["manual_gift_nifty_pct"] = manual_gift_pct.strip()

def get_gift_nifty_display() -> str:
    v = st.session_state.get("manual_gift_nifty_pct")
    if v:
        try:
            f = float(v.replace("%", "").replace("+", ""))
            arrow = "🟢▲" if f > 0 else ("🔴▼" if f < 0 else "⚪●")
            return f"{arrow} {f:+.2f}% (manual)"
        except Exception:
            return f"{v} (manual)"
    return "उपलब्ध नहीं — sidebar से डालें"

if "alerts" not in st.session_state: st.session_state.alerts = []
if "all_tf_signals_map" not in st.session_state: st.session_state.all_tf_signals_map = {}
if "ds_zone_cache" not in st.session_state: st.session_state.ds_zone_cache = {}

# 🆕 पुराने 20:00 वाले अलर्ट-क्लियर की जगह अब यूनिफाइड 23:00 (रात 11 बजे) डेली-क्लियर
maybe_clear_all_daily_data()


# ==========================================
# 6. DATA FETCH ENGINES
# ==========================================
@st.cache_data(ttl=180, show_spinner=False)
def unified_yf_download_engine(tickers_tuple, period="10d", interval="1d") -> Dict[str, pd.DataFrame]:
    tickers = list(tickers_tuple)
    if not tickers: return {}
    try:
        data = yf.download(tickers, period=period, interval=interval, group_by="ticker", progress=False, threads=True)
    except Exception: return {}
    out = {}
    for t in tickers:
        try:
            df = data[t].dropna() if len(tickers) > 1 else data.dropna()
            if not df.empty: out[t] = df
        except Exception: continue
    return out

def get_quotes(tickers: List[str]) -> Dict[str, Dict[str, Any]]:
    daily_data = unified_yf_download_engine(tuple(tickers), period="10d", interval="1d")
    intraday_data = unified_yf_download_engine(tuple(tickers), period="1d", interval="5m")
    quotes = {}
    for t in tickers:
        df_d = daily_data.get(t)
        if df_d is None or len(df_d) < 2: continue
        last, prev = df_d["Close"].iloc[-1], df_d["Close"].iloc[-2]
        chg = last - prev
        pct = (chg / prev) * 100
        df_intra = intraday_data.get(t)
        live_price = df_intra["Close"].iloc[-1] if df_intra is not None and len(df_intra) > 0 else last
        quotes[t] = {"price": live_price, "pct": pct, "chg": chg}
    return quotes

@st.cache_data(ttl=300, show_spinner=False)
def fetch_tf_data_single_v2(tf_key: str, items_tuple: Tuple):
    cfg = TIMEFRAMES[tf_key]
    yf_symbols = [item[1] for item in items_tuple]
    if not yf_symbols: return {}
    data = unified_yf_download_engine(tuple(yf_symbols), period=cfg["period"], interval=cfg["interval"])
    out = {}
    for display_name, yf_sym, tv_sym, cat in items_tuple:
        df = data.get(yf_sym)
        if df is None or df.empty: continue
        if cfg["resample"]:
            df = df.resample(cfg["resample"]).agg(
                {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()
        if len(df) >= 20:
            out[display_name] = {"df": df, "tv": tv_sym, "category": cat}
    return out

def fetch_all_tf_data_fast_v2(selected_tfs, items_tuple):
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(selected_tfs), 8)) as executor:
        future_to_tf = {executor.submit(fetch_tf_data_single_v2, tf, items_tuple): tf for tf in selected_tfs}
        for future in concurrent.futures.as_completed(future_to_tf):
            tf = future_to_tf[future]
            try: results[tf] = future.result()
            except Exception: results[tf] = {}
    return results

@st.cache_data(ttl=900, show_spinner=False)
def fetch_nse_json(api_path):
    session = requests.Session()
    session.headers.update(NSE_HEADERS)
    try:
        session.get("https://www.nseindia.com", timeout=8)
        r = session.get(f"https://www.nseindia.com{api_path}", timeout=8)
        if r.status_code == 200: return r.json()
    except Exception: pass
    return None

@st.cache_data(ttl=900, show_spinner=False)
def fetch_fii_dii():
    try:
        r = requests.get("https://sedg.in/p8nximtd", headers=NSE_HEADERS, timeout=10, allow_redirects=True)
        for t in pd.read_html(io.StringIO(r.text)):
            if t.shape[1] >= 3 and t.shape[0] >= 3: return t.head(5), "StockEdge"
    except Exception: pass
    fii_data = fetch_nse_json("/api/fiidiiTradeReact")
    if fii_data: return pd.DataFrame(fii_data).head(5), "NSE (fallback)"
    return None, None

def fii_dii_insight(df):
    try:
        cols_lower = {c.lower(): c for c in df.columns}
        net_col = cols_lower.get("netvalue") or cols_lower.get("net_value")
        cat_col = cols_lower.get("category")
        if not net_col or not cat_col: return None
        fii_net, dii_net = None, None
        for _, row in df.iterrows():
            cat = str(row[cat_col]).upper()
            try: val = float(row[net_col])
            except Exception: continue
            if "FII" in cat or "FPI" in cat: fii_net = val if fii_net is None else fii_net
            elif "DII" in cat: dii_net = val if dii_net is None else dii_net
        if fii_net is None and dii_net is None: return None
        if fii_net > 0 and dii_net > 0: return "success", f"🟢 FII (₹{fii_net:+.0f} Cr) और DII (₹{dii_net:+.0f} Cr) दोनों खरीदार।"
        if fii_net < 0 and dii_net > 0: return "info", f"🔵 FII बिकवाली पर DII सपोर्ट (₹{dii_net:+.0f} Cr)।"
        if fii_net > 0 and dii_net < 0: return "info", f"🔵 FII खरीदारी, DII बेच रहे।"
        if fii_net < 0 and dii_net < 0: return "error", f"🔴 दोनों बिकवाल — Cautious bias।"
        return None
    except Exception: return None

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_stock_quick_news_link_live(stock_name: str):
    if feedparser is None: return None
    query = urllib.parse.quote_plus(f"{stock_name} NSE when:1d")
    url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
    try:
        feed = feedparser.parse(requests.get(url, timeout=8, headers=NSE_HEADERS).content)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        for e in feed.entries[:5]:
            pub = e.get("published_parsed")
            if pub and datetime(*pub[:6], tzinfo=timezone.utc) >= cutoff:
                return e.link
    except Exception: pass
    return None

def fetch_news_links_parallel(stocks):
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(fetch_stock_quick_news_link_live, s): s for s in stocks}
        for fut in concurrent.futures.as_completed(futures):
            s = futures[fut]
            try: results[s] = fut.result()
            except Exception: results[s] = None
    return results

@st.cache_data(ttl=300, show_spinner=False)
def fetch_nse_corporate_announcements():
    data = fetch_nse_json("/api/corporate-announcements?index=equities")
    if not data: return []
    items = []
    for d in data[:50]:
        try:
            items.append({"symbol": d.get("symbol", ""), "subject": d.get("desc") or d.get("subject") or "",
                          "time": d.get("an_dt") or d.get("sort_date") or ""})
        except Exception: continue
    return items

@st.cache_data(ttl=600, show_spinner=False)
def fetch_market_wide_news(max_items=10) -> List[Dict[str, Any]]:
    """पूरे मार्केट (Nifty/Sensex/भारतीय बाज़ार) से जुड़ी ताज़ा headlines — Market Pulse के लिए।
    ttl=600 (10 मिनट) cache — इसलिए बार-बार कॉल करने पर भी नेटवर्क लोड नहीं बढ़ता,
    ऊपर update_and_get_daily_market_news() इसे दिनभर के संचित स्टोर में जोड़ता रहता है।"""
    if feedparser is None:
        return []
    query = urllib.parse.quote_plus("Nifty Sensex Indian stock market when:1d")
    url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
    items = []
    try:
        feed = feedparser.parse(requests.get(url, timeout=8, headers=NSE_HEADERS).content)
        for e in feed.entries[:max_items]:
            pub = e.get("published_parsed")
            pub_dt = datetime(*pub[:6], tzinfo=timezone.utc) if pub else None
            items.append({
                "title": e.get("title", ""), "link": e.get("link", ""), "published": pub_dt,
                "published_str": pub_dt.astimezone(IST).strftime("%H:%M %d-%b") if pub_dt else "",
            })
    except Exception:
        pass
    return items


# ==========================================
# 🆕 6b. MARKET PULSE — Pre-Market / Live Snapshot Renderer
# ==========================================
def render_market_pulse():
    """एक-नज़र में पूरा मार्केट सेंटीमेंट: GIFT Nifty, Nifty/BankNifty, सेक्टर, ग्लोबल मार्केट्स,
    OI/PCR, FII/DII, प्रमुख स्टॉक्स, ताज़ा न्यूज़ और अंत में evidence-based Overall Market Bias।"""
    st.markdown("### 🌅 Market Pulse — प्री-मार्केट / लाइव स्नैपशॉट")
    st.caption("एक-नज़र में: GIFT Nifty, Nifty/BankNifty, सेक्टर, ग्लोबल मार्केट्स, OI/PCR, FII/DII, टॉप स्टॉक्स, न्यूज़")

    # --- Row 1: GIFT Nifty + Nifty + Bank Nifty ---
    idx_quotes = get_quotes(["^NSEI", "^NSEBANK"])
    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("GIFT Nifty", get_gift_nifty_display())
    with c2:
        n = idx_quotes.get("^NSEI")
        st.metric("Nifty 50", f"{n['price']:.2f}" if n else "—", f"{n['pct']:+.2f}%" if n else None)
    with c3:
        bn = idx_quotes.get("^NSEBANK")
        st.metric("Bank Nifty", f"{bn['price']:.2f}" if bn else "—", f"{bn['pct']:+.2f}%" if bn else None)

    # --- Row 2: सेक्टर सेंटीमेंट ---
    st.markdown("#### 🏭 सेक्टर सेंटीमेंट")
    sector_quotes_raw = get_quotes(list(SECTOR_INDEX_TICKERS.values()))
    sec_df = pd.DataFrame([
        {"Sector": name, "% Chg": sector_quotes_raw.get(yft, {}).get("pct")}
        for name, yft in SECTOR_INDEX_TICKERS.items()
    ])
    sec_df["% Chg"] = sec_df["% Chg"].apply(lambda v: f"{v:+.2f}%" if v is not None else "—")
    st.dataframe(style_pct_columns(sec_df, ["% Chg"]), use_container_width=True, hide_index=True)

    # --- Row 3: ग्लोबल मार्केट्स ---
    st.markdown("#### 🌍 ग्लोबल मार्केट्स")
    global_quotes = get_quotes([g[2] for g in GLOBAL_INSTRUMENTS if g[2]])
    g_df = pd.DataFrame([
        {"Instrument": name, "% Chg": global_quotes.get(yft, {}).get("pct")}
        for _, name, yft, _ in GLOBAL_INSTRUMENTS
    ])
    g_df["% Chg"] = g_df["% Chg"].apply(lambda v: f"{v:+.2f}%" if v is not None else "—")
    st.dataframe(style_pct_columns(g_df, ["% Chg"]), use_container_width=True, hide_index=True)

    # --- Row 4: Options / OI / PCR ---
    st.markdown("#### 📌 Options: Nifty PCR & OI")
    oc_data = fetch_nse_json("/api/option-chain-indices?symbol=NIFTY")
    pcr_value = None
    if oc_data:
        try:
            records = oc_data["records"]["data"]
            call_oi = {r["strikePrice"]: r["CE"]["openInterest"] for r in records if "CE" in r}
            put_oi = {r["strikePrice"]: r["PE"]["openInterest"] for r in records if "PE" in r}
            tc, tp = sum(call_oi.values()), sum(put_oi.values())
            pcr_value = round(tp / tc, 2) if tc else None
            oc1, oc2, oc3 = st.columns(3)
            oc1.metric("PCR", pcr_value if pcr_value is not None else "—")
            oc2.metric("Resistance (Max Call OI)", max(call_oi, key=call_oi.get) if call_oi else "—")
            oc3.metric("Support (Max Put OI)", max(put_oi, key=put_oi.get) if put_oi else "—")
        except Exception:
            st.warning("Option chain data parse नहीं हो सका।")
    else:
        st.caption("Option chain data अभी उपलब्ध नहीं (NSE rate-limit हो सकता है)।")

    # --- Row 5: FII / DII (🆕 अब दिनभर के trail के साथ) ---
    st.markdown("#### 💰 FII / DII (पिछला EOD + आज का Trail)")
    fii_df, source, fii_log = update_and_get_fii_dii_log()
    fii_net_val = dii_net_val = None
    if fii_df is not None:
        st.dataframe(fii_df, use_container_width=True, hide_index=True)
        try:
            net_col = [c for c in fii_df.columns if "net" in c.lower()][0]
            cat_col = [c for c in fii_df.columns if "cat" in c.lower()][0]
            for _, r in fii_df.iterrows():
                if "FII" in str(r[cat_col]).upper(): fii_net_val = float(r[net_col])
                elif "DII" in str(r[cat_col]).upper(): dii_net_val = float(r[net_col])
            insight = fii_dii_insight(fii_df)
            if insight: getattr(st, insight[0])(insight[1])
        except Exception:
            pass
        st.caption(f"Source: {source}")
    else:
        st.caption("FII/DII data अभी उपलब्ध नहीं।")
    if len(fii_log) > 1:
        with st.expander(f"📈 आज का FII/DII Trail ({len(fii_log)} स्नैपशॉट्स)"):
            st.dataframe(pd.DataFrame(fii_log), use_container_width=True, hide_index=True)

    # --- Row 6: प्रमुख स्टॉक्स ---
    st.markdown("#### 🏆 प्रमुख स्टॉक्स (Nifty Heavyweights)")
    hw_list = sorted(NIFTY_HEAVYWEIGHTS)
    hw_quotes = get_quotes([yf_ticker_for_stock(s) for s in hw_list])
    hw_rows = [{"Stock": s, "% Chg": hw_quotes[yf_ticker_for_stock(s)]["pct"]}
               for s in hw_list if yf_ticker_for_stock(s) in hw_quotes]
    if hw_rows:
        hw_df = pd.DataFrame(hw_rows).sort_values("% Chg", ascending=False)
        hw_df["% Chg"] = hw_df["% Chg"].apply(lambda v: f"{v:+.2f}%")
        st.dataframe(style_pct_columns(hw_df, ["% Chg"]), use_container_width=True, hide_index=True)
    else:
        st.caption("Heavyweight quotes उपलब्ध नहीं।")

    # --- Row 7: ताज़ा News (🆕 दिनभर संचित) ---
    st.markdown("#### 📰 ताज़ा Market News (आज का संचित लॉग)")
    with st.spinner("News scan हो रहा है..."):
        market_news = update_and_get_daily_market_news()
    if market_news:
        for item in market_news[:8]:
            st.markdown(f"- [{item['title']}]({item['link']}) · _{item['published_str']}_")
        st.caption(f"कुल आज जमा: {len(market_news)} headlines — पूरा लॉग '🗄️ Daily Log' टैब में देखें।")
    else:
        st.caption("कोई ताज़ा news नहीं मिली।")

    # --- Row 8: Overall Evidence-Based Bias ---
    st.markdown("---")
    st.markdown("#### 🧭 ओवरऑल मार्केट बायस (सभी evidence combine करके)")
    overall = build_market_overall_bias(idx_quotes, sector_quotes_raw, global_quotes,
                                         pcr_value, fii_net_val, dii_net_val, market_news)
    render_hypothesis_card(overall)


# ==========================================
# 7. STICKY SUMMARY BAR (हमेशा दिखे — freshness/status)
# ==========================================
st.markdown(f"""
<div class="sticky-bar">
    <span>🕒 {now_ist().strftime('%H:%M:%S')}</span>
    <span>{'🟢 Market Open' if is_market_hours() else '🔴 Market Closed'}</span>
    <span>Data: <b>Delayed ~15min (yfinance)</b></span>
</div>
""", unsafe_allow_html=True)
st.title("📈 Full Market Dashboard & Institutional D&S Scanner")
st.caption("⚠️ EDUCATIONAL टूल — SEBI-registered निवेश सलाह नहीं। Data delayed हो सकता है, अपनी पुष्टि खुद करें।")


# ==========================================
# 8. TABS
# ==========================================
# 🆕 नया टैब जोड़ा गया: "🗄️ Daily Log"
tab_names = ["📊 Signals", "🤖 AI Hypothesis", "🗄️ Daily Log", "🔔 Alerts", "🌍 Global", "📋 Watchlist",
             "🏭 Sector", "💰 FII/DII+Nifty", "🗓️ Calendar", "🏆 Movers"]

if is_mobile_view:
    section = st.selectbox("📱 सेक्शन चुनें", tab_names)
else:
    tabs = st.tabs(tab_names)


def render_signal_card(row):
    css_class = "demand" if ("DEMAND" in row["टाइप"] or "UP" in row["टाइप"]) else \
                "supply" if ("SUPPLY" in row["टाइप"] or "DOWN" in row["टाइप"]) else ""
    if "HQ" in row["सिग्नल"]: css_class = "hq"
    st.markdown(f"""
    <div class="signal-card {css_class}">
        <div class="signal-title">{row['सिग्नल']} · {row['एसेट']} <span style="float:right">{row['LTP']}</span></div>
        <div class="signal-sub">{row['टाइप']}</div>
        <div class="signal-sub">⏱ {row['टाइमफ्रेम']} · {row['समय']} · <a href="{row['Chart']}" target="_blank">📈 Chart</a></div>
    </div>
    """, unsafe_allow_html=True)


# ---------- SIGNALS TAB CONTENT (function इसलिए ताकि mobile/desktop दोनों में reuse हो) ----------
def render_signals_tab():
    st.subheader("📊 Institutional D&S + Technical Scanner (Incremental Cache ⚡)")
    is_after_close = now_ist().hour >= 16 or now_ist().hour < 8
    if is_after_close:
        st.info("🌙 भारतीय बाज़ार बंद — Daily स्कैन + Global Markets Live स्कैन चालू है।")

    all_scan_items = []
    if "Indian Watchlist" in scan_scope:
        for s in selected_stocks:
            all_scan_items.append((s, yf_ticker_for_stock(s), tv_symbol_for_stock(s), "🇮🇳 Stock"))
    if "Global Markets" in scan_scope:
        for sym, name, yft, tvs in GLOBAL_INSTRUMENTS:
            if yft: all_scan_items.append((f"{sym} ({name})", yft, tvs, "🌍 Global"))

    if not signal_timeframes or not all_scan_items:
        st.warning("कृपया कम से कम एक Timeframe और Scope सलेक्ट करें।")
        return

    required_tfs = set(signal_timeframes)
    if use_mtf_validation:
        for tf_key in signal_timeframes:
            lower_tf = LOWER_TF_MAP.get(tf_key)
            if lower_tf: required_tfs.add(lower_tf)

    with st.spinner("⚡ Fast Scanning चल रहा है..."):
        all_tf_data = fetch_all_tf_data_fast_v2(tuple(required_tfs), tuple(all_scan_items))

    rows = []
    existing_keys = {a["key"] for a in st.session_state.alerts}
    st.session_state.all_tf_signals_map = {}  # हर full-refresh पर rebuild (हल्का — सिर्फ dict, compute नहीं)

    for tf_key in signal_timeframes:
        tf_is_intraday = TIMEFRAMES[tf_key]["intraday"]
        tf_data = all_tf_data.get(tf_key, {})
        lower_tf_key = LOWER_TF_MAP.get(tf_key) if use_mtf_validation else None
        lower_tf_data_for_this_tf = all_tf_data.get(lower_tf_key, {}) if lower_tf_key else {}

        for item_name, item_dict in tf_data.items():
            cat = item_dict["category"]
            if is_after_close and tf_is_intraday and cat == "🇮🇳 Stock": continue

            df = item_dict["df"]
            tv_sym = item_dict["tv"]
            price, bar_time = df["Close"].iloc[-1], df.index[-1]
            close_np = df["Close"].to_numpy(dtype=np.float64)
            vol_np = df["Volume"].to_numpy(dtype=np.float64)

            type_parts, is_daily_vol_spike, is_hq_ds_zone = [], False, False
            zone_detail = None

            if "Institutional D&S Zones (Demand/Supply)" in selected_indicators:
                lower_item = lower_tf_data_for_this_tf.get(item_name)
                lower_df = lower_item["df"] if lower_item else None
                # 🆕 यहीं incremental cached scanner call हो रहा है
                ds_sig, is_hq, zone_detail = check_ds_zones(df, item_name, tf_key,
                                                             lower_tf_df=lower_df, use_mtf=use_mtf_validation)
                if ds_sig:
                    type_parts.append(ds_sig)
                    if is_hq: is_hq_ds_zone = True

            cross = ema35_cross = rsi_sig = vr = None
            if "EMA Crossover (20/50)" in selected_indicators:
                cross = check_ema_cross_fast(close_np)
                if cross: type_parts.append(cross)
            if "EMA Crossover (3/5)" in selected_indicators:
                ema35_cross = check_ema_cross_3_5(close_np)
                if ema35_cross: type_parts.append(ema35_cross)
            if "Volume Spike" in selected_indicators:
                vr = check_volume_spike_fast(vol_np, vol_mult)
                if vr:
                    type_parts.append(vr)
                    if tf_key == "Daily": is_daily_vol_spike = True
            if "RSI (14)" in selected_indicators:
                rsi_sig = check_rsi_fast(close_np)
                if rsi_sig: type_parts.append(rsi_sig)

            # 🆕 AI Hypothesis के लिए raw signal-dict भी collect करें (बिना नया fetch)
            st.session_state.all_tf_signals_map.setdefault(item_name, {})[tf_key] = {
                "ds_signal": type_parts[0] if (type_parts and "ZONE" in type_parts[0]) else None,
                "is_hq_zone": is_hq_ds_zone, "mtf_confirmed": use_mtf_validation,
                "ema_cross": cross, "ema_cross_35": ema35_cross, "rsi_signal": rsi_sig, "vol_spike": vr,
                "candle_bullish": bool(df["Close"].iloc[-1] > df["Open"].iloc[-1]),
                "price": float(price), "bar_time": bar_time.strftime("%H:%M %d-%b"),
                "zone_entry": zone_detail["entry"] if zone_detail else None,
                "zone_sl": zone_detail["sl"] if zone_detail else None,
                "zone_tp": zone_detail["tp"] if zone_detail else None,
            }

            if not type_parts: continue
            if is_hq_ds_zone: stars = "🚀 HQ Zone"
            elif is_daily_vol_spike: stars = "🔥 Vol Spike"
            elif len(type_parts) >= 2: stars = "⭐⭐ Strong"
            else: stars = "⭐ Signal"

            bar_time_str = bar_time.strftime("%H:%M %d-%b")
            rows.append({"सिग्नल": stars, "कैटेगरी": cat, "एसेट": item_name, "टाइमफ्रेम": tf_key,
                         "टाइप": " | ".join(type_parts), "LTP": round(price, 2), "समय": bar_time_str,
                         "Chart": tv_link(tv_sym)})

            alert_key = f"{item_name}|{tf_key}|{'|'.join(type_parts)}|{bar_time_str}"
            if alert_key not in existing_keys:
                st.session_state.alerts.append({
                    "key": alert_key, "stock": item_name, "category": cat, "tf": tf_key,
                    "type": " | ".join(type_parts), "stars": stars, "time": bar_time_str,
                    "logged_at": now_ist().strftime("%H:%M:%S"), "chart": tv_link(tv_sym),
                })
                existing_keys.add(alert_key)

    if not rows:
        st.success("अभी कोई नया सिग्नल नहीं मिला।")
    else:
        sig_df = pd.DataFrame(rows)
        sort_rank = {"🚀 HQ Zone": 4, "🔥 Vol Spike": 3, "⭐⭐ Strong": 2, "⭐ Signal": 1}
        sig_df["_sort"] = sig_df["सिग्नल"].map(lambda x: sort_rank.get(x, 0))
        sig_df = sig_df.sort_values(["_sort", "समय"], ascending=[False, False]).drop(columns="_sort")

        if is_mobile_view:
            for _, row in sig_df.iterrows(): render_signal_card(row)
        else:
            def hl(row):
                if "HQ Zone" in row["सिग्नल"]: base = "background-color:#d1e7dd; font-weight:bold;"
                elif row["सिग्नल"] == "🔥 Vol Spike": base = f"background-color:{COLOR_SPIKE_BG}"
                elif row["सिग्नल"] == "⭐⭐ Strong": base = "background-color:#e8d4f8"
                elif "DEMAND" in row["टाइप"] or "UP" in row["टाइप"]: base = f"background-color:{COLOR_POS_BG}"
                elif "SUPPLY" in row["टाइप"] or "DOWN" in row["टाइप"]: base = f"background-color:{COLOR_NEG_BG}"
                else: base = "background-color:#fff2cc"
                return [base] * len(row)
            st.dataframe(sig_df.style.apply(hl, axis=1), use_container_width=True, hide_index=True,
                        column_config={"Chart": st.column_config.LinkColumn("Chart", display_text="📈 खोलें")})


LABEL_COLORS = {"STRONG BUY": ("#0a7d2f", "#d4f8d4"), "BUY": ("#0a7d2f", "#eafbea"),
                "NEUTRAL": ("#70758A", "#f0f1f5"), "SELL": ("#c0392b", "#fdeeee"),
                "STRONG SELL": ("#c0392b", "#f8d4d4")}

def render_hypothesis_card(h: Hypothesis):
    text_c, bg_c = LABEL_COLORS.get(h.label, ("#333", "#eee"))
    st.markdown(f"""
    <div style="background:{bg_c}; border-left:5px solid {text_c}; border-radius:10px; padding:14px 16px; margin-bottom:10px;">
        <div style="display:flex; justify-content:space-between; align-items:center;">
            <span style="font-size:16px; font-weight:800; color:{text_c};">{h.label}</span>
            <span style="font-size:13px; color:#555;">Score: {h.score} · भरोसा: {h.confidence_label}</span>
        </div>
        <div style="font-size:13px; margin-top:4px;">
            <b>{h.stock}</b> · 👍 {h.bullish_count} बुलिश · 👎 {h.bearish_count} बेयरिश evidence
        </div>
    </div>""", unsafe_allow_html=True)
    if h.suggested_entry:
        c1, c2, c3 = st.columns(3)
        c1.metric("Entry (D&S Zone)", f"{h.suggested_entry:.2f}")
        c2.metric("Stop Loss", f"{h.suggested_sl:.2f}")
        c3.metric("Target", f"{h.suggested_tp:.2f}")
        st.caption(f"स्रोत: {h.zone_source_tf} टाइमफ्रेम का Institutional D&S Zone")
    if h.label == "NEUTRAL" and (h.bullish_count + h.bearish_count) < 2:
        st.info("⚠️ पर्याप्त evidence नहीं — कोई पक्का निष्कर्ष नहीं।")
    with st.expander(f"🔍 सभी Evidence देखें ({len(h.evidence)})"):
        for e in h.evidence:
            icon = "🟢" if e.direction == "bullish" else ("🔴" if e.direction == "bearish" else "⚪")
            st.markdown(f"{icon} **[{e.source}]** {e.detail} "
                        f"<span style='color:#888; font-size:11px;'>(weight: {e.signed_score:+.2f})</span>",
                        unsafe_allow_html=True)
    st.caption("⚠️ EDUCATIONAL टूल — SEBI-registered सलाह नहीं।")

def render_ai_hypothesis_tab():
    st.subheader("🤖 AI Buy/Sell Hypothesis — Evidence-Based")

    # 🆕 Market Pulse — प्री-मार्केट/लाइव पूरे मार्केट का स्नैपशॉट, single-stock hypothesis से पहले
    with st.expander("🌅 पहले Market Pulse देखें (प्री-मार्केट/ओपनिंग स्नैपशॉट)", expanded=True):
        if st.button("🔄 Market Pulse Load/Refresh करें", key="btn_market_pulse"):
            with st.spinner("पूरे मार्केट का डेटा collect हो रहा है..."):
                render_market_pulse()
        else:
            st.info("बटन दबाकर GIFT Nifty, Nifty/BankNifty, सेक्टर, ग्लोबल मार्केट्स, OI/PCR, FII/DII, "
                    "प्रमुख स्टॉक्स और ताज़ा न्यूज़ का पूरा स्नैपशॉट देखें।")

    st.markdown("---")
    st.caption("पहले Signals टैब खोलें ताकि तकनीकी evidence collect हो जाए, फिर यहां आएं।")
    if not st.session_state.all_tf_signals_map:
        st.warning("⚠️ पहले '📊 Signals' टैब खोलें ताकि Technical evidence भर जाए।")
        return

    macro_quotes = get_quotes([g[2] for g in GLOBAL_INSTRUMENTS if g[2]])
    sector_quotes_raw = get_quotes(list(SECTOR_INDEX_TICKERS.values()))
    sector_quotes = {name: sector_quotes_raw.get(yft, {}).get("pct") for name, yft in SECTOR_INDEX_TICKERS.items()}
    fii_df, _, _ = update_and_get_fii_dii_log()
    fii_net_val = dii_net_val = None
    if fii_df is not None:
        try:
            net_col = [c for c in fii_df.columns if "net" in c.lower()][0]
            cat_col = [c for c in fii_df.columns if "cat" in c.lower()][0]
            for _, r in fii_df.iterrows():
                if "FII" in str(r[cat_col]).upper(): fii_net_val = float(r[net_col])
                elif "DII" in str(r[cat_col]).upper(): dii_net_val = float(r[net_col])
        except Exception: pass

    oc_data = fetch_nse_json("/api/option-chain-indices?symbol=NIFTY")
    pcr_value = None
    if oc_data:
        try:
            records = oc_data["records"]["data"]
            tc = sum(r["CE"]["openInterest"] for r in records if "CE" in r)
            tp = sum(r["PE"]["openInterest"] for r in records if "PE" in r)
            pcr_value = round(tp / tc, 2) if tc else None
        except Exception: pass

    corp_announcements = update_and_get_corp_announcements()  # 🆕 दिनभर संचित
    mode = st.radio("व्यू चुनें", ["📋 पूरी Watchlist Radar", "🔎 सिंगल स्टॉक Deep-Dive"], horizontal=True)

    if mode == "🔎 सिंगल स्टॉक Deep-Dive":
        stock = st.selectbox("स्टॉक चुनें", list(st.session_state.all_tf_signals_map.keys()) or selected_stocks)
        if st.button("🤖 Hypothesis बनाएं"):
            with st.spinner("News evidence collect हो रहा है..."):
                news_items = update_and_get_stock_news(stock)  # 🆕 दिनभर संचित
            h = build_hypothesis(stock, None, st.session_state.all_tf_signals_map.get(stock, {}),
                                  news_items, corp_announcements, macro_quotes, sector_quotes,
                                  fii_net_val, dii_net_val, pcr_value)
            render_hypothesis_card(h)
    else:
        if st.button("📋 Watchlist Radar बनाएं (बिना नई News fetch — तेज़, संचित डेटा इस्तेमाल)"):
            rows = []
            for stock in st.session_state.all_tf_signals_map.keys():
                h = build_hypothesis(stock, None, st.session_state.all_tf_signals_map.get(stock, {}),
                                      None, corp_announcements, macro_quotes, sector_quotes,
                                      fii_net_val, dii_net_val, pcr_value)
                rows.append({"स्टॉक": stock, "Label": h.label, "Score": h.score,
                            "भरोसा": h.confidence_label, "बुलिश": h.bullish_count, "बेयरिश": h.bearish_count})
            df = pd.DataFrame(rows).sort_values("Score", key=abs, ascending=False)
            st.dataframe(df, use_container_width=True, hide_index=True)
            st.caption("💡 यह result में संचित market-news mentions + corp announcements + technical + "
                       "macro/sector/FII-DII/PCR evidence शामिल है। पूरी stock-specific news के लिए "
                       "'Single स्टॉक Deep-Dive' इस्तेमाल करें।")


def render_daily_log_tab():
    """🆕 दिनभर जमा हुए महत्वपूर्ण मार्केट इवेंट/न्यूज/डेटा का लॉग + वॉचलिस्ट पर evidence-based असर।
    रात 11 बजे (IST) यह सब ऑटोमेटिक क्लियर हो जाता है।"""
    st.subheader("🗄️ आज का जमा डेटा (Daily Persistent Store)")
    store = get_daily_store()
    now = now_ist()
    clear_dt = now.replace(hour=DAILY_CLEAR_HOUR_IST, minute=0, second=0, microsecond=0)
    remaining = clear_dt - now
    if remaining.total_seconds() < 0:
        st.warning("🌙 रात 11 बजे के बाद — डेटा जल्द ही ऑटो-क्लियर होगा / हो चुका है।")
    else:
        hrs, rem = divmod(int(remaining.total_seconds()), 3600)
        mins = rem // 60
        st.info(f"⏳ अगला ऑटो-क्लियर रात 11:00 बजे — लगभग {hrs} घंटे {mins} मिनट बाकी।")

    c1, c2, c3 = st.columns(3)
    c1.metric("संचित Market News", len(store["market_news"]))
    c2.metric("Corporate Announcements", len(store["corp_announcements"]))
    c3.metric("FII/DII स्नैपशॉट्स", len(store["fii_dii_log"]))

    if st.button("🔄 अभी डेटा Update करें (हल्का — सिर्फ नया merge होगा)"):
        with st.spinner("नया डेटा merge हो रहा है..."):
            update_and_get_daily_market_news()
            update_and_get_corp_announcements()
            update_and_get_fii_dii_log()
        st.success("अपडेट हो गया — सिर्फ नई entries जोड़ी गईं, पूरा दोबारा fetch नहीं हुआ।")

    st.markdown("#### 📰 आज की संचित Market News")
    if store["market_news"]:
        for item in store["market_news"][:30]:
            st.markdown(f"- [{item['title']}]({item['link']}) · _{item.get('published_str','')}_")
    else:
        st.caption("अभी तक कोई news जमा नहीं — ऊपर बटन दबाकर Update करें।")

    st.markdown("#### 📢 Corporate Announcements (संचित)")
    if store["corp_announcements"]:
        ann_df = pd.DataFrame(store["corp_announcements"][:30])
        st.dataframe(ann_df, use_container_width=True, hide_index=True)
    else:
        st.caption("अभी कोई announcement जमा नहीं।")

    st.markdown("#### 💰 FII/DII Trail (आज)")
    if store["fii_dii_log"]:
        st.dataframe(pd.DataFrame(store["fii_dii_log"]), use_container_width=True, hide_index=True)
    else:
        st.caption("अभी कोई FII/DII स्नैपशॉट जमा नहीं।")

    st.markdown("---")
    st.markdown("#### 🎯 वॉचलिस्ट पर संचित न्यूज़ का Evidence-Based असर")
    st.caption("दिनभर जमा हुई Market News + Corporate Announcements में वॉचलिस्ट स्टॉक्स के ज़िक्र को स्कैन करके "
               "bullish/bearish evidence दिखाया गया है — बिना किसी नए भारी fetch के (पूरी तरह session-cached)।")
    if st.button("🔍 वॉचलिस्ट Impact स्कैन करें"):
        rows = []
        for stock in selected_stocks:
            evid = collect_daily_market_mentions_evidence(stock)
            if not evid:
                continue
            bull = sum(1 for e in evid if e.direction == "bullish")
            bear = sum(1 for e in evid if e.direction == "bearish")
            rows.append({"Stock": stock, "बुलिश Evidence": bull, "बेयरिश Evidence": bear,
                        "टॉप डिटेल": evid[0].detail[:80]})
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.info("अभी वॉचलिस्ट स्टॉक्स से जुड़ी कोई संचित न्यूज़/announcement नहीं मिली। "
                    "ऊपर 'Update करें' बटन दबाकर पहले डेटा जमा करें।")

    st.markdown("---")
    if st.button("🗑️ अभी पूरा Daily Store मैन्युअली क्लियर करें"):
        st.session_state.daily_store = _empty_daily_store()
        st.success("Daily Store साफ कर दिया गया।")
        st.rerun()


def render_alerts_tab():
    st.subheader("🔔 Live Signal & D&S Zone Alerts")
    alerts = sorted(st.session_state.alerts, key=lambda a: a["logged_at"], reverse=True)
    st.metric("कुल Active Alerts", len(alerts))
    if not alerts:
        st.info("अभी कोई अलर्ट नहीं है।")
        return
    adf = pd.DataFrame(alerts)
    if "category" not in adf.columns: adf["category"] = "—"
    adf = adf[["stars", "category", "stock", "tf", "type", "time", "logged_at", "chart"]]
    adf.columns = ["सिग्नल", "कैटेगरी", "एसेट", "टाइमफ्रेम", "टाइप", "बार टाइम", "मिला", "Chart"]
    if is_mobile_view:
        for _, row in adf.iterrows():
            st.markdown(f"**{row['सिग्नल']} · {row['एसेट']}** ({row['टाइमफ्रेम']})  \n{row['टाइप']}  \n"
                        f"⏱ {row['बार टाइम']} · [📈 Chart]({row['Chart']})")
            st.markdown("---")
    else:
        st.dataframe(adf, use_container_width=True, hide_index=True,
                    column_config={"Chart": st.column_config.LinkColumn("Chart", display_text="📈 खोलें")})
    if st.button("🗑️ सभी Alerts साफ करें"):
        st.session_state.alerts = []
        st.rerun()


def render_global_tab():
    st.subheader("🌍 Global Markets")
    global_yf_tickers = [g[2] for g in GLOBAL_INSTRUMENTS if g[2]]
    global_quotes = get_quotes(global_yf_tickers)
    ref_rows = []
    for sym, name, yft, tvs in GLOBAL_INSTRUMENTS:
        q = global_quotes.get(yft) if yft else None
        ref_rows.append({"Symbol": sym, "Name": name, "Price": f"{q['price']:.2f}" if q else "—",
                         "Change": fmt_change(q.get("chg"), q.get("pct")) if q else "—", "Chart": tv_link(tvs)})
    ref_df = pd.DataFrame(ref_rows)
    st.dataframe(style_pct_columns(ref_df, ["Change"]), use_container_width=True, hide_index=True,
                column_config={"Chart": st.column_config.LinkColumn("Chart", display_text="📈")})


def render_watchlist_tab():
    if st.button("▶️ Watchlist Load करें", key="btn_watchlist"):
        s_quotes = get_quotes([yf_ticker_for_stock(s) for s in selected_stocks])
        with st.spinner("News चेक हो रही है..."):
            news_links = fetch_news_links_parallel(selected_stocks)
        rows = []
        for s in selected_stocks:
            q = s_quotes.get(yf_ticker_for_stock(s))
            rows.append({"Stock": s, "LTP": f"{q['price']:.2f}" if q else "—",
                        "Change": fmt_change(q.get("chg"), q.get("pct")) if q else "—",
                        "Chart": tv_link(tv_symbol_for_stock(s)), "News": news_links.get(s)})
        sdf = pd.DataFrame(rows)
        st.dataframe(style_pct_columns(sdf, ["Change"]), use_container_width=True, hide_index=True,
                    column_config={"Chart": st.column_config.LinkColumn("Chart", display_text="📈"),
                                  "News": st.column_config.LinkColumn("News", display_text="📰")})
    else:
        st.info("💡 ऊपर बटन दबाएं।")


def render_sector_tab():
    if st.button("▶️ Sector Data Load करें", key="btn_sector"):
        sector_quotes = get_quotes(list(SECTOR_INDEX_TICKERS.values()))
        sec_rows = [{"Sector": name, "% Chg": f"{sector_quotes[yft]['pct']:+.2f}%" if yft in sector_quotes else "—"}
                    for name, yft in SECTOR_INDEX_TICKERS.items()]
        st.dataframe(style_pct_columns(pd.DataFrame(sec_rows), ["% Chg"]), use_container_width=True, hide_index=True)
    else:
        st.info("💡 ऊपर बटन दबाएं।")


def render_fii_tab():
    if st.button("▶️ FII/DII + Nifty Load करें", key="btn_fii"):
        fii_df, source, fii_log = update_and_get_fii_dii_log()
        if fii_df is not None:
            st.dataframe(fii_df, use_container_width=True, hide_index=True)
            insight = fii_dii_insight(fii_df)
            if insight: getattr(st, insight[0])(insight[1])
            st.caption(f"Source: {source}")
        if len(fii_log) > 1:
            with st.expander(f"📈 आज का FII/DII Trail ({len(fii_log)} स्नैपशॉट्स)"):
                st.dataframe(pd.DataFrame(fii_log), use_container_width=True, hide_index=True)
        oc_data = fetch_nse_json("/api/option-chain-indices?symbol=NIFTY")
        if oc_data:
            try:
                records, spot = oc_data["records"]["data"], oc_data["records"]["underlyingValue"]
                call_oi = {r["strikePrice"]: r["CE"]["openInterest"] for r in records if "CE" in r}
                put_oi = {r["strikePrice"]: r["PE"]["openInterest"] for r in records if "PE" in r}
                pcr = round(sum(put_oi.values()) / sum(call_oi.values()), 2) if call_oi else None
                st.metric("Nifty Spot", f"{spot:.2f}")
                c1, c2, c3 = st.columns(3)
                c1.metric("PCR", pcr or "—")
                c2.metric("Resistance", max(call_oi, key=call_oi.get) if call_oi else "—")
                c3.metric("Support", max(put_oi, key=put_oi.get) if put_oi else "—")
            except Exception: st.warning("Option data parse नहीं हो पाया।")
    else:
        st.info("💡 ऊपर बटन दबाएं।")


def render_calendar_tab():
    if st.button("▶️ Calendar Load करें", key="btn_cal"):
        components.html("""<div class="tradingview-widget-container">
          <div class="tradingview-widget-container__widget"></div>
          <script src="https://s3.tradingview.com/external-embedding/embed-widget-events.js" async>
          {"colorTheme":"light","isTransparent":false,"width":"100%","height":"600","locale":"en",
           "importanceFilter":"0,1","countryFilter":"us,in,cn,jp,gb,eu"}</script></div>""", height=620)
    else:
        st.info("💡 ऊपर बटन दबाएं।")


def render_movers_tab():
    if st.button("▶️ Gainers/Losers Load करें", key="btn_mov"):
        quotes = get_quotes([yf_ticker_for_stock(s) for s in selected_stocks])
        mv_rows = []
        for s in selected_stocks:
            qd = quotes.get(yf_ticker_for_stock(s))
            if qd: mv_rows.append({"Stock": s, "LTP": qd["price"], "pct": qd["pct"], "chg": qd.get("chg"),
                                   "Chart": tv_link(tv_symbol_for_stock(s))})
        mv_df = pd.DataFrame(mv_rows)
        if not mv_df.empty:
            gainers = mv_df.sort_values("pct", ascending=False).head(5).copy()
            losers = mv_df.sort_values("pct", ascending=True).head(5).copy()
            for _df in (gainers, losers):
                _df["Change"] = _df.apply(lambda r: fmt_change(r["chg"], r["pct"]), axis=1)
                _df.drop(columns=["pct", "chg"], inplace=True)
            st.markdown("#### 🟢 Top Gainers")
            st.dataframe(style_pct_columns(gainers, ["Change"]), use_container_width=True, hide_index=True)
            st.markdown("#### 🔴 Top Losers")
            st.dataframe(style_pct_columns(losers, ["Change"]), use_container_width=True, hide_index=True)
    else:
        st.info("💡 ऊपर बटन दबाएं।")


# ==========================================
# 9. RENDER (Mobile selectbox OR Desktop tabs)
# ==========================================
if is_mobile_view:
    if section == "📊 Signals": render_signals_tab()
    elif section == "🤖 AI Hypothesis": render_ai_hypothesis_tab()
    elif section == "🗄️ Daily Log": render_daily_log_tab()
    elif section == "🔔 Alerts": render_alerts_tab()
    elif section == "🌍 Global": render_global_tab()
    elif section == "📋 Watchlist": render_watchlist_tab()
    elif section == "🏭 Sector": render_sector_tab()
    elif section == "💰 FII/DII+Nifty": render_fii_tab()
    elif section == "🗓️ Calendar": render_calendar_tab()
    elif section == "🏆 Movers": render_movers_tab()
else:
    with tabs[0]: render_signals_tab()
    with tabs[1]: render_ai_hypothesis_tab()
    with tabs[2]: render_daily_log_tab()
    with tabs[3]: render_alerts_tab()
    with tabs[4]: render_global_tab()
    with tabs[5]: render_watchlist_tab()
    with tabs[6]: render_sector_tab()
    with tabs[7]: render_fii_tab()
    with tabs[8]: render_calendar_tab()
    with tabs[9]: render_movers_tab()
