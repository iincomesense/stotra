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
    ("SPOTCRUDE", "WTI Crude Oil", "CL=F", "TVC:USOIL"),
    ("COPPER", "Copper", "HG=F", "COMEX:HG1!"),
    ("NATGAS", "Natural Gas", "NG=F", "NYMEX:NG1!"),
    ("ZINC", "Zinc", None, "CAPITALCOM:ZINC"),
    ("ALUMINIUM", "Aluminium", None, "CAPITALCOM:ALUMINIUM"),
    ("US30", "Dow Jones Industrial Avg", "^DJI", "TVC:DJI"),
    ("US500", "S&P 500", "^GSPC", "TVC:SPX"),
    ("000001", "Shanghai Composite (China)", "000001.SS", "SSE:000001"),
    ("XIN9", "FTSE China A50", None, "TVC:XIN9"),
    ("JP225", "Nikkei 225 (Japan)", "^N225", "TVC:NI225"),
    ("NIFTY1!", "Nifty 50 Futures", None, "NSE:NIFTY1!"),
    ("GIFTNIFTY", "GIFT Nifty 50", None, "NSEIX:NIFTY1!"),
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
html, body, [class^="css"], [class*=" css"] {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif !important;
}
[data-testid="stAppViewContainer"], [data-testid="stHeader"], .main, section.main {
  background: var(--dh-bg) !important;
}
[data-testid="stHeader"] { background: transparent !important; }
h1, h2, h3, h4 { color: var(--dh-text) !important; font-weight: 700 !important; letter-spacing: -0.01em; }
[data-testid="stCaptionContainer"] { color: var(--dh-muted) !important; font-size: 12.5px !important; }
@media (max-width: 768px) {
    .block-container {padding-left: 0.6rem; padding-right: 0.6rem; padding-top: 1rem;}
    div[data-testid="stMetricValue"] {font-size: 1.1rem;}
    h1 {font-size: 1.4rem !important;} h2, h3 {font-size: 1.1rem !important;}
}
div[data-testid="stTabs"] div[role="tablist"], .stTabs [data-baseweb="tab-list"] {
  gap: 4px !important; background: var(--dh-card) !important; padding: 6px !important;
  border-radius: 14px !important; border: 1px solid var(--dh-border) !important; overflow-x: auto;
}
div[data-testid="stTabs"] button[role="tab"], .stTabs [data-baseweb="tab"] {
  border-radius: 10px !important; padding: 9px 16px !important; font-weight: 600 !important;
  font-size: 13.5px !important; color: var(--dh-muted) !important; background: transparent !important;
}
div[data-testid="stTabs"] button[aria-selected="true"], .stTabs [aria-selected="true"] {
  background: #0B1F3A !important; color: #FFFFFF !important;
}
div[data-testid="stTabs"] [data-baseweb="tab-highlight"],
div[data-testid="stTabs"] [data-baseweb="tab-border"] { display: none !important; }
[data-testid="stDataFrame"] {
  border-radius: 12px !important; overflow: hidden; border: 1px solid var(--dh-border) !important;
}
div[data-testid="stMetric"] {
  background: var(--dh-card); border: 1px solid var(--dh-border); border-radius: 12px; padding: 14px 16px;
}
div[data-testid="stMetricValue"] { color: var(--dh-text) !important; font-weight: 800 !important; }
div[data-testid="stMetricLabel"] { color: var(--dh-muted) !important; }
[data-testid="stAlert"] { border-radius: 12px !important; }
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


# ============================== TIMEFRAMES & INDICATOR ENGINES ==============================
TIMEFRAMES = {
    "3 Min":   {"interval": "1m",  "period": "5d",  "resample": "3min",  "intraday": True},
    "5 Min":   {"interval": "5m",  "period": "5d",  "resample": None,    "intraday": True},
    "15 Min":  {"interval": "5m",  "period": "5d",  "resample": "15min", "intraday": True},
    "30 Min":  {"interval": "15m", "period": "1mo", "resample": "30min", "intraday": True},
    "75 Min":  {"interval": "15m", "period": "1mo", "resample": "75min", "intraday": True},
    "1 Hour":  {"interval": "60m", "period": "1mo", "resample": None,    "intraday": True},
    "2 Hours": {"interval": "60m", "period": "3mo", "resample": "120min", "intraday": True},
    "4 Hours": {"interval": "60m", "period": "3mo", "resample": "240min", "intraday": True},
    "6 Hours": {"interval": "60m", "period": "3mo", "resample": "360min", "intraday": True},
    "Daily":   {"interval": "1d",  "period": "6mo", "resample": None,     "intraday": False},
}

# Approximate real duration (minutes) of each timeframe — used only to derive
# the "next lower timeframe" mapping for the optional MTF validation below.
TF_MINUTES = {
    "3 Min": 3, "5 Min": 5, "15 Min": 15, "30 Min": 30,
    "1 Hour": 60, "75 Min": 75, "2 Hours": 120, "4 Hours": 240,
    "6 Hours": 360, "Daily": 1440,
}

def _build_lower_tf_map() -> Dict[str, Optional[str]]:
    ordered = sorted(TF_MINUTES.items(), key=lambda kv: kv[1])  # ascending by minutes
    mapping: Dict[str, Optional[str]] = {}
    for pos, (tf_name, _) in enumerate(ordered):
        mapping[tf_name] = ordered[pos - 1][0] if pos > 0 else None
    return mapping

# e.g. "2 Hours" -> "1 Hour", "1 Hour" -> "30 Min", "3 Min" -> None (already lowest)
LOWER_TF_MAP: Dict[str, Optional[str]] = _build_lower_tf_map()


def calc_ema_np(arr: np.ndarray, span: int) -> np.ndarray:
    """Reusable NumPy EMA calculator (extracted so 20/50 AND 3/5 can share it)."""
    alpha = 2.0 / (span + 1.0)
    res = np.empty_like(arr)
    res[0] = arr[0]
    one_minus_alpha = 1.0 - alpha
    for i in range(1, len(arr)):
        res[i] = alpha * arr[i] + one_minus_alpha * res[i - 1]
    return res

def check_ema_cross_generic(close: np.ndarray, fast: int = 20, slow: int = 50, label: str = ""):
    """Generic NumPy Optimized EMA Crossover Check for any (fast, slow) pair."""
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
    """NumPy Optimized EMA 20/50 Crossover Check"""
    return check_ema_cross_generic(close, 20, 50, "EMA20/50")

def check_ema_cross_3_5(close: np.ndarray):
    """NumPy Optimized EMA 3/5 Crossover Check — fast scalping / very-short-term intraday signal.
    Needs at least 5 bars; naturally fires far more often than 20/50, so treat it as a
    'momentum trigger' rather than a standalone trade signal — confirm with volume/RSI/D&S zone."""
    return check_ema_cross_generic(close, 3, 5, "EMA3/5")

def check_volume_spike_fast(vol: np.ndarray, mult=2.0):
    """NumPy Optimized Volume Spike Check"""
    if len(vol) < 21: return None
    avg_vol = np.mean(vol[-21:-1])
    curr_vol = vol[-1]
    if avg_vol > 0 and (curr_vol / avg_vol) >= mult:
        return f"⚡ Vol {curr_vol / avg_vol:.1f}x"
    return None

def check_rsi_fast(close: np.ndarray, period=14):
    """NumPy Optimized RSI Check"""
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

def check_ds_zones(df: pd.DataFrame, lower_tf_df: Optional[pd.DataFrame] = None, use_mtf: bool = False):
    """Institutional Demand & Supply Proximity Alert Check.
    A zone remains actionable in BOTH the 'Fresh' and 'Retest' states — per the
    rule that only a FULL fill of the base range invalidates a zone, not a
    single wick touch."""
    zones = scan_institutional_ds_zones(df, lower_tf_df=lower_tf_df, use_mtf=use_mtf)
    if not zones:
        return None, False
    current_price = df['Close'].iloc[-1]
    active_zones = [z for z in zones if z.state in ("Fresh", "Retest")]

    signals = []
    is_hq_signal = False
    for z in active_zones:
        mtf_tag = "🔬MTF✓ " if (use_mtf and z.mtf_confirmed) else ""
        retest_tag = f"(Retest#{z.touch_count}) " if z.state == "Retest" else ""
        if z.is_demand:
            diff_pct = (current_price - z.prox_val) / z.prox_val
            if MIN_PROXIMITY_PCT <= diff_pct <= MAX_PROXIMITY_PCT:
                hq_tag = "★ HQ " if z.is_hq else ""
                if z.is_hq: is_hq_signal = True
                signals.append(f"🟢 DEMAND ZONE ({hq_tag}{mtf_tag}{retest_tag}Entry: {z.prox_val:.2f}, SL: {z.sl_val:.2f}, TP: {z.tp_val:.2f}, {diff_pct*100:.2f}% away)")
        else:
            diff_pct = (z.prox_val - current_price) / z.prox_val
            if MIN_PROXIMITY_PCT <= diff_pct <= MAX_PROXIMITY_PCT:
                hq_tag = "★ HQ " if z.is_hq else ""
                if z.is_hq: is_hq_signal = True
                signals.append(f"🔴 SUPPLY ZONE ({hq_tag}{mtf_tag}{retest_tag}Entry: {z.prox_val:.2f}, SL: {z.sl_val:.2f}, TP: {z.tp_val:.2f}, {diff_pct*100:.2f}% away)")

    if signals:
        return " | ".join(signals), is_hq_signal
    return None, False

# ============================== SIDEBAR ==============================
st.sidebar.header("⚙️ Settings")
refresh_min = st.sidebar.slider("Auto-Refresh हर (मिनट)", 0.5, 15.0, 1.0, 0.5)
if HAS_AUTOREFRESH:
    st_autorefresh(interval=int(refresh_min * 60 * 1000), key="auto_refresh")

st.sidebar.markdown(f"🕒 IST: **{now_ist().strftime('%d-%b-%Y %H:%M:%S')}**")
st.sidebar.markdown("🟢 भारतीय बाज़ार खुला" if is_market_hours() else "🔴 भारतीय बाज़ार बंद (ग्लोबल चालू)")
if st.sidebar.button("🔄 अभी Refresh करें"):
    st.cache_data.clear()
    st.rerun()

selected_stocks = st.sidebar.multiselect("Indian Stock Watchlist", WATCHLIST_DEFAULT, default=WATCHLIST_DEFAULT)

st.sidebar.markdown("---")
st.sidebar.subheader("📊 Signal & Scan Settings")

scan_scope = st.sidebar.multiselect(
    "Scan Scope (सिग्नल दायरा)",
    ["Indian Watchlist", "Global Markets (Commodities/FX/Bonds/Indices)"],
    default=["Indian Watchlist", "Global Markets (Commodities/FX/Bonds/Indices)"]
)

tf_options = ["ALL"] + list(TIMEFRAMES.keys())
selected_tf_raw = st.sidebar.multiselect("Signal Scan Timeframes", tf_options, default=["3 Min", "5 Min", "1 Hour", "Daily"])

if "ALL" in selected_tf_raw:
    signal_timeframes = list(TIMEFRAMES.keys())
else:
    signal_timeframes = selected_tf_raw

selected_indicators = st.sidebar.multiselect(
    "इंडिकेटर चुनें (Signals)",
    ["Institutional D&S Zones (Demand/Supply)", "EMA Crossover (20/50)", "EMA Crossover (3/5)", "Volume Spike", "RSI (14)"],
    default=["Institutional D&S Zones (Demand/Supply)", "EMA Crossover (20/50)", "EMA Crossover (3/5)", "Volume Spike"]
)

vol_mult = st.sidebar.slider("Volume Spike Multiplier", 1.5, 5.0, 2.0, 0.5)

st.sidebar.markdown("---")
st.sidebar.subheader("🔬 High-Validation D&S Filters (Optional)")
use_mtf_validation = st.sidebar.checkbox(
    "Multi-Timeframe No-Break Validation (सख्त)",
    value=False,
    help=("जब ON हो: हर D&S zone को उसके अगले-नीचे टाइमफ्रेम पर दोबारा जांचा जाता है — "
          "leg-in से leg-out तक के बीच price zone की far-boundary को cross नहीं करना चाहिए। "
          "उदाहरण: 2 Hours का zone 1 Hour पर वैलिडेट होगा, 1 Hour का zone 30 Min पर, आदि। "
          "इससे कम zones मिलेंगी पर हर zone ज़्यादा भरोसेमंद होगी। '3 Min' टाइमफ्रेम के लिए यह "
          "check skip होगा (उससे नीचे कोई TF उपलब्ध नहीं है)।")
)
if use_mtf_validation:
    st.sidebar.caption("🔬 MTF validation ON — scan थोड़ा धीमा होगा (हर TF के लिए एक निचला TF भी fetch होगा)।")

# ============================== ALERTS STATE ==============================
if "alerts" not in st.session_state:
    st.session_state.alerts = []
if "alerts_clear_date" not in st.session_state:
    st.session_state.alerts_clear_date = None

def maybe_clear_alerts():
    today = now_ist().date()
    if now_ist().time() >= dtime(ALERT_CLEAR_HOUR_IST, 0):
        if st.session_state.alerts_clear_date != today:
            st.session_state.alerts = []
            st.session_state.alerts_clear_date = today

maybe_clear_alerts()

# ============================== UNIFIED SINGLE DOWNLOAD ENGINE ==============================
@st.cache_data(ttl=180, show_spinner=False)
def unified_yf_download_engine(tickers_tuple: Tuple[str, ...], period: str = "10d", interval: str = "1d") -> Dict[str, pd.DataFrame]:
    """Unified Single Batch Download Engine for all YFinance Requests"""
    tickers = list(tickers_tuple)
    if not tickers: return {}
    try:
        data = yf.download(tickers, period=period, interval=interval, group_by="ticker", progress=False, threads=True)
    except Exception: return {}

    out = {}
    for t in tickers:
        try:
            df = data[t].dropna() if len(tickers) > 1 else data.dropna()
            if not df.empty:
                out[t] = df
        except Exception: continue
    return out

def get_quotes(tickers: List[str]) -> Dict[str, Dict[str, Any]]:
    """Fetches Live and Daily Quotes using Unified Download Engine"""
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

# ============================== FAST PARALLEL TIMEFRAME SCANNER ==============================
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

# ============================== OTHER API HELPERS ==============================
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
        if fii_net > 0 and dii_net > 0:
            return "success", f"🟢 FII (₹{fii_net:+.0f} Cr) और DII (₹{dii_net:+.0f} Cr) दोनों खरीदार — Bullish bias।"
        if fii_net < 0 and dii_net > 0:
            return "info", f"🔵 FII बिकवाली (₹{fii_net:+.0f} Cr) पर DII (₹{dii_net:+.0f} Cr) सपोर्ट दे रहे हैं।"
        if fii_net > 0 and dii_net < 0:
            return "info", f"🔵 FII खरीदारी (₹{fii_net:+.0f} Cr) कर रहे, DII बेच रहे (₹{dii_net:+.0f} Cr)।"
        if fii_net < 0 and dii_net < 0:
            return "error", f"🔴 FII (₹{fii_net:+.0f} Cr) और DII (₹{dii_net:+.0f} Cr) दोनों बिकवाल — Cautious bias।"
        return None
    except Exception: return None

@st.cache_data(ttl=3600 * 6, show_spinner=False)
def fetch_bhavcopy(date_str_ddmmyyyy):
    url = f"https://archives.nseindia.com/products/content/sec_bhavdata_full_{date_str_ddmmyyyy}.csv"
    try:
        r = requests.get(url, headers=NSE_HEADERS, timeout=10)
        if r.status_code == 200 and "SYMBOL" in r.text[:300].upper():
            df = pd.read_csv(io.StringIO(r.text))
            df.columns = [c.strip() for c in df.columns]
            return df
    except Exception: pass
    return None

def get_last_n_trading_bhavcopies(n=2, lookback_days=15):
    results = []
    cursor = now_ist().date() - timedelta(days=1)
    tries = 0
    while len(results) < n and tries < lookback_days:
        df = fetch_bhavcopy(cursor.strftime("%d%m%Y"))
        if df is not None: results.append((cursor, df))
        cursor -= timedelta(days=1)
        tries += 1
    return list(reversed(results))

def get_delivery_2day_compare(stocks):
    data = get_last_n_trading_bhavcopies(2)
    if len(data) < 2: return None, None
    (date1, df1), (date2, df2) = data[0], data[1]
    def deliv_col(df):
        cols = [c for c in df.columns if "DELIV_PER" in c.upper()]
        return cols[0] if cols else None

    dcol1, dcol2 = deliv_col(df1), deliv_col(df2)
    if not dcol1 or not dcol2: return date2, None

    rows = []
    for stock in stocks:
        try:
            r1 = df1[(df1["SYMBOL"].astype(str).str.strip() == stock) & (df1["SERIES"].astype(str).str.strip() == "EQ")]
            r2 = df2[(df2["SYMBOL"].astype(str).str.strip() == stock) & (df2["SERIES"].astype(str).str.strip() == "EQ")]
            if r1.empty or r2.empty: continue
            v1 = float(str(r1.iloc[0][dcol1]).strip())
            v2 = float(str(r2.iloc[0][dcol2]).strip())
            rows.append({
                "Stock": stock, date1.strftime("%d-%b"): round(v1, 2),
                date2.strftime("%d-%b (नया)"): round(v2, 2), "बदलाव": round(v2 - v1, 2),
                "Chart": tv_link(tv_symbol_for_stock(stock)),
            })
        except Exception: continue
    return date2, rows

@st.cache_data(ttl=900, show_spinner=False)
def fetch_bulk_block_deals():
    return fetch_nse_json("/api/snapshot-capital-market-largedeals")

def filter_deals_for_watchlist(deals_list, stocks):
    if not deals_list: return pd.DataFrame()
    df = pd.DataFrame(deals_list)
    symbol_col = next((c for c in ["BD_SYMBOL", "symbol", "SYMBOL", "clientSymbol"] if c in df.columns), None)
    if symbol_col is None: return df
    return df[df[symbol_col].astype(str).str.strip().isin(stocks)]

@st.cache_data(ttl=1800, show_spinner=False)
def fetch_economic_event_count_today():
    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", headers=NSE_HEADERS, timeout=10)
        events, today, count = r.json(), now_ist().date(), 0
        for e in events:
            if str(e.get("impact", "")).lower() not in ("high", "medium"): continue
            try: ev_date = datetime.fromisoformat(e.get("date").replace("Z", "+00:00")).astimezone(IST).date()
            except Exception: continue
            if ev_date == today: count += 1
        return count
    except Exception: return None

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_stock_quick_news_link_live(stock_name: str):
    """Google News API Fetcher with High TTL (1 Hr Cache) & Lazy Load"""
    if feedparser is None: return None
    query = urllib.parse.quote_plus(f"{stock_name} NSE when:1d")
    url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
    try:
        feed = feedparser.parse(requests.get(url, timeout=8).content)
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
            items.append({
                "symbol": d.get("symbol", ""),
                "subject": d.get("desc") or d.get("subject") or "",
                "attachment": d.get("attchmntFile", ""),
                "time": d.get("an_dt") or d.get("sort_date") or "",
            })
        except Exception: continue
    return items

# ==========================================
# 4. AI HYPOTHESIS ENGINE (STOCK-LEVEL BUY/SELL FROM LIVE ALERTS + PCR + MACRO)
# ==========================================
import re as _re

# Timeframe confidence weight — higher timeframe signal = more institutional weight
TF_CONF_WEIGHT = {
    "3 Min": 0.5, "5 Min": 0.6, "15 Min": 0.8, "30 Min": 1.0, "75 Min": 1.0,
    "1 Hour": 1.3, "2 Hours": 1.5, "4 Hours": 1.8, "6 Hours": 2.0, "Daily": 2.5,
}
# Signal-quality star weight (matches the "सिग्नल" labels used in Signals/Alerts tabs)
STARS_WEIGHT = {"🚀 HQ Zone": 3.0, "🔥 Vol Spike": 1.5, "⭐⭐ Strong": 2.0, "⭐ Signal": 1.0}


def _extract_zone_levels(type_str: str) -> Optional[Dict[str, float]]:
    """Pulls Entry/SL/TP numbers out of a D&S zone alert string, if present."""
    m = _re.search(r"Entry:\s*([\d.]+),\s*SL:\s*([\d.]+),\s*TP:\s*([\d.]+)", type_str)
    if not m:
        return None
    try:
        return {"entry": float(m.group(1)), "sl": float(m.group(2)), "tp": float(m.group(3))}
    except Exception:
        return None


def score_stock_alerts(alerts: list) -> Dict[str, Dict[str, Any]]:
    """
    Aggregates every live alert (D&S zones, EMA cross 20/50 & 3/5, Vol spike, RSI)
    per stock/asset into a net bullish vs bearish score, with multi-timeframe
    (MTF) confluence and signal-quality weighting baked in.
    """
    per_stock: Dict[str, Dict[str, Any]] = {}

    for a in alerts:
        stock = a["stock"]
        tf = a.get("tf", "")
        typ = a.get("type", "")
        stars = a.get("stars", "⭐ Signal")
        cat = a.get("category", "")
        chart = a.get("chart", "")

        rec = per_stock.setdefault(stock, {
            "bull": 0.0, "bear": 0.0, "reasons_bull": [], "reasons_bear": [],
            "timeframes": set(), "category": cat, "chart": chart, "zone": None,
            "hq": False,
        })
        rec["timeframes"].add(tf)
        rec["chart"] = chart
        w = TF_CONF_WEIGHT.get(tf, 1.0) * STARS_WEIGHT.get(stars, 1.0)

        for p in [x.strip() for x in typ.split("|")]:
            pu = p.upper()
            if "DEMAND ZONE" in pu:
                rec["bull"] += w * 1.5
                rec["reasons_bull"].append(f"{tf}: Demand Zone")
                if "★ HQ" in p:
                    rec["hq"] = True
                zl = _extract_zone_levels(p)
                if zl:
                    rec["zone"] = {**zl, "side": "BUY"}
            elif "SUPPLY ZONE" in pu:
                rec["bear"] += w * 1.5
                rec["reasons_bear"].append(f"{tf}: Supply Zone")
                if "★ HQ" in p:
                    rec["hq"] = True
                zl = _extract_zone_levels(p)
                if zl:
                    rec["zone"] = {**zl, "side": "SELL"}
            elif "EMA20/50 UP" in pu:
                rec["bull"] += w
                rec["reasons_bull"].append(f"{tf}: EMA20/50 बुलिश क्रॉस")
            elif "EMA20/50 DOWN" in pu:
                rec["bear"] += w
                rec["reasons_bear"].append(f"{tf}: EMA20/50 बेयरिश क्रॉस")
            elif "EMA3/5 UP" in pu:
                rec["bull"] += w * 0.6
                rec["reasons_bull"].append(f"{tf}: EMA3/5 मोमेंटम अप")
            elif "EMA3/5 DOWN" in pu:
                rec["bear"] += w * 0.6
                rec["reasons_bear"].append(f"{tf}: EMA3/5 मोमेंटम डाउन")
            elif "RSI OS" in pu:
                rec["bull"] += w * 0.7
                rec["reasons_bull"].append(f"{tf}: RSI Oversold (रिवर्सल संभावना)")
            elif "RSI OB" in pu:
                rec["bear"] += w * 0.7
                rec["reasons_bear"].append(f"{tf}: RSI Overbought (करेक्शन संभावना)")
            elif "VOL" in pu and "X" in pu:
                # A volume spike is directionless on its own — it just confirms
                # whichever side already dominates that timeframe's signal mix.
                if rec["bull"] >= rec["bear"]:
                    rec["bull"] += w * 0.4
                    rec["reasons_bull"].append(f"{tf}: Volume Spike (कन्फर्मेशन)")
                else:
                    rec["bear"] += w * 0.4
                    rec["reasons_bear"].append(f"{tf}: Volume Spike (कन्फर्मेशन)")

    return per_stock


def build_ai_hypothesis(alerts: list, pcr_value: Optional[float], top_n: int = 5
                         ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Combines per-stock alert scoring (D&S Zones + EMA + Volume + RSI, weighted by
    timeframe & signal quality) with the broader Nifty PCR/OI tilt to rank
    High-Probability BUY-side and SELL-side hypothesis candidates.
    """
    per_stock = score_stock_alerts(alerts)

    market_tilt = 0.0
    if pcr_value is not None:
        if pcr_value > 1.1:
            market_tilt = 0.3   # heavy put-writing -> broad bullish tilt
        elif pcr_value < 0.8:
            market_tilt = -0.3  # heavy call-writing -> broad bearish tilt

    buy_list, sell_list = [], []
    for stock, rec in per_stock.items():
        net = rec["bull"] - rec["bear"] + market_tilt
        confluence = len(rec["timeframes"])
        confidence = round(min(99, (abs(net) * 12) + confluence * 6 + (15 if rec["hq"] else 0)), 0)

        entry = {
            "Stock": stock, "Category": rec["category"], "Net Score": round(net, 2),
            "Confidence %": confidence, "MTF Confluence": confluence,
            "HQ Zone": "✅" if rec["hq"] else "—",
            "Reasons": rec["reasons_bull"] if net > 0 else rec["reasons_bear"],
            "Zone": rec["zone"], "Chart": rec["chart"],
        }
        if net > 0.3:
            buy_list.append(entry)
        elif net < -0.3:
            sell_list.append(entry)

    buy_list.sort(key=lambda x: x["Net Score"], reverse=True)
    sell_list.sort(key=lambda x: x["Net Score"])

    return buy_list[:top_n], sell_list[:top_n]


# ============================== TABS ORDER ==============================
# Signals, Alerts, Global, News: AUTO-REFRESH ALWAYS ACTIVE
# Sector, Watchlist, Calendar, FII/DII, Delivery, Gainers/Losers: DEFERRED EXECUTION ON TOUCH
(tab_signals, tab_alerts, tab_hypothesis, tab_global, tab_news, tab_sector, tab_stocks,
 tab_calendar, tab_fii, tab_delivery, tab_movers) = st.tabs([
    "📊 Signals", "🔔 Alerts", "🎯 Buy/Sell Hypothesis", "🌍 Global", "📰 News & AI Hypothesis",
    "🏭 Sector Impact", "📋 Watchlist", "🗓️ Calendar", "💰 FII/DII+Nifty",
    "📦 Delivery%+Deals", "🏆 Gainers/Losers"
])

# ---------- TAB 1: FAST EMA/VOLUME/RSI & INSTITUTIONAL D&S SIGNALS (AUTO-REFRESH) ----------
with tab_signals:
    st.subheader("📊 Institutional D&S + Technical Multi-Asset Scanner")

    is_after_close = now_ist().hour >= 16 or now_ist().hour < 8
    if is_after_close:
        st.info("🌙 भारतीय बाज़ार बंद है — भारतीय स्टॉक्स के लिए Daily स्कैन और **ग्लोबल मार्केट (Gold, Crude, Forex, US Yields, Global Indices)** के लिए Live Multi-timeframe स्कैन चालू है।")

    all_scan_items = []

    if "Indian Watchlist" in scan_scope:
        for s in selected_stocks:
            all_scan_items.append((s, yf_ticker_for_stock(s), tv_symbol_for_stock(s), "🇮🇳 Stock"))

    if "Global Markets (Commodities/FX/Bonds/Indices)" in scan_scope:
        for sym, name, yft, tvs in GLOBAL_INSTRUMENTS:
            if yft:
                all_scan_items.append((f"{sym} ({name})", yft, tvs, "🌍 Global"))

    if not signal_timeframes or not all_scan_items:
        st.warning("कृपया साइडबार से कम से कम एक Timeframe और Scope सलेक्ट करें।")
    else:
        # ---- Determine which extra "lower" timeframes must ALSO be fetched
        #      when MTF No-Break Validation is enabled ----
        required_tfs = set(signal_timeframes)
        if use_mtf_validation:
            for tf_key in signal_timeframes:
                lower_tf = LOWER_TF_MAP.get(tf_key)
                if lower_tf:
                    required_tfs.add(lower_tf)

        with st.spinner("⚡ Fast Scanning (Demand/Supply Zones + EMA/Volume/RSI) चल रहा है..."):
            all_tf_data = fetch_all_tf_data_fast_v2(tuple(required_tfs), tuple(all_scan_items))

        rows = []
        existing_keys = {a["key"] for a in st.session_state.alerts}

        for tf_key in signal_timeframes:
            tf_is_intraday = TIMEFRAMES[tf_key]["intraday"]
            tf_data = all_tf_data.get(tf_key, {})
            lower_tf_key = LOWER_TF_MAP.get(tf_key) if use_mtf_validation else None
            lower_tf_data_for_this_tf = all_tf_data.get(lower_tf_key, {}) if lower_tf_key else {}

            for item_name, item_dict in tf_data.items():
                cat = item_dict["category"]

                if is_after_close and tf_is_intraday and cat == "🇮🇳 Stock":
                    continue

                df = item_dict["df"]
                tv_sym = item_dict["tv"]
                price, bar_time = df["Close"].iloc[-1], df.index[-1]

                # NumPy High-Speed Array Conversion
                close_np = df["Close"].to_numpy(dtype=np.float64)
                vol_np = df["Volume"].to_numpy(dtype=np.float64)

                type_parts = []
                is_daily_vol_spike = False
                is_hq_ds_zone = False

                # 1. Institutional D&S Zone Scanner (Refined v2, optional MTF validation)
                if "Institutional D&S Zones (Demand/Supply)" in selected_indicators:
                    lower_item = lower_tf_data_for_this_tf.get(item_name)
                    lower_df = lower_item["df"] if lower_item else None
                    ds_sig, is_hq = check_ds_zones(df, lower_tf_df=lower_df, use_mtf=use_mtf_validation)
                    if ds_sig:
                        type_parts.append(ds_sig)
                        if is_hq: is_hq_ds_zone = True

                # 2. EMA Crossover (20/50)
                if "EMA Crossover (20/50)" in selected_indicators:
                    cross = check_ema_cross_fast(close_np)
                    if cross: type_parts.append(cross)

                # 2b. EMA Crossover (3/5) — fast scalping trigger
                if "EMA Crossover (3/5)" in selected_indicators:
                    cross_35 = check_ema_cross_3_5(close_np)
                    if cross_35: type_parts.append(cross_35)

                # 3. Volume Spike
                if "Volume Spike" in selected_indicators:
                    vr = check_volume_spike_fast(vol_np, vol_mult)
                    if vr:
                        type_parts.append(vr)
                        if tf_key == "Daily": is_daily_vol_spike = True

                # 4. RSI
                if "RSI (14)" in selected_indicators:
                    rsi_sig = check_rsi_fast(close_np)
                    if rsi_sig: type_parts.append(rsi_sig)

                if not type_parts: continue

                if is_hq_ds_zone: stars = "🚀 HQ Zone"
                elif is_daily_vol_spike: stars = "🔥 Vol Spike"
                elif len(type_parts) >= 2: stars = "⭐⭐ Strong"
                else: stars = "⭐ Signal"

                bar_time_str = bar_time.strftime("%H:%M %d-%b")

                rows.append({
                    "सिग्नल": stars,
                    "कैटेगरी": cat,
                    "एसेट": item_name,
                    "टाइमफ्रेम": tf_key,
                    "टाइप": " | ".join(type_parts),
                    "LTP": round(price, 2),
                    "समय": bar_time_str,
                    "Chart": tv_link(tv_sym),
                })

                alert_key = f"{item_name}|{tf_key}|{'|'.join(type_parts)}|{bar_time_str}"
                if alert_key not in existing_keys:
                    st.session_state.alerts.append({
                        "key": alert_key, "stock": item_name, "category": cat, "tf": tf_key,
                        "type": " | ".join(type_parts), "stars": stars, "time": bar_time_str,
                        "logged_at": now_ist().strftime("%H:%M:%S"), "chart": tv_link(tv_sym),
                    })
                    existing_keys.add(alert_key)

        if not rows:
            st.success("चुने गए इंडिकेटर्स/टाइमफ्रेम पर अभी कोई नया सिग्नल या D&S zone proximity नहीं मिली।")
        else:
            sig_df = pd.DataFrame(rows)
            sort_rank = {"🚀 HQ Zone": 4, "🔥 Vol Spike": 3, "⭐⭐ Strong": 2, "⭐ Signal": 1}
            sig_df["_sort"] = sig_df["सिग्नल"].map(lambda x: sort_rank.get(x, 0))
            sig_df = sig_df.sort_values(["_sort", "समय"], ascending=[False, False]).drop(columns="_sort")

            def hl(row):
                if "HQ Zone" in row["सिग्नल"]: base = "background-color:#d1e7dd; font-weight:bold;"
                elif row["सिग्नल"] == "🔥 Vol Spike": base = f"background-color:{COLOR_SPIKE_BG}"
                elif row["सिग्नल"] == "⭐⭐ Strong": base = "background-color:#e8d4f8"
                elif "DEMAND" in row["टाइप"] or "UP" in row["टाइप"]: base = f"background-color:{COLOR_POS_BG}"
                elif "SUPPLY" in row["टाइप"] or "DOWN" in row["टाइप"]: base = f"background-color:{COLOR_NEG_BG}"
                else: base = "background-color:#fff2cc"
                return [base] * len(row)

            st.dataframe(
                sig_df.style.apply(hl, axis=1), use_container_width=True, hide_index=True,
                column_config={"Chart": st.column_config.LinkColumn("Chart", display_text="📈 खोलें")},
            )

# ---------- TAB 2: ALERTS (AUTO-REFRESH) ----------
with tab_alerts:
    st.subheader("🔔 Live Signal & D&S Zone Alerts")
    alerts = sorted(st.session_state.alerts, key=lambda a: a["logged_at"], reverse=True)
    st.metric("कुल Active Alerts", len(alerts))
    if not alerts:
        st.info("अभी कोई अलर्ट नहीं है। Signals टैब में D&S Zone या टेक्निकल सिग्नल मिलते ही यहां जुड़ जाएगा।")
    else:
        adf = pd.DataFrame(alerts)
        if "category" not in adf.columns: adf["category"] = "—"
        adf = adf[["stars", "category", "stock", "tf", "type", "time", "logged_at", "chart"]]
        adf.columns = ["सिग्नल", "कैटेगरी", "एसेट/स्टॉक", "टाइमफ्रेम", "टाइप", "बार टाइम", "Alert मिला", "Chart"]

        def hl_alert(row):
            if "HQ Zone" in row["सिग्नल"]: base = "background-color:#d1e7dd; font-weight:bold;"
            elif "Vol Spike" in row["सिग्नल"]: base = f"background-color:{COLOR_SPIKE_BG}"
            elif "DEMAND" in row["टाइप"] or "UP" in row["टाइप"]: base = f"background-color:{COLOR_POS_BG}"
            elif "SUPPLY" in row["टाइप"] or "DOWN" in row["टाइप"]: base = f"background-color:{COLOR_NEG_BG}"
            else: base = "background-color:#fff2cc"
            return [base] * len(row)

        st.dataframe(
            adf.style.apply(hl_alert, axis=1), use_container_width=True, hide_index=True,
            column_config={"Chart": st.column_config.LinkColumn("Chart", display_text="📈 खोलें")},
        )
        if st.button("🗑️ सभी Alerts अभी साफ करें"):
            st.session_state.alerts = []
            st.rerun()

# ---------- TAB 2b: HIGH-PROBABILITY AI BUY/SELL HYPOTHESIS (DEFERRED LOAD ON TOUCH) ----------
with tab_hypothesis:
    st.subheader("🎯 High-Probability AI Hypothesis — BUY साइड बनाम SELL साइड")
    st.caption(
        "📊 Signals टैब में जमा हुए सभी Live Alerts (D&S Zones, EMA 20/50, EMA 3/5, Volume Spike, RSI) "
        "को Multi-Timeframe Confluence + Signal Quality के हिसाब से वेट कर, Nifty Option PCR/OI और "
        "ग्लोबल मैक्रो सेंटीमेंट के साथ कॉम्बाइन किया गया है। यह ट्रेड सलाह नहीं है — केवल डेटा-ड्रिवन हाइपोथिसिस है, "
        "एंट्री लेने से पहले अपनी रिसर्च और रिस्क मैनेजमेंट ज़रूर करें।"
    )

    if st.button("▶️ AI Hypothesis बनाएं (Signals + PCR + Global Macro Analyze करें)", key="btn_hypothesis"):
        if not st.session_state.alerts:
            st.warning("अभी कोई Live Alert मौजूद नहीं है। पहले 📊 Signals टैब खोलें ताकि Alerts जनरेट हों, फिर यहां वापस आकर हाइपोथिसिस बनाएं।")
        else:
            with st.spinner("⚡ Nifty PCR/OI, Global Macro Cues और सभी Live Alerts को कॉम्बाइन कर हाइपोथिसिस तैयार हो रहा है..."):
                # 1. Nifty Option Chain -> PCR + Support/Resistance strikes
                oc_data = fetch_nse_json("/api/option-chain-indices?symbol=NIFTY")
                pcr_value, max_call_oi_strike, max_put_oi_strike = None, None, None
                if oc_data:
                    try:
                        records = oc_data["records"]["data"]
                        total_call_oi = sum(r["CE"]["openInterest"] for r in records if "CE" in r)
                        total_put_oi = sum(r["PE"]["openInterest"] for r in records if "PE" in r)
                        if total_call_oi > 0:
                            pcr_value = round(total_put_oi / total_call_oi, 2)
                        call_oi_map = {r["strikePrice"]: r["CE"]["openInterest"] for r in records if "CE" in r}
                        put_oi_map = {r["strikePrice"]: r["PE"]["openInterest"] for r in records if "PE" in r}
                        if call_oi_map:
                            max_call_oi_strike = max(call_oi_map, key=call_oi_map.get)
                        if put_oi_map:
                            max_put_oi_strike = max(put_oi_map, key=put_oi_map.get)
                    except Exception:
                        pass

                # 2. Global Macro Cues (reuses cached quotes engine — no duplicate network cost)
                macro_symbols = [g[2] for g in GLOBAL_INSTRUMENTS if g[2]]
                macro_quotes = get_quotes(macro_symbols)
                sp500_pct = macro_quotes.get("^GSPC", {}).get("pct", 0)
                crude_pct = macro_quotes.get("CL=F", {}).get("pct", 0)
                dxy_pct = macro_quotes.get("DX-Y.NYB", {}).get("pct", 0)

                if sp500_pct > 0.3 and crude_pct < 0:
                    macro_bias_text = "🟢 ग्लोबल सेंटीमेंट BUY-साइड के पक्ष में झुका हुआ है (US बुलिश + Crude नरम)"
                elif sp500_pct < -0.3 or dxy_pct > 0.3:
                    macro_bias_text = "🔴 ग्लोबल सेंटीमेंट SELL-साइड के पक्ष में झुका हुआ है (US कमज़ोर / डॉलर मज़बूत)"
                else:
                    macro_bias_text = "🟡 ग्लोबल सेंटीमेंट न्यूट्रल है — कोई स्ट्रॉन्ग मैक्रो बायस नहीं"

                # 3. Combine everything -> ranked BUY/SELL hypothesis lists
                buy_list, sell_list = build_ai_hypothesis(st.session_state.alerts, pcr_value, top_n=5)

            # ---- Market Context Banner ----
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Nifty PCR (OI)", pcr_value if pcr_value is not None else "—")
            c2.metric("S&P500", f"{sp500_pct:+.2f}%")
            c3.metric("Crude Oil", f"{crude_pct:+.2f}%")
            c4.metric("DXY", f"{dxy_pct:+.2f}%")
            st.info(f"{macro_bias_text} · Demand Support ~{max_put_oi_strike or 'N/A'} · Supply Resistance ~{max_call_oi_strike or 'N/A'}")

            st.markdown("---")

            def render_hypothesis_side(title, items, side_bg):
                st.markdown(f"### {title}")
                if not items:
                    st.caption("अभी इस साइड के लिए कोई हाई-प्रोबेबिलिटी उम्मीदवार नहीं मिला — या तो अलर्ट्स कम हैं या सिग्नल मिक्स-न्यूट्रल है।")
                    return
                for it in items:
                    zone = it["Zone"]
                    zone_txt = ""
                    if zone:
                        zone_txt = f" · <b>Entry:</b> {zone['entry']:.2f} · <b>SL:</b> {zone['sl']:.2f} · <b>TP:</b> {zone['tp']:.2f}"
                    reasons_txt = " • ".join(it["Reasons"][:4]) if it["Reasons"] else "General Multi-Signal Confluence"
                    hq_txt = " · 🚀 HQ Zone" if it["HQ Zone"] == "✅" else ""
                    st.markdown(
                        f"""
                        <div style="background-color:{side_bg}; border-radius:10px; padding:12px 16px; margin-bottom:10px; border:1px solid #e0e0e0;">
                            <b style="font-size:15px;">{it['Stock']}</b> &nbsp;
                            <span style="font-size:12px; color:#555;">({it['Category']})</span>
                            <span style="float:right; font-weight:bold;">Confidence: {it['Confidence %']:.0f}%{hq_txt}</span>
                            <br><span style="font-size:13px;">Net Score: {it['Net Score']} · MTF Confluence: {it['MTF Confluence']} टाइमफ्रेम{zone_txt}</span>
                            <br><span style="font-size:12.5px; color:#333;">📌 {reasons_txt}</span>
                            <br><a href="{it['Chart']}" target="_blank" style="font-size:12.5px;">📈 Chart देखें</a>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )

            col_buy, col_sell = st.columns(2)
            with col_buy:
                render_hypothesis_side("🟢 High-Probability BUY साइड", buy_list, "#e6f4ea")
            with col_sell:
                render_hypothesis_side("🔴 High-Probability SELL साइड", sell_list, "#fce8e6")

            st.caption(
                "⚠️ यह हाइपोथिसिस पूरी तरह ऑटोमेटेड सिग्नल कॉम्बिनेशन (D&S Zones + EMA + Volume + RSI + Nifty PCR + "
                "Global Macro Cues) पर आधारित है — निवेश सलाह नहीं है। एंट्री से पहले करेंट न्यूज़/इवेंट्स खुद वेरीफाई करें।"
            )
    else:
        st.info("💡 📊 Signals टैब में कुछ Alerts जनरेट होने के बाद, ऊपर **▶️ AI Hypothesis बनाएं** बटन पर क्लिक करें ताकि हाई-प्रोबेबिलिटी Buy/Sell साइड यहां दिखे।")

# ---------- TAB 3: GLOBAL MARKETS (AUTO-REFRESH) ----------
with tab_global:
    st.subheader("🌍 Global Markets")
    ticker_items = ",".join(
        '{"proName": "%s", "title": "%s"}' % (tvs, sym) for sym, _, _, tvs in GLOBAL_INSTRUMENTS
    )
    components.html(f"""
        <div class="tradingview-widget-container">
          <div class="tradingview-widget-container__widget"></div>
          <script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-ticker-tape.js" async>
          {{"symbols": [{ticker_items}], "showSymbolLogo": true, "isTransparent": false, "displayMode": "adaptive", "colorTheme": "light", "locale": "en"}}
          </script>
        </div>""", height=80)

    st.markdown("&nbsp;")
    st.markdown("**Price, बदलाव और live chart:** &nbsp; 🟢▲ = ऊपर · 🔴▼ = नीचे")
    global_yf_tickers = [g[2] for g in GLOBAL_INSTRUMENTS if g[2]]
    global_quotes = get_quotes(global_yf_tickers)
    ref_rows = []
    for sym, name, yft, tvs in GLOBAL_INSTRUMENTS:
        q = global_quotes.get(yft) if yft else None
        ref_rows.append({
            "Symbol": sym, "Name": name,
            "Price": f"{q['price']:.2f}" if q else "—",
            "Change": fmt_change(q.get("chg"), q.get("pct")) if q else "—",
            "Chart": tv_link(tvs),
        })
    ref_df = pd.DataFrame(ref_rows)
    st.dataframe(
        style_pct_columns(ref_df, ["Change"]), use_container_width=True, hide_index=True,
        column_config={"Chart": st.column_config.LinkColumn("Chart", display_text="📈 Live Chart खोलें")},
    )

    # ---------- TAB 4: ADVANCED AI MARKET INTELLIGENCE & HYPOTHESIS ENGINE (AUTO-REFRESH) ----------
with tab_news:
    st.subheader("🤖 Real-Time AI Market Intelligence & Opening Hypothesis Engine")
    st.caption("Live Price/Volume + Option OI/PCR + Macro Drivers + Top 10 Stocks OI + FII/DII Trends + Global Cues")

    with st.spinner("🤖 AI Engine मल्टी-स्ट्रीम डेटा (ग्लोबल, FII/DII, OI/PCR, टॉप स्टॉक्स) एनालाइज कर रहा है..."):
        # 1. Macro Quotes Fetching
        macro_symbols = [g[2] for g in GLOBAL_INSTRUMENTS if g[2]]
        macro_quotes = get_quotes(macro_symbols) if 'get_quotes' in globals() else {}

        usdinr_data = macro_quotes.get("INR=X", {})
        crude_data = macro_quotes.get("CL=F", {})
        sp500_data = macro_quotes.get("^GSPC", {})
        dxy_data = macro_quotes.get("DX-Y.NYB", {})
        us10y_data = macro_quotes.get("^TNX", {})
        gold_data = macro_quotes.get("GC=F", {})

        crude_pct = crude_data.get("pct", 0) if crude_data else 0
        sp500_pct = sp500_data.get("pct", 0) if sp500_data else 0
        dxy_pct = dxy_data.get("pct", 0) if dxy_data else 0
        us10y_pct = us10y_data.get("pct", 0) if us10y_data else 0

        # 2. FII / DII Data Fetch & Analysis
        fii_df = None
        if 'fetch_fii_dii' in globals():
            try:
                fii_df, _ = fetch_fii_dii()
            except Exception:
                fii_df = None

        fii_net_val, dii_net_val = 0, 0
        fii_bullish, fii_bearish = False, False
        if fii_df is not None and not fii_df.empty:
            try:
                net_col = [c for c in fii_df.columns if "net" in c.lower()][0]
                cat_col = [c for c in fii_df.columns if "cat" in c.lower()][0]
                for _, r in fii_df.iterrows():
                    cat_name = str(r[cat_col]).upper()
                    if "FII" in cat_name:
                        fii_net_val = float(r[net_col])
                    elif "DII" in cat_name:
                        dii_net_val = float(r[net_col])
                if fii_net_val > 500: fii_bullish = True
                elif fii_net_val < -500: fii_bearish = True
            except Exception: pass

        # 3. Nifty Option Chain & PCR Analysis
        oc_data = fetch_nse_json("/api/option-chain-indices?symbol=NIFTY") if 'fetch_nse_json' in globals() else None
        pcr_value = 1.0
        max_call_oi_strike, max_put_oi_strike = 0, 0
        total_call_oi, total_put_oi = 0, 0
        if oc_data:
            try:
                records = oc_data["records"]["data"]
                total_call_oi = sum(r["CE"]["openInterest"] for r in records if "CE" in r)
                total_put_oi = sum(r["PE"]["openInterest"] for r in records if "PE" in r)
                if total_call_oi > 0: pcr_value = round(total_put_oi / total_call_oi, 2)

                call_oi_map = {r["strikePrice"]: r["CE"]["openInterest"] for r in records if "CE" in r}
                put_oi_map = {r["strikePrice"]: r["PE"]["openInterest"] for r in records if "PE" in r}
                if call_oi_map: max_call_oi_strike = max(call_oi_map, key=call_oi_map.get)
                if put_oi_map: max_put_oi_strike = max(put_oi_map, key=put_oi_map.get)
            except Exception: pass

    # ---------- Opening Hypothesis Dynamic Score Algorithm ----------
    overnight_score = 0
    if sp500_pct > 0.4: overnight_score += 1.5
    elif sp500_pct < -0.4: overnight_score -= 1.5

    if pcr_value > 1.1: overnight_score += 1.0
    elif pcr_value < 0.8: overnight_score -= 1.0

    if crude_pct < -1.0: overnight_score += 1.0
    elif crude_pct > 1.0: overnight_score -= 1.0

    if fii_bullish: overnight_score += 1.0
    elif fii_bearish: overnight_score -= 1.0

    if dxy_pct < -0.3: overnight_score += 0.5
    elif dxy_pct > 0.3: overnight_score -= 0.5

    if overnight_score >= 2.0:
        opening_pred = "🟢 High Probability GAP-UP / Strong Bullish Opening"
        pred_bg, pred_border = "#e6f4ea", "#34a853"
        bias_text = "बुलिश (Buy-on-Dips Bias)"
    elif overnight_score <= -2.0:
        opening_pred = "🔴 High Probability GAP-DOWN / Strong Bearish Opening"
        pred_bg, pred_border = "#fce8e6", "#ea4335"
        bias_text = "बेयरिश (Sell-on-Rally Bias)"
    else:
        opening_pred = "🟡 FLAT / Range-Bound Opening Expected"
        pred_bg, pred_border = "#fef7e0", "#fbbc04"
        bias_text = "न्यूट्रल / रेंजबाउंड (Breakout/Breakdown Confirmation Required)"

    # Top Opening Hypothesis Banner
    st.markdown("### 🌅 Closing Analysis & Opening Hypothesis")
    col_h1, col_h2 = st.columns([1.3, 1])

    with col_h1:
        st.markdown(
            f"""
            <div style="background-color: {pred_bg}; border-left: 5px solid {pred_border}; padding: 14px 18px; border-radius: 8px; margin-bottom: 12px;">
                <h4 style="margin:0; color: #111;">अगले ट्रेडिंग सेशन के लिए मार्केट प्रेडिक्शन:</h4>
                <p style="font-size: 17px; font-weight: bold; margin: 6px 0 4px 0;">{opening_pred}</p>
                <p style="margin: 0; font-size: 13px; color: #333;"><b>ट्रेडिंग बायस:</b> {bias_text}</p>
                <small style="color: #555; display:block; margin-top:4px;">(ग्लोबल संकेत: US S&P500 {sp500_pct:+.2f}%, Crude Oil {crude_pct:+.2f}%, DXY {dxy_pct:+.2f}%, PCR: {pcr_value}, FII Net: ₹{fii_net_val} Cr)</small>
            </div>
            """,
            unsafe_allow_html=True
        )

    with col_h2:
        st.markdown(
            f"""
            <div style="background-color: #f8f9fa; border: 1px solid #e0e0e0; padding: 14px 18px; border-radius: 8px;">
                <h4 style="margin:0; color: #111;">Nifty Option Support & Resistance:</h4>
                <p style="margin:6px 0 2px 0;"><b>Put-Call Ratio (PCR):</b> <span style="font-weight:bold; color:{'#34a853' if pcr_value>=1.0 else '#ea4335'};">{pcr_value}</span></p>
                <p style="margin:2px 0 2px 0;"><b>Demand Support (Max Put OI):</b> ~{max_put_oi_strike if max_put_oi_strike else 'N/A'}</p>
                <p style="margin:2px 0 0 0;"><b>Supply Resistance (Max Call OI):</b> ~{max_call_oi_strike if max_call_oi_strike else 'N/A'}</p>
            </div>
            """,
            unsafe_allow_html=True
        )

    st.markdown("---")

    # Smart Trader Deep Dive Sections (Bullet Points format)
    st.markdown("## 🧠 Intraday Smart Trader AI Market Hypothesis & Multi-Stream Analysis")

    # Section 1: Global Instruments & Macro Cues
    st.markdown("### 🌐 1. ग्लोबल इंस्ट्रूमेंट्स, क्रूड & कमोडिटी सेंटीमेंट")
    st.markdown("""
* **US Dollar Index (DXY) & USD/INR:** डॉलर इंडेक्स में मजबूती इमर्जिंग मार्केट्स (भारत) से FII आउटफ्लो का दबाव बनाती है। DXY का 104 के ऊपर स्थिर होना रुपया (USD/INR) पर दबाव डालता है।
* **US 10-Yr Treasury Yield (^TNX) & TLT:** बॉन्ड यील्ड में 4.2%+ की बढ़त रिस्क-ऑफ (Risk-Off) सेंटीमेंट लाती है, जिससे इक्विटी मार्केट्स में प्रॉफिट बुकिंग देखने को मिलती है।
* **WTI Crude Oil & Energy Supply:** भारतीय बाजार के लिए क्रूड ऑयल बहुत क्रिटिकल है। क्रूड की कीमत $80/बैरल से ऊपर जाने पर पेंट, ऑटो, टाइल और एविएशन स्टॉक्स पर दबाव बनता है, जबकि ऑयल एक्सप्लोरेशन कंपनियों (ONGC, Oil India) को फायदा होता है।
* **Precious Metals (Gold & Silver / XAUUSD & XAGUSD):** सोना और चांदी में सेफ-हेवन बाइंग का बढ़ना ग्लोबल जिओ-पॉलिटिकल तनाव या मार्केट अनिश्चितता का संकेत देता है।
* **Global Indices Momentum (US30, US500, Nikkei 225, FTSE China A50, GIFT Nifty):** overnight US S&P 500 और GIFT Nifty का मोमेंटम निफ्टी की प्री-ओपनिंग दिशा तय करता है।
""")

    # Section 2: FII & DII Data Analysis
    st.markdown("### 🏦 2. FII & DII पिछले 2-3 दिनों का फ्लो डेटा एनालिसिस")
    st.markdown(f"""
* **FII Net Cash Activity:** हालिया आंकड़े दर्शा रहे हैं कि FII की नेट वैल्यू **₹{fii_net_val} Cr** रही। {'FIIs नेट बायर्स हैं जो मार्केट को संस्थागत मजबूती दे रहे हैं।' if fii_net_val > 0 else 'FIIs कैश सेगमेंट में नेट सेलर हैं, जो ऊपरी स्तरों पर सप्लाई प्रेशर दर्शाता है।'}
* **DII Net Cash Activity:** DIIs म्यूचुअल फंड SIP इनफ्लो के दम पर **₹{dii_net_val if dii_net_val else 'सकारात्मक'} Cr** का नेट सपोर्ट दे रहे हैं, जिससे बाजार में हर गिरावट पर बाइंग (Dip buying) की ताकत बनी हुई है।
* **F&O Institutional Position:** FIIs का इंडेक्स फ्यूचर्स में लॉन्ग-टू-शॉर्ट रेशियो और कॉल/पुट राइटिंग डेटा इंट्राडे मोमेंटम का दायरा तय करता है।
""")

    # Section 3: Option Chain OI & PCR Structure
    st.markdown("### 🎯 3. Nifty Option Chain, PCR & Strike Level Dynamic")
    st.markdown(f"""
* **Current Put-Call Ratio (PCR):** निफ्टी का PCR वर्तमान में **{pcr_value}** है।
  * *PCR > 1.2:* ओवरसोल्ड रिकवरी या स्ट्रॉन्ग बुलिश सेंटीमेंट (पुट राइटिंग हैवी)।
  * *PCR < 0.8:* ओवरबॉट करेक्शन का खतरा या बेयरिश सेंटीमेंट (कॉल राइटिंग हैवी)।
* **Demand Zone / Support Level (~{max_put_oi_strike if max_put_oi_strike else 'N/A'} Strike):** मैक्सिमम पुट ओपन इंटरेस्ट (Put OI) इस स्ट्राइक पर है, जो इंट्राडे के लिए मजबूत सपोर्ट/डिमांड ज़ोन का काम करेगा।
* **Supply Zone / Resistance Level (~{max_call_oi_strike if max_call_oi_strike else 'N/A'} Strike):** मैक्सिमम कॉल ओपन इंटरेस्ट (Call OI) इस स्ट्राइक पर है, जो ऊपर की ओर तत्काल कड़ा रेजिस्टेंस दर्शा रहा है।
* **OI buildup Dynamic:** कॉल राइटर्स की अनवाइंडिंग होने पर ही शॉर्ट कवरिंग (Short Covering) रैली संभव है, जबकि पुट अनवाइंडिंग से मार्केट निचले स्तरों की ओर फिसलेगा।
""")

    # Section 4: Top 10 Weighted Stocks
    st.markdown("### 🏢 4. Nifty 50 टॉप 10 वेटेज स्टॉक्स OI & सेक्टोरल ट्रेंड")

    top10_data = [
        {"Stock": "HDFC Bank (HDFCBANK)", "Weight": "~11.5%", "Impact Sector": "Banking & Financials", "OI Sentiment": "इंडेक्स का मुख्य डायरेक्शनल लीडर"},
        {"Stock": "Reliance Industries (RELIANCE)", "Weight": "~9.8%", "Impact Sector": "Energy & Telecom", "OI Sentiment": "हैवीवेट निफ्टी मूवर"},
        {"Stock": "ICICI Bank (ICICIBANK)", "Weight": "~7.9%", "Impact Sector": "Private Banking", "OI Sentiment": "बैंक निफ्टी का सपोर्ट पिलर"},
        {"Stock": "Infosys (INFY)", "Weight": "~5.8%", "Impact Sector": "IT Services", "OI Sentiment": "US टेक & नास्डैक से सीधा कनेक्शन"},
        {"Stock": "ITC Ltd (ITC)", "Weight": "~4.3%", "Impact Sector": "FMCG", "OI Sentiment": "डिफेंसिव बाइंग / लो वोलेटिलिटी"},
        {"Stock": "TCS (TCS)", "Weight": "~3.9%", "Impact Sector": "IT Major", "OI Sentiment": "करेंसी मूव & ग्लोबल टेक सेंटीमेंट"},
        {"Stock": "Larsen & Toubro (LT)", "Weight": "~3.7%", "Impact Sector": "Infra & Capital Goods", "OI Sentiment": "डोमेस्टिक कैपेक्स ड्राइवर"},
        {"Stock": "Axis Bank (AXISBANK)", "Weight": "~3.1%", "Impact Sector": "Banking", "OI Sentiment": "निफ्टी बैंक इंट्राडे मोमेंटम"},
        {"Stock": "State Bank of India (SBIN)", "Weight": "~2.9%", "Impact Sector": "PSU Banking", "OI Sentiment": "PSU और मैक्रो क्रेडिट ग्रोथ"},
        {"Stock": "Bharti Airtel (BHARTIARTL)", "Weight": "~2.8%", "Impact Sector": "Telecom", "OI Sentiment": "स्ट्रॉन्ग स्ट्रक्चरल लॉन्ग बिल्ड-अप"}
    ]
    st.table(top10_data)

    st.markdown("""
* **Banking Heavyweights (HDFC Bank & ICICI Bank):** इन दोनों स्टॉक्स का निफ्टी में मिलाकर ~19%+ योगदान है। इनमें यदि लॉन्ग बिल्ड-अप होता है तो निफ्टी और बैंक निफ्टी दोनों में बड़ी तेजी संभव है।
* **IT Heavyweights (TCS & INFY):** US फेड रेट आउटलुक और NASDAQ के ट्रेंड पर ये निर्भर करते हैं। IT में शार्ट कवरिंग निफ्टी को निचले स्तरों पर सहारा देती है।
* **Reliance Industries:** crude refining margins (GRM) और जियो टैरिफ न्यूज़ रिलायंस में बड़ा मूव ट्रिगर करते हैं।
""")

    # Section 5: Global Financial & Geopolitical News Focus
    st.markdown("### 🌍 5. ग्लोबल फाइनेंशियल & जिओ-पॉलिटिकल न्यूज इंपैक्ट (US, China, Japan, Europe, India)")
    st.markdown("""
* **United States (US Fed & US Markets):** US Fed की ब्याज दर नीतियां, CPI महंगाई आंकड़े और टेक कंपनियों की अर्निंग्स रिपोर्ट ग्लोबल लिक्विडिटी की दिशा तय करती हैं।
* **China (Macro Policy & Demand):** चीन सरकार द्वारा आर्थिक प्रोत्साहन (Stimulus) पैकेजों की घोषणा मेटल्स (Steel, Copper, Aluminium) और कमोडिटी स्टॉक्स में डिमांड ट्रिगर करती है।
* **Japan (BOJ Policy & Yen Carry Trade):** बैंक ऑफ जापान (BOJ) की मॉनेटरी पॉलिसी और येन (Yen) की चाल से ग्लोबल 'Yen Carry Trade' पर असर पड़ता है, जो ग्लोबल मार्केट में वोलेटिलिटी बढ़ा सकता है।
* **European Union (ECB & Energy Dynamics):** ECB इंटरेस्ट रेट्स और यूरोपियन एनर्जी सप्लाई स्थिति FTSE100 / DAX और भारतीय एक्सपोर्ट ओरिएंटेड सेक्टर्स को प्रभावित करती है।
* **India Focus (RBI Policy, GDP Growth & Domestic Capex):** भारत की मजबूत GDP ग्रोथ, नियंत्रित रिटेल इन्फ्लेशन और कैपेक्स साइकिल के बल पर घरेलू फंड (DIIs) विदेशी बिकवाली को आसानी से सोख रहे हैं।
""")

    # Section 6: Actionable Intraday Smart Trader Execution Strategy
    st.markdown("### ⚡ 6. Real-Time Smart Trader AI Execution Strategy & Rules")
    st.markdown("""
* **परिदृश्य A: गैप-अप ओपनिंग (Gap-Up Scenario):**
  * यदि निफ्टी Supply Zone (~Max Call OI) के पास गैप-अप खुलता है, तो तुरंत FOMO में बाइंग न करें।
  * 9:15-9:30 AM के 15-मिनट कैंडल का हाई/लो मार्क करें। VWAP या Demand Zone सपोर्ट तक पुलबैक आने पर ही **Buy-on-Dip** सेटअप खोजें।
* **परिदृश्य B: गैप-डाउन ओपनिंग (Gap-Down Scenario):**
  * Demand Zone (~Max Put OI) के पास प्राइस एक्शन देखें। यदि सपोर्ट ज़ोन पर बुशिश रिजेक्शन कैंडल (जैसे Hammer या Bullish Engulfing) बनती है, तो री-टेस्ट पर लॉन्ग ट्रेड लें।
  * यदि Demand Zone वॉल्यूम के साथ ब्रेक होता है, तो **Sell-on-Rally** रणनीति अपनाएं।
* **परिदृश्य C: फ्लैट / रेंजबाउंड ओपनिंग (Flat Opening):**
  * VWAP लाइन और Option OI अनवाइंडिंग को ट्रैक करें। जिस स्ट्राइक पर कॉल या पुट अनवाइंडिंग शुरू हो, उस ब्रेकआउट की दिशा में मोमेंटम ट्रेड लें।
* **स्मार्ट ट्रेडर रिस्क मैनेजमेंट रूल:**
  * किसी भी ट्रेड में 1:2 रिस्क-टू-रिवॉर्ड (R:R) रेशियो से कम पर एंट्री न लें।
  * 15-मिनट कैंडल क्लोजिंग के आधार पर सख्त स्टॉप-लॉस (SL) का पालन करें।
""")

    st.markdown("---")
    st.subheader("📰 Corporate Announcements & Live News Filings")
    corporate_filings = fetch_nse_corporate_announcements() if 'fetch_nse_corporate_announcements' in globals() else None
    if corporate_filings:
        filings_df = pd.DataFrame(corporate_filings)[["symbol", "subject", "time"]]
        st.dataframe(filings_df, use_container_width=True, hide_index=True)
    else:
        st.info("हाल ही में कोई मुख्य कॉरपोरेट अनाउंसमेंट नहीं मिला।")


# ---------- TAB 5: SECTOR INDEX & IMPACT (DEFERRED LOAD ON TOUCH) ----------
with tab_sector:
    if st.button("▶️ Sector Data Load/Refresh करें", key="btn_sector"):
        sector_quotes = get_quotes(list(SECTOR_INDEX_TICKERS.values()))
        sec_rows = [{"Sector Index": name, "% Chg": f"{sector_quotes[yft]['pct']:+.2f}%" if yft in sector_quotes else "—"}
                    for name, yft in SECTOR_INDEX_TICKERS.items()]
        sec_df = pd.DataFrame(sec_rows)
        if not sec_df.empty:
            st.dataframe(style_pct_columns(sec_df, ["% Chg"]), use_container_width=True, hide_index=True)
        st.caption("नोट: कुछ सेक्टर इंडेक्स टिकर Yahoo Finance पर उपलब्ध ना हों तो वहां '—' दिखेगा।")

        st.markdown("---")
        st.subheader("📌 Global + India Macro Sector Impact")
        quotes_map = get_quotes([g[2] for g in GLOBAL_INSTRUMENTS if g[2]])
        def q(yft): return quotes_map.get(yft)

        impact_rows = []
        usdinr, crude, us10y = q("INR=X"), q("CL=F"), q("^TNX")
        copper = q("HG=F")

        if usdinr and abs(usdinr["pct"]) >= 0.15:
            it_stocks = ["TCS", "INFY", "HCLTECH", "WIPRO", "TECHM", "COFORGE", "PERSISTENT"]
            omc_stocks = ["BPCL", "IOC", "HINDPETRO"]
            if usdinr["pct"] > 0:
                impact_rows.append({"sector": "IT / Export", "stocks": it_stocks, "signal": "🟢 Positive",
                                     "reason": f"रुपया {usdinr['pct']:+.2f}% कमज़ोर — export revenue का rupee-value बढ़ता है"})
                impact_rows.append({"sector": "Oil Importers / OMC", "stocks": omc_stocks, "signal": "🔴 Negative",
                                     "reason": "Import bill महंगा पड़ेगा"})
            else:
                impact_rows.append({"sector": "IT / Export", "stocks": it_stocks, "signal": "🔴 Negative",
                                     "reason": f"रुपया {abs(usdinr['pct']):.2f}% मज़बूत — export margin पर दबाव"})
                impact_rows.append({"sector": "Oil Importers / OMC", "stocks": omc_stocks, "signal": "🟢 Positive",
                                     "reason": "Import cost घटेगा"})

        if crude and abs(crude["pct"]) >= 0.5:
            if crude["pct"] > 0:
                impact_rows.append({"sector": "Upstream Oil", "stocks": ["ONGC", "OIL"], "signal": "🟢 Positive",
                                     "reason": f"Crude {crude['pct']:+.2f}% — realisation बेहतर"})
                impact_rows.append({"sector": "OMC / Aviation / Paints",
                                     "stocks": ["BPCL", "IOC", "HINDPETRO", "INDIGO", "ASIANPAINT"],
                                     "signal": "🔴 Negative", "reason": "इनपुट कॉस्ट/ATF महंगा"})
            else:
                impact_rows.append({"sector": "OMC / Aviation", "stocks": ["BPCL", "IOC", "HINDPETRO", "INDIGO"],
                                     "signal": "🟢 Positive", "reason": f"Crude {crude['pct']:+.2f}% — इनपुट कॉस्ट घटेगा"})
                impact_rows.append({"sector": "Upstream Oil", "stocks": ["ONGC", "OIL"], "signal": "🔴 Negative",
                                     "reason": "Realisation घटेगा"})

        if us10y and abs(us10y["pct"]) >= 1.0:
            tag = "🔴 Negative" if us10y["pct"] > 0 else "🟢 Positive"
            impact_rows.append({"sector": "Banks / NBFC / High-Valuation Stocks",
                                 "stocks": ["HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK", "BAJFINANCE"],
                                 "signal": tag, "reason": f"US 10Y yield {us10y['pct']:+.2f}% — FII flow पर असर"})

        if copper and abs(copper["pct"]) >= 0.5:
            tag = "🟢 Positive" if copper["pct"] > 0 else "🔴 Negative"
            impact_rows.append({"sector": "Metals",
                                 "stocks": ["HINDALCO", "VEDL", "NATIONALUM", "TATASTEEL", "JSWSTEEL", "JINDALSTEL"],
                                 "signal": tag, "reason": f"Copper {copper['pct']:+.2f}% — base-metal sentiment"})

        if not impact_rows:
            st.info("आज कोई भी macro driver threshold से ऊपर move नहीं हुआ।")
        else:
            for row in impact_rows:
                st.markdown(f"**{row['sector']}** — {row['signal']}")
                st.caption(row["reason"])
                if row["stocks"]:
                    st.markdown(" &nbsp;|&nbsp; ".join(f"[{s}]({tv_link(tv_symbol_for_stock(s))})" for s in row["stocks"]), unsafe_allow_html=True)
                st.markdown("---")
    else:
        st.info("💡 डेटा लोड करने के लिए ऊपर **▶️ Sector Data Load/Refresh करें** बटन पर क्लिक करें।")

# ---------- TAB 6: STOCK WATCHLIST (DEFERRED LOAD ON TOUCH) ----------
with tab_stocks:
    if st.button("▶️ Watchlist Load/Refresh करें", key="btn_watchlist"):
        flash_badge = "🔴 LIVE (मार्केट खुला)" if is_market_hours() else "⚪ मार्केट बंद"
        st.subheader(f"📋 Stock Watchlist ({len(selected_stocks)} स्टॉक्स)")
        st.caption(flash_badge + " · 🟢▲ = ऊपर · 🔴▼ = नीचे")

        s_quotes = get_quotes([yf_ticker_for_stock(s) for s in selected_stocks])
        with st.spinner("हर स्टॉक की ताज़ा news (Cache 1hr) चेक हो रही है..."):
            news_links = fetch_news_links_parallel(selected_stocks)

        rows = []
        for s in selected_stocks:
            q = s_quotes.get(yf_ticker_for_stock(s))
            rows.append({
                "Stock": s, "LTP": f"{q['price']:.2f}" if q else "—",
                "Change": fmt_change(q.get("chg"), q.get("pct")) if q else "—",
                "Chart": tv_link(tv_symbol_for_stock(s)), "News (24h)": news_links.get(s),
            })
        sdf = pd.DataFrame(rows)
        st.dataframe(
            style_pct_columns(sdf, ["Change"]), use_container_width=True, hide_index=True, height=460,
            column_config={
                "Chart": st.column_config.LinkColumn("Chart", display_text="📈 खोलें"),
                "News (24h)": st.column_config.LinkColumn("News (24h)", display_text="📰 पढ़ें"),
            },
        )
    else:
        st.info("💡 Watchlist लोड करने के लिए ऊपर **▶️ Watchlist Load/Refresh करें** बटन पर क्लिक करें।")

# ---------- TAB 7: ECONOMIC CALENDAR (DEFERRED LOAD ON TOUCH) ----------
with tab_calendar:
    if st.button("▶️ Calendar Load/Refresh करें", key="btn_calendar"):
        st.subheader("🗓️ Global + India Economic Calendar")
        event_count = fetch_economic_event_count_today()
        if event_count is not None:
            st.metric("🔔 आज के Medium/High Importance Events", event_count)
        components.html("""
            <div class="tradingview-widget-container">
              <div class="tradingview-widget-container__widget"></div>
              <script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-events.js" async>
              {"colorTheme": "light", "isTransparent": false, "width": "100%", "height": "600", "locale": "en", "importanceFilter": "0,1", "countryFilter": "us,in,cn,jp,gb,eu"}
              </script>
            </div>""", height=620)
    else:
        st.info("💡 Calendar देखने के लिए ऊपर **▶️ Calendar Load/Refresh करें** बटन दबाएं।")

# ---------- TAB 8: FII/DII + NIFTY OUTLOOK (DEFERRED LOAD ON TOUCH) ----------
with tab_fii:
    if st.button("▶️ FII/DII + Nifty Data Load करें", key="btn_fii"):
        col_fii, col_nifty = st.columns(2)

        with col_fii:
            st.markdown("### 💰 FII / DII Activity")
            fii_df, source = fetch_fii_dii()
            if fii_df is not None:
                latest_row = fii_df.iloc[0]
                cols = st.columns(len(latest_row))
                for i, (colname, val) in enumerate(latest_row.items()):
                    cols[i].metric(str(colname), str(val))

                insight = fii_dii_insight(fii_df)
                if insight:
                    level, msg = insight
                    getattr(st, level)(msg)

                with st.expander("📅 पिछले 5 दिन का पूरा डाटा देखें"):
                    st.dataframe(fii_df, use_container_width=True, hide_index=True)
                st.caption(f"Source: {source}")
            else:
                st.warning("FII/DII data उपलब्ध नहीं हो पाया।")

        with col_nifty:
            st.markdown("### 🎯 Nifty 50 — Option Chain Outlook")
            oc_data = fetch_nse_json("/api/option-chain-indices?symbol=NIFTY")
            if oc_data:
                try:
                    records, spot = oc_data["records"]["data"], oc_data["records"]["underlyingValue"]
                    call_oi, put_oi = {}, {}
                    for r in records:
                        strike = r["strikePrice"]
                        if "CE" in r: call_oi[strike] = r["CE"]["openInterest"]
                        if "PE" in r: put_oi[strike] = r["PE"]["openInterest"]
                    total_call, total_put = sum(call_oi.values()), sum(put_oi.values())
                    pcr = round(total_put / total_call, 2) if total_call else None
                    resistance = max(call_oi, key=call_oi.get) if call_oi else None
                    support = max(put_oi, key=put_oi.get) if put_oi else None

                    st.metric("Nifty Spot", f"{spot:.2f}")
                    c1, c2, c3 = st.columns(3)
                    c1.metric("PCR (OI)", pcr if pcr else "—")
                    c2.metric("Resistance", resistance if resistance else "—")
                    c3.metric("Support", support if support else "—")
                    if pcr:
                        bias = "Mildly Bullish" if pcr > 1.1 else "Mildly Bearish" if pcr < 0.8 else "Range-bound"
                        st.info(f"📌 **{bias}** (PCR={pcr}). Support ~{support}, Resistance ~{resistance}.")
                except Exception:
                    st.warning("Option-chain data parse नहीं हो पाया।")
            else:
                st.warning("NSE Option-Chain data नहीं मिला।")
    else:
        st.info("💡 FII/DII और Option Chain Data देखने के लिए ऊपर **▶️ FII/DII + Nifty Data Load करें** बटन पर क्लिक करें।")

# ---------- TAB 9: DELIVERY % + BULK/BLOCK DEALS (DEFERRED LOAD ON TOUCH) ----------
with tab_delivery:
    if st.button("▶️ Delivery & Deals Data Load करें", key="btn_deliv"):
        st.subheader("📦 Delivery % — पिछले 2 दिन (Compare)")
        with st.spinner("पिछले 2 दिन का delivery data देखा जा रहा है..."):
            deliv_date, deliv_rows = get_delivery_2day_compare(selected_stocks)

        if deliv_date is None or not deliv_rows:
            st.info("Delivery data लोड नहीं हो सका या watchlist खाली है।")
        else:
            ddf = pd.DataFrame(deliv_rows).sort_values("बदलाव", ascending=False)
            def hl_change(val):
                if val > 0: return f"background-color:{COLOR_POS_BG}; color:{COLOR_POS_TEXT}; font-weight:600;"
                if val < 0: return f"background-color:{COLOR_NEG_BG}; color:{COLOR_NEG_TEXT}; font-weight:600;"
                return ""

            styler = _styler_apply_map(ddf.style, hl_change, ["बदलाव"])
            st.dataframe(
                styler, use_container_width=True, hide_index=True,
                column_config={"Chart": st.column_config.LinkColumn("Chart", display_text="📈 खोलें")},
            )

        st.markdown("---")
        st.subheader("🏦 Bulk / Block Deals (आपकी Watchlist में)")
        deals_data = fetch_bulk_block_deals()
        if deals_data:
            bulk = filter_deals_for_watchlist(deals_data.get("BULK_DEALS_DATA", []), selected_stocks)
            block = filter_deals_for_watchlist(deals_data.get("BLOCK_DEALS_DATA", []), selected_stocks)
            if bulk is not None and not bulk.empty:
                st.markdown("**Bulk Deals**")
                st.dataframe(bulk, use_container_width=True, hide_index=True)
            if block is not None and not block.empty:
                st.markdown("**Block Deals**")
                st.dataframe(block, use_container_width=True, hide_index=True)
    else:
        st.info("💡 Delivery और Deals Data देखने के लिए ऊपर **▶️ Delivery & Deals Data Load करें** बटन पर क्लिक करें।")

# ---------- TAB 10: TOP GAINERS / LOSERS (DEFERRED LOAD ON TOUCH) ----------
with tab_movers:
    if st.button("▶️ Top Gainers & Losers Load करें", key="btn_movers"):
        st.subheader("🏆 Top 5 Gainers & Top 5 Losers")
        quotes = get_quotes([yf_ticker_for_stock(s) for s in selected_stocks])
        mv_rows = []
        for s in selected_stocks:
            qd = quotes.get(yf_ticker_for_stock(s))
            if qd:
                mv_rows.append({"Stock": s, "LTP": qd["price"], "pct": qd["pct"], "chg": qd.get("chg"),
                                 "Chart": tv_link(tv_symbol_for_stock(s))})
        mv_df = pd.DataFrame(mv_rows)
        if not mv_df.empty:
            gainers = mv_df.sort_values("pct", ascending=False).head(5).copy()
            losers = mv_df.sort_values("pct", ascending=True).head(5).copy()
            for _df in (gainers, losers):
                _df["Change"] = _df.apply(lambda r: fmt_change(r["chg"], r["pct"]), axis=1)
                _df.drop(columns=["pct", "chg"], inplace=True)

            st.markdown("#### 🟢 Top 5 Gainers")
            st.dataframe(
                style_pct_columns(gainers.style.format({"LTP": "{:.2f}"}), ["Change"]),
                use_container_width=True, hide_index=True,
                column_config={"Chart": st.column_config.LinkColumn("Chart", display_text="📈")},
            )
            st.markdown("#### 🔴 Top 5 Losers")
            st.dataframe(
                style_pct_columns(losers.style.format({"LTP": "{:.2f}"}), ["Change"]),
                use_container_width=True, hide_index=True,
                column_config={"Chart": st.column_config.LinkColumn("Chart", display_text="📈")},
            )
    else:
        st.info("💡 Gainers & Losers देखने के लिए ऊपर **▶️ Top Gainers & Losers Load करें** बटन पर क्लिक करें।")
