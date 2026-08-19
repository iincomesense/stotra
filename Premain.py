"""
Full Market Dashboard (Streamlit) — High-Performance Ultra-Fast Edition
==========================================================================
Global Markets + TradingView charts | Sector Index Impact | Stock
Watchlist with live-flash news | Institutional D&S Zones / EMA / Volume / RSI Signals + Alerts |
Economic Calendar | FII/DII (Analysis-driven) + Nifty Option-OI |
Delivery% (2-day compare) + Bulk/Block Deals | Gainers/Losers |
Institutional-style News & Opening Hypothesis Engine.

REFINED INSTITUTIONAL D&S ENGINE v2
------------------------------------
- Leg-In candle: strong directional close (>=70% of range) + volume/TR filters
- Base candles: small body (indecision), tight range, volume contraction,
  and price-overlap between consecutive base candles (limit-order cluster proxy)
- Leg-Out candle: explosive TR, wick-clean, and a volume CLIMAX vs 20-bar avg
  (not just vs leg-in volume) — proof that resting orders actually triggered
- Zone stays "Fresh"/"Retest" (still actionable) until price fully consumes
  (fills) the base candle range — a single wick touch does NOT invalidate it
- Optional Multi-Timeframe (MTF) No-Break Validation: re-checks the leg-in -> 
  leg-out impulse on the next-lower timeframe to confirm price never broke
  back through the zone's far boundary mid-formation (i.e. the move was
  genuinely explosive even at half timeframe, not a higher-TF illusion)
"""

import concurrent.futures
import io
import urllib.parse
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from typing import List, Optional, Tuple, Dict, Any

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

MIN_PROXIMITY_PCT = 0.005  # 0.5%
MAX_PROXIMITY_PCT = 0.010  # 1.0%

# ---- REFINED "Limit-Order-Resting-Probability" rules for base candles ----
LEG_IN_STRONG_CLOSE_PCT = 0.70   # leg-in candle close must be in top/bottom 70% of its range
BASE_MAX_BODY_RATIO     = 0.35   # base candle body <= 35% of its own range (indecision candle)
BASE_MIN_OVERLAP_PCT    = 0.50   # consecutive base candles must overlap >=50% (price cluster)
BASE_VOL_MAX_RATIO      = 1.0    # avg base volume <= leg-in volume (activity should dry up)
LEG_OUT_VOL_LOOKBACK    = 20
LEG_OUT_VOL_MULT        = 1.5    # leg-out volume >= 1.5x of last 20-bar avg volume (climax)

# ---- Optional Multi-Timeframe (MTF) No-Break Validation ----
MTF_BUFFER_ATR_MULT     = 0.15   # small ATR-based tolerance so tiny lower-TF wicks don't reject a valid zone

# ==========================================
# 2. VECTORIZED NUMPY CALCULATIONS (PURE C-SPEED)
# ==========================================
def calculate_atr_np(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    """Calculates True Range and RMA-smoothed ATR using Pure NumPy (No Pandas Overhead)"""
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
    """Calculates Pivot Highs & Lows using NumPy Arrays"""
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


# ==========================================
# 2b. REFINED BASE-CANDLE / LEG QUALITY HELPERS
# ==========================================
def _leg_in_strong_close_ok(open_v: float, high_v: float, low_v: float, close_v: float, is_bull: bool) -> bool:
    """Leg-in candle must close strongly in its own directional extreme —
    proof of directional intent, not a random/noise bar."""
    rng = high_v - low_v
    if rng <= 0:
        return False
    if is_bull:
        return ((close_v - low_v) / rng) >= LEG_IN_STRONG_CLOSE_PCT
    else:
        return ((high_v - close_v) / rng) >= LEG_IN_STRONG_CLOSE_PCT

def _base_body_ratio_ok(o_arr: np.ndarray, h_arr: np.ndarray, l_arr: np.ndarray, c_arr: np.ndarray) -> bool:
    """Every base candle must be a small-body / indecision candle — this is the
    footprint of resting limit orders absorbing supply/demand quietly."""
    rng = h_arr - l_arr
    body = np.abs(c_arr - o_arr)
    safe_rng = np.where(rng == 0, 1e-9, rng)
    ratios = body / safe_rng
    return bool(np.all(np.where(rng == 0, 0.0, ratios) <= BASE_MAX_BODY_RATIO))

def _base_overlap_ok(base_high_arr: np.ndarray, base_low_arr: np.ndarray) -> bool:
    """Consecutive base candles should overlap heavily — price staying in one
    tight cluster (where big players can accumulate orders), not drifting."""
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

def validate_mtf_no_break(lower_df: Optional[pd.DataFrame], leg_in_time, leg_out_time,
                           boundary_val: float, is_demand: bool, atr_val: float,
                           buffer_mult: float = MTF_BUFFER_ATR_MULT) -> bool:
    """
    OPTIONAL Multi-Timeframe (MTF) No-Break Validation.

    Re-examines the leg-in -> leg-out impulse window on the NEXT-LOWER
    timeframe. Confirms price never traded back through the zone's far
    boundary (the base/leg-in far edge) while the impulse was forming.
    This proves the explosive move holds up even when you "zoom in" to half
    timeframe — i.e. genuine institutional aggression, not a higher-TF
    candle-compression illusion.

    Fails OPEN (returns True) when lower-TF data isn't available/alignable,
    so a missing lower-TF fetch never silently kills otherwise-valid zones —
    it simply skips the extra confirmation for that instance.
    """
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
        min_low = float(window['Low'].min())
        return min_low >= (boundary_val - buffer)
    else:
        max_high = float(window['High'].max())
        return max_high <= (boundary_val + buffer)


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
        # Fresh  -> zone untouched, fully actionable
        # Retest -> price wicked into prox_val but did NOT fully fill the base
        #           (still actionable per user rule: only a FULL fill kills a zone)
        # Filled -> price traded all the way through to dist_val — zone is dead
        self.state = "Fresh"
        self.touch_count = 0
        self.mtf_confirmed = mtf_confirmed
        self.start_idx = start_idx


def scan_institutional_ds_zones(df: pd.DataFrame, lower_tf_df: Optional[pd.DataFrame] = None,
                                 use_mtf: bool = False) -> List[Zone]:
    """Pine Script Version 6 D&S Engine — High-Speed NumPy Vectorized Edition (Refined v2)"""
    if df is None or len(df) < 30:
        return []

    # Extract pure 1D NumPy arrays to eliminate Pandas .iloc overhead
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

            # ---- REFINED: base body ratio (indecision / resting-order footprint) ----
            body_ratio_ok = _base_body_ratio_ok(base_open_slice, base_high_slice, base_low_slice, base_close_slice)

            # ---- REFINED: base volume contraction (orders quietly accumulating) ----
            base_vol_ok = True
            if len(base_vol_slice) > 0:
                base_vol_ok = np.mean(base_vol_slice) <= (BASE_VOL_MAX_RATIO * volume[leg_in_idx])

            # ---- REFINED: base overlap (limit-order price cluster, not a drift) ----
            overlap_ok = _base_overlap_ok(base_high_slice, base_low_slice)

            # ---- REFINED: leg-in strong directional close ----
            leg_in_strong_close_ok = _leg_in_strong_close_ok(
                open_p[leg_in_idx], high[leg_in_idx], low[leg_in_idx], close[leg_in_idx], is_bull[leg_in_idx]
            )

            # ---- REFINED: leg-out volume CLIMAX vs 20-bar average (not just vs leg-in) ----
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

            # prox_val / dist_val computed early so the optional MTF check can use dist_val
            prox_val = max_base_high if is_demand_leg_out else min_base_low
            dist_val = min_base_low if is_demand_leg_out else max_base_high

            # ---- OPTIONAL: Multi-Timeframe No-Break Validation ----
            mtf_ok = True
            if use_mtf and lower_tf_df is not None:
                try:
                    leg_in_time = idx[leg_in_idx]
                    end_pos = leg_out_idx + 1 if (leg_out_idx + 1) < n else leg_out_idx
                    leg_out_time = idx[end_pos]
                    mtf_ok = validate_mtf_no_break(
                        lower_tf_df, leg_in_time, leg_out_time, dist_val, is_demand_leg_out, leg_out_atr
                    )
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

                curr_atr = leg_out_atr  # leg_out_idx == i, so atr[i] == leg_out_atr
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

        # ---- STATE MACHINE: Fresh -> Retest -> Filled ----
        # A zone stays actionable (Fresh/Retest) until price FULLY fills the
        # base range (reaches dist_val). A wick-only touch of prox_val just
        # marks it as "Retest" — still tradeable, not invalidated.
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
# 3. GLOBAL MASTER LIST & MARKET TIME SETUP
# ==========================================
IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
ALERT_CLEAR_HOUR_IST = 20
NEWS_WINDOW_START = dtime(8, 30)
NEWS_WINDOW_END = dtime(20, 30)

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
    ("SPOTCRUDE", "WTI Crude Oil",
