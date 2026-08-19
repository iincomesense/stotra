"""
app.py — Full Market Dashboard (Streamlit) + Incremental D&S Cache + AI Hypothesis
=====================================================================================
Institutional D&S Zones (incremental/cached scan) | EMA/Volume/RSI Signals |
Global Markets | Sector Impact | Watchlist | Economic Calendar | FII/DII + Nifty OI |
Delivery% + Bulk/Block Deals | Gainers/Losers | Evidence-Based AI Buy/Sell Hypothesis |
Mobile-Friendly UI.

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
CONTEXT_BUFFER = 40   # leg-in/base lookback के लिए सुरक्षित buffer (नए-bar scan में)

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
    यह function खुद नहीं बदला — बस इसे कम बार, छोटे data पर बुलाया जाता है।"""
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
    return f"{symbol}::{tf_key}::mtf{int(use_mtf)}"

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
