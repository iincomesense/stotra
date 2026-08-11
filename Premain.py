"""
Full Market Dashboard (Streamlit) — Compact Edition
=====================================================
Global Markets + TradingView charts | Sector Index Impact | Stock
Watchlist with live-flash news | EMA/Volume Signals + Alerts |
Economic Calendar | FII/DII (Analysis-driven) + Nifty Option-OI |
Delivery% (2-day compare) + Bulk/Block Deals | Gainers/Losers |
Institutional-style News (8:30 AM - 8:30 PM window).

बदलाव (इस version में):
  1) News अब सुबह 8:30 से रात 8:30 तक की विंडो में रहती है, उसके बाद अपने-आप खाली।
  2) Delivery% अब पिछले 2 दिन साथ-साथ दिखाता है (compare करने के लिए)।
  3) Signals में Daily timeframe पर Volume Spike को अलग से 🔥 हाइलाइट मिलता है।
  4) FII/DII अब सिर्फ लिंक नहीं — डाटा पर आधारित असल analysis/insight दिखाता है।
  5) पूरी फाइल छोटी की गई: dead/unused code (जो कहीं इस्तेमाल ही नहीं होता था) हटाया,
     FII/DII के 4 भारी fallback sources को 2 तक सीमित किया, और 2 syntax bugs ठीक किए
     (batch_daily और chip function में अधूरी if-लाइनें थीं जिनसे app crash हो सकती थी)।

Deploy: share.streamlit.io -> connect GitHub repo -> main file: app.py
"""

import concurrent.futures
import io
import urllib.parse
from datetime import datetime, timedelta, timezone
from datetime import time as dtime

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

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
ALERT_CLEAR_HOUR_IST = 20          # रात 8 बजे Alerts साफ
NEWS_WINDOW_START = dtime(8, 30)   # सुबह 8:30 से news दिखनी शुरू
NEWS_WINDOW_END = dtime(20, 30)    # रात 8:30 के बाद news अपने-आप खाली

COLOR_POS_BG, COLOR_POS_TEXT = "#d4f8d4", "#0a7d2f"
COLOR_NEG_BG, COLOR_NEG_TEXT = "#f8d4d4", "#c0392b"
COLOR_FLAT_TEXT = "#555555"
COLOR_SPIKE_BG = "#ffe1a8"   # 🔥 daily volume spike highlight

# ============================== MASTER STOCK LIST ==============================
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

NEWS_KEYWORDS = [
    "Nifty", "Sensex", "RBI policy", "SEBI notice", "order win",
    "block deal", "bulk deal", "quarterly results", "FII inflow",
    "brokerage upgrade OR downgrade", "stock market India",
]
POSITIVE_WORDS = ["surge", "rally", "jump", "gain", "upgrade", "record profit",
                   "order win", "bags order", "beats estimate", "buyback",
                   "strong results", "upper circuit", "bullish", "outperform"]
NEGATIVE_WORDS = ["crash", "plunge", "fall", "downgrade", "miss estimate",
                   "loss", "lower circuit", "bearish", "underperform",
                   "probe", "raid", "fraud", "default", "resign"]
HIGH_IMPACT_WORDS = ["rbi", "sebi", "fed", "war", "ban", "sanction",
                      "interest rate", "inflation", "gdp", "election",
                      "recession", "crisis", "circuit breaker"]

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}

# ============================== PAGE SETUP ==============================
st.set_page_config(page_title="Full Market Dashboard", layout="wide",
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


# ============================== छोटे हेल्पर्स ==============================
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
    """कई formats को safely float में parse करता है।"""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if s in ("", "—", "-", "None", "nan"):
        return None
    import re
    m = re.search(r"\(([-+]?\d+\.?\d*)%\)", s)
    if m:
        try:
            return float(m.group(1))
        except Exception:
            pass
    s = s.replace("%", "").replace("+", "").replace("▲", "").replace("▼", "").replace("●", "").strip()
    try:
        return float(s)
    except Exception:
        return None


def pct_bg_style(val):
    v = _parse_pct(val)
    if v is None:
        return ""
    if v > 0:
        return f"background-color:{COLOR_POS_BG}; color:{COLOR_POS_TEXT}; font-weight:600;"
    if v < 0:
        return f"background-color:{COLOR_NEG_BG}; color:{COLOR_NEG_TEXT}; font-weight:600;"
    return f"color:{COLOR_FLAT_TEXT};"


def _styler_apply_map(styler, fn, subset):
    """pandas नई/पुरानी दोनों versions पर काम करे (Styler.map बनाम .applymap)।"""
    if hasattr(styler, "map"):
        try:
            return styler.map(fn, subset=subset)
        except Exception:
            pass
    return styler.applymap(fn, subset=subset)


def style_pct_columns(obj, cols):
    """DataFrame या Styler दोनों ले सकता है। दिए गए % columns पर green/red background लगाता है।"""
    if isinstance(obj, pd.DataFrame):
        styler, available_cols = obj.style, obj.columns
    else:
        styler, available_cols = obj, obj.data.columns
    valid_cols = [c for c in cols if c in available_cols]
    if not valid_cols:
        return styler
    return _styler_apply_map(styler, pct_bg_style, valid_cols)


def fmt_change(chg, pct):
    """Dhan-app जैसा combined format: '7.70 (+1.63%) ▲'"""
    if chg is None or pct is None:
        return "—"
    arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "●")
    return f"{chg:+.2f} ({pct:+.2f}%) {arrow}"


def tag_news(title):
    t = title.lower()
    if any(w in t for w in HIGH_IMPACT_WORDS):
        return "⚠️ HIGH IMPACT / RISK"
    if any(w in t for w in NEGATIVE_WORDS):
        return "📉 NEGATIVE"
    if any(w in t for w in POSITIVE_WORDS):
        return "🚀 POSITIVE"
    return "🔵 NEUTRAL"


# ============================== SIDEBAR ==============================
st.sidebar.header("⚙️ Settings")
refresh_min = st.sidebar.slider("Auto-Refresh हर (मिनट)", 0.5, 15.0, 1.0, 0.5)
if HAS_AUTOREFRESH:
    st_autorefresh(interval=int(refresh_min * 60 * 1000), key="auto_refresh")
else:
    st.sidebar.warning("`streamlit-autorefresh` install करें auto-refresh के लिए।")

st.sidebar.markdown(f"🕒 IST: **{now_ist().strftime('%d-%b-%Y %H:%M:%S')}**")
st.sidebar.markdown("🟢 मार्केट खुला" if is_market_hours() else "🔴 मार्केट बंद")
if st.sidebar.button("🔄 अभी Refresh करें"):
    st.cache_data.clear()
    st.rerun()

selected_stocks = st.sidebar.multiselect("Watchlist", WATCHLIST_DEFAULT, default=WATCHLIST_DEFAULT)
vol_mult = st.sidebar.slider("Volume Spike Multiplier", 1.5, 5.0, 2.0, 0.5)
signal_timeframes = st.sidebar.multiselect(
    "Signal Scan Timeframes", ["15 Min", "1 Hour", "Daily"], default=["1 Hour", "Daily"],
)

# ============================== ALERTS STATE (रात 8 बजे auto-clear) ==============================
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

# ============================== QUOTE FETCH HELPERS ==============================
@st.cache_data(ttl=300, show_spinner=False)
def batch_daily(tickers_tuple):
    tickers = list(tickers_tuple)
    if not tickers:
        return {}
    try:
        data = yf.download(tickers, period="10d", interval="1d",
                            group_by="ticker", progress=False, threads=True)
    except Exception:
        return {}
    out = {}
    for t in tickers:
        try:
            df = data[t].dropna() if len(tickers) > 1 else data.dropna()
            if len(df) >= 2:
                last, prev = df["Close"].iloc[-1], df["Close"].iloc[-2]
                out[t] = {"price": last, "pct": (last - prev) / prev * 100, "chg": last - prev}
        except Exception:
            continue
    return out


@st.cache_data(ttl=180, show_spinner=False)
def batch_intraday_last(tickers_tuple):
    tickers = list(tickers_tuple)
    if not tickers:
        return {}
    try:
        data = yf.download(tickers, period="1d", interval="5m",
                            group_by="ticker", progress=False, threads=True)
    except Exception:
        return {}
    out = {}
    for t in tickers:
        try:
            df = data[t].dropna() if len(tickers) > 1 else data.dropna()
            if len(df):
                out[t] = df["Close"].iloc[-1]
        except Exception:
            continue
    return out


def get_quotes(tickers):
    daily = batch_daily(tuple(tickers))
    intraday = batch_intraday_last(tuple(tickers))
    quotes = {}
    for t in tickers:
        d = daily.get(t)
        if not d:
            continue
        quotes[t] = {"price": intraday.get(t, d["price"]), "pct": d["pct"], "chg": d.get("chg")}
    return quotes


# ============================== NSE / FALLBACK DATA ==============================
@st.cache_data(ttl=900, show_spinner=False)
def fetch_nse_json(api_path):
    session = requests.Session()
    session.headers.update(NSE_HEADERS)
    try:
        session.get("https://www.nseindia.com", timeout=8)
        r = session.get(f"https://www.nseindia.com{api_path}", timeout=8)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


@st.cache_data(ttl=900, show_spinner=False)
def fetch_fii_dii():
    """FII/DII डाटा — पहले StockEdge लिंक (5 दिन), फिर NSE API fallback।"""
    try:
        r = requests.get("https://sedg.in/p8nximtd", headers=NSE_HEADERS, timeout=10, allow_redirects=True)
        for t in pd.read_html(io.StringIO(r.text)):
            if t.shape[1] >= 3 and t.shape[0] >= 3:
                return t.head(5), "StockEdge"
    except Exception:
        pass
    fii_data = fetch_nse_json("/api/fiidiiTradeReact")
    if fii_data:
        return pd.DataFrame(fii_data).head(5), "NSE (fallback)"
    return None, None


def fii_dii_insight(df):
    """डाटा से खुद insight निकालता है — सिर्फ लिंक नहीं, असल analysis।"""
    try:
        cols_lower = {c.lower(): c for c in df.columns}
        net_col = cols_lower.get("netvalue") or cols_lower.get("net_value")
        cat_col = cols_lower.get("category")
        if not net_col or not cat_col:
            return None
        latest = df.iloc[0] if "date" not in cols_lower else df
        fii_net = None
        dii_net = None
        for _, row in df.iterrows():
            cat = str(row[cat_col]).upper()
            try:
                val = float(row[net_col])
            except Exception:
                continue
            if "FII" in cat or "FPI" in cat:
                fii_net = val if fii_net is None else fii_net
            elif "DII" in cat:
                dii_net = val if dii_net is None else dii_net
        if fii_net is None and dii_net is None:
            return None
        if fii_net is not None and fii_net > 0 and dii_net is not None and dii_net > 0:
            return "success", f"🟢 FII (₹{fii_net:+.0f} Cr) और DII (₹{dii_net:+.0f} Cr) दोनों खरीदार — Bullish bias।"
        if fii_net is not None and fii_net < 0 and dii_net is not None and dii_net > 0:
            return "info", f"🔵 FII बिकवाली (₹{fii_net:+.0f} Cr) पर DII (₹{dii_net:+.0f} Cr) सपोर्ट दे रहे हैं — गिरावट सीमित रह सकती है।"
        if fii_net is not None and fii_net > 0 and dii_net is not None and dii_net < 0:
            return "info", f"🔵 FII खरीदारी (₹{fii_net:+.0f} Cr) कर रहे, DII बेच रहे (₹{dii_net:+.0f} Cr) — मिला-जुला संकेत।"
        if fii_net is not None and fii_net < 0 and dii_net is not None and dii_net < 0:
            return "error", f"🔴 FII (₹{fii_net:+.0f} Cr) और DII (₹{dii_net:+.0f} Cr) दोनों बिकवाल — Cautious bias।"
        return None
    except Exception:
        return None


@st.cache_data(ttl=3600 * 6, show_spinner=False)
def fetch_bhavcopy(date_str_ddmmyyyy):
    url = f"https://archives.nseindia.com/products/content/sec_bhavdata_full_{date_str_ddmmyyyy}.csv"
    try:
        r = requests.get(url, headers=NSE_HEADERS, timeout=10)
        if r.status_code == 200 and "SYMBOL" in r.text[:300].upper():
            df = pd.read_csv(io.StringIO(r.text))
            df.columns = [c.strip() for c in df.columns]
            return df
    except Exception:
        pass
    return None


def get_last_n_trading_bhavcopies(n=2, lookback_days=15):
    results = []
    cursor = now_ist().date() - timedelta(days=1)
    tries = 0
    while len(results) < n and tries < lookback_days:
        df = fetch_bhavcopy(cursor.strftime("%d%m%Y"))
        if df is not None:
            results.append((cursor, df))
        cursor -= timedelta(days=1)
        tries += 1
    return list(reversed(results))  # पुराना -> नया


def get_delivery_2day_compare(stocks):
    """पिछले 2 ट्रेडिंग दिनों का Delivery% साथ-साथ (compare के लिए)।"""
    data = get_last_n_trading_bhavcopies(2)
    if len(data) < 2:
        return None, None
    (date1, df1), (date2, df2) = data[0], data[1]

    def deliv_col(df):
        cols = [c for c in df.columns if "DELIV_PER" in c.upper()]
        return cols[0] if cols else None

    dcol1, dcol2 = deliv_col(df1), deliv_col(df2)
    if not dcol1 or not dcol2:
        return date2, None

    rows = []
    for stock in stocks:
        try:
            r1 = df1[(df1["SYMBOL"].astype(str).str.strip() == stock) & (df1["SERIES"].astype(str).str.strip() == "EQ")]
            r2 = df2[(df2["SYMBOL"].astype(str).str.strip() == stock) & (df2["SERIES"].astype(str).str.strip() == "EQ")]
            if r1.empty or r2.empty:
                continue
            v1 = float(str(r1.iloc[0][dcol1]).strip())
            v2 = float(str(r2.iloc[0][dcol2]).strip())
            rows.append({
                "Stock": stock,
                date1.strftime("%d-%b"): round(v1, 2),
                date2.strftime("%d-%b (नया)"): round(v2, 2),
                "बदलाव": round(v2 - v1, 2),
                "Chart": tv_link(tv_symbol_for_stock(stock)),
            })
        except Exception:
            continue
    return date2, rows


@st.cache_data(ttl=900, show_spinner=False)
def fetch_bulk_block_deals():
    return fetch_nse_json("/api/snapshot-capital-market-largedeals")


def filter_deals_for_watchlist(deals_list, stocks):
    if not deals_list:
        return pd.DataFrame()
    df = pd.DataFrame(deals_list)
    symbol_col = next((c for c in ["BD_SYMBOL", "symbol", "SYMBOL", "clientSymbol"] if c in df.columns), None)
    if symbol_col is None:
        return df
    return df[df[symbol_col].astype(str).str.strip().isin(stocks)]


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_economic_event_count_today():
    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                          headers=NSE_HEADERS, timeout=10)
        events, today, count = r.json(), now_ist().date(), 0
        for e in events:
            if str(e.get("impact", "")).lower() not in ("high", "medium"):
                continue
            try:
                ev_date = datetime.fromisoformat(e.get("date").replace("Z", "+00:00")).astimezone(IST).date()
            except Exception:
                continue
            if ev_date == today:
                count += 1
        return count
    except Exception:
        return None


@st.cache_data(ttl=300, show_spinner=False)
def fetch_nse_corporate_announcements():
    data = fetch_nse_json("/api/corporate-announcements?index=equities")
    if not data:
        return []
    items = []
    for d in data[:50]:
        try:
            items.append({
                "symbol": d.get("symbol", ""),
                "subject": d.get("desc") or d.get("subject") or "",
                "attachment": d.get("attchmntFile", ""),
                "time": d.get("an_dt") or d.get("sort_date") or "",
            })
        except Exception:
            continue
    return items


# ============================== NEWS WINDOW हेल्पर (8:30 AM - 8:30 PM) ==============================
def news_window_active():
    """रात 8:30 के बाद से सुबह 8:30 तक विंडो बंद (news list खाली) रहती है।"""
    t = now_ist().time()
    return NEWS_WINDOW_START <= t <= NEWS_WINDOW_END


def news_cutoff_utc():
    """आज सुबह 8:30 IST को UTC में बदलकर देता है — इससे पहले की news नहीं दिखेगी।"""
    start_ist = now_ist().replace(hour=NEWS_WINDOW_START.hour, minute=NEWS_WINDOW_START.minute,
                                   second=0, microsecond=0)
    return start_ist.astimezone(timezone.utc)


# ============================== TABS ==============================
(tab_global, tab_sector, tab_stocks, tab_signals, tab_alerts, tab_calendar,
 tab_fii, tab_delivery, tab_movers, tab_news) = st.tabs([
    "🌍 Global", "🏭 Sector Impact", "📋 Watchlist", "📊 Signals", "🔔 Alerts",
    "🗓️ Calendar", "💰 FII/DII+Nifty", "📦 Delivery%+Deals",
    "🏆 Gainers/Losers", "📰 News",
])

# ---------- TAB: GLOBAL MARKETS ----------
with tab_global:
    st.subheader("🌍 Global Markets, Currencies, Commodities & Indices — Live (TradingView)")
    st.caption("यह widget सीधे TradingView के live data से चलता है — पेज खोलते ही असल-समय भाव दिखेगा।")
    ticker_items = ",".join(
        '{"proName": "%s", "title": "%s"}' % (tvs, sym) for sym, _, _, tvs in GLOBAL_INSTRUMENTS
    )
    components.html(f"""
        <div class="tradingview-widget-container">
          <div class="tradingview-widget-container__widget"></div>
          <script type="text/javascript"
            src="https://s3.tradingview.com/external-embedding/embed-widget-ticker-tape.js" async>
          {{"symbols": [{ticker_items}], "showSymbolLogo": true, "isTransparent": false,
            "displayMode": "adaptive", "colorTheme": "light", "locale": "en"}}
          </script>
        </div>""", height=80)

    st.markdown("&nbsp;")
    st.markdown("**हर instrument का Price, बदलाव और live chart:** &nbsp; 🟢▲ = ऊपर · 🔴▼ = नीचे")
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

# ---------- TAB: SECTOR INDEX & IMPACT ----------
with tab_sector:
    st.subheader("🏭 सेक्टर इंडेक्स — % बदलाव")
    st.caption("🟢▲ = ऊपर · 🔴▼ = नीचे")
    sector_quotes = get_quotes(list(SECTOR_INDEX_TICKERS.values()))
    sec_rows = [{"Sector Index": name, "% Chg": f"{sector_quotes[yft]['pct']:+.2f}%" if yft in sector_quotes else "—"}
                for name, yft in SECTOR_INDEX_TICKERS.items()]
    sec_df = pd.DataFrame(sec_rows)
    if not sec_df.empty:
        st.dataframe(style_pct_columns(sec_df, ["% Chg"]), use_container_width=True, hide_index=True)
    st.caption("नोट: कुछ सेक्टर इंडेक्स टिकर Yahoo Finance पर उपलब्ध ना हों तो वहां '—' दिखेगा।")

    st.markdown("---")
    st.subheader("📌 Global + India Macro के आधार पर Impact (कारण सहित)")
    st.caption("Rule-based heuristic — सिर्फ जानकारी के लिए, यह वित्तीय सलाह नहीं है।")
    quotes_map = get_quotes([g[2] for g in GLOBAL_INSTRUMENTS if g[2]])

    def q(yft):
        return quotes_map.get(yft)

    impact_rows = []
    usdinr, crude, us10y = q("INR=X"), q("CL=F"), q("^TNX")
    gold, copper, natgas, dxy = q("GC=F"), q("HG=F"), q("NG=F"), q("DX-Y.NYB")

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

    if gold and abs(gold["pct"]) >= 0.5:
        tag = "🟢 Positive" if gold["pct"] > 0 else "🔴 Negative"
        impact_rows.append({"sector": "Gold-linked", "stocks": ["TITAN"], "signal": tag, "reason": f"Gold {gold['pct']:+.2f}%"})

    if natgas and abs(natgas["pct"]) >= 1.0:
        tag = "🟢 Positive" if natgas["pct"] > 0 else "🔴 Negative"
        impact_rows.append({"sector": "Gas Utility", "stocks": ["GAIL"], "signal": tag, "reason": f"Natural Gas {natgas['pct']:+.2f}%"})

    if dxy and abs(dxy["pct"]) >= 0.2:
        tag = "🔴 Negative" if dxy["pct"] > 0 else "🟢 Positive"
        impact_rows.append({"sector": "Broad Nifty / EM Risk Sentiment", "stocks": [], "signal": tag,
                             "reason": f"DXY {dxy['pct']:+.2f}% — डॉलर की मज़बूती/कमज़ोरी का global risk-appetite पर असर"})

    if not impact_rows:
        st.info("आज कोई भी macro driver threshold से ऊपर move नहीं हुआ — कोई स्पष्ट सेक्टर bias नहीं।")
    else:
        for row in impact_rows:
            st.markdown(f"**{row['sector']}** — {row['signal']}")
            st.caption(row["reason"])
            if row["stocks"]:
                st.markdown(" &nbsp;|&nbsp; ".join(f"[{s}]({tv_link(tv_symbol_for_stock(s))})" for s in row["stocks"]),
                            unsafe_allow_html=True)
            st.markdown("---")

# ---------- TAB: STOCK WATCHLIST ----------
@st.cache_data(ttl=120, show_spinner=False)
def fetch_stock_quick_news_link_live(stock_name):
    if feedparser is None:
        return None
    query = urllib.parse.quote_plus(f"{stock_name} NSE when:1d")
    url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
    try:
        feed = feedparser.parse(requests.get(url, timeout=10).content)
    except Exception:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    for e in feed.entries[:5]:
        pub = e.get("published_parsed")
        if pub and datetime(*pub[:6], tzinfo=timezone.utc) >= cutoff:
            return e.link
    return None


def fetch_news_links_parallel(stocks):
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        futures = {ex.submit(fetch_stock_quick_news_link_live, s): s for s in stocks}
        for fut in concurrent.futures.as_completed(futures):
            s = futures[fut]
            try:
                results[s] = fut.result()
            except Exception:
                results[s] = None
    return results


with tab_stocks:
    flash_badge = "🔴 LIVE (मार्केट खुला — हर 2 मिनट में news check)" if is_market_hours() \
        else "⚪ मार्केट बंद — news हर 30 मिनट में check होगी"
    st.subheader(f"📋 Stock Watchlist ({len(selected_stocks)} स्टॉक्स)")
    st.caption(flash_badge + " · 🟢▲ = ऊपर · 🔴▼ = नीचे")

    s_quotes = get_quotes([yf_ticker_for_stock(s) for s in selected_stocks])
    with st.spinner("हर स्टॉक की ताज़ा news (24h) चेक हो रही है..."):
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
    st.caption("📰 सिर्फ़ वो स्टॉक जिनकी पिछले 24 घंटे में कोई खबर मिली, वहाँ लिंक दिखेगा।")

    st.markdown("---")
    st.markdown("**किसी एक स्टॉक की ताज़ा News देखें:**")
    news_stock = st.selectbox("स्टॉक चुनें", selected_stocks)

    @st.cache_data(ttl=600, show_spinner=False)
    def fetch_stock_news(stock_name):
        if feedparser is None:
            return []
        query = urllib.parse.quote_plus(f"{stock_name} NSE stock when:1d")
        url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
        try:
            feed = feedparser.parse(requests.get(url, timeout=15).content)
        except Exception:
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        items = []
        for e in feed.entries[:10]:
            pub = e.get("published_parsed")
            if pub and datetime(*pub[:6], tzinfo=timezone.utc) >= cutoff:
                items.append({"title": e.title, "link": e.link, "published": datetime(*pub[:6], tzinfo=timezone.utc)})
        return items

    with st.spinner("News fetch हो रही है..."):
        s_news = fetch_stock_news(news_stock)
    if not s_news:
        st.info(f"{news_stock} के लिए पिछले 24 घंटे में कोई ताज़ा खबर नहीं मिली।")
    else:
        for it in s_news:
            t = it["published"].astimezone(IST).strftime("%d-%b %H:%M")
            st.markdown(f"- {tag_news(it['title'])} — [{it['title']}]({it['link']})  \n  _{t} IST_")

# ---------- TAB: EMA/VOLUME SIGNALS ----------
TIMEFRAMES = {
    "15 Min": {"interval": "5m", "period": "5d", "resample": "15min", "intraday": True},
    "1 Hour": {"interval": "60m", "period": "1mo", "resample": None, "intraday": True},
    "Daily":  {"interval": "1d", "period": "6mo", "resample": None, "intraday": False},
}


@st.cache_data(ttl=300, show_spinner=False)
def fetch_tf_data(tf_key, symbols_tuple):
    cfg = TIMEFRAMES[tf_key]
    symbols = [yf_ticker_for_stock(s) for s in symbols_tuple]
    try:
        data = yf.download(symbols, period=cfg["period"], interval=cfg["interval"],
                            group_by="ticker", progress=False, threads=True)
    except Exception:
        return {}
    out = {}
    for stock in symbols_tuple:
        sym = yf_ticker_for_stock(stock)
        try:
            df = data[sym].dropna() if len(symbols) > 1 else data.dropna()
        except Exception:
            continue
        if df is None or df.empty:
            continue
        if cfg["resample"]:
            df = df.resample(cfg["resample"]).agg(
                {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()
        if len(df) >= 51:
            out[stock] = df
    return out


def check_ema_cross(df):
    ema20 = df["Close"].ewm(span=20, adjust=False).mean()
    ema50 = df["Close"].ewm(span=50, adjust=False).mean()
    if ema20.iloc[-2] <= ema50.iloc[-2] and ema20.iloc[-1] > ema50.iloc[-1]:
        return "UP"
    if ema20.iloc[-2] >= ema50.iloc[-2] and ema20.iloc[-1] < ema50.iloc[-1]:
        return "DOWN"
    return None


def check_volume_spike(df, mult):
    vol = df["Volume"]
    if len(vol) < 21:
        return None
    avg_vol = vol.iloc[-21:-1].mean()
    curr_vol = vol.iloc[-1]
    if avg_vol > 0 and (curr_vol / avg_vol) >= mult:
        return curr_vol / avg_vol
    return None


with tab_signals:
    st.subheader("📊 EMA 20×50 Crossover + Volume Spike Signals")
    st.caption(
        "🔥 = Daily टाइमफ्रेम पर Volume Spike (सबसे मज़बूत सिग्नल, अलग हाइलाइट) · "
        "⭐⭐ = EMA Cross + Volume Spike साथ · ⭐ = सिर्फ एक सिग्नल · "
        "🟢/🔴 पूरी रो = EMA UP/DOWN · हर नया सिग्नल 🔔 Alerts टैब में भी जुड़ता है (रात 8 बजे auto-clear)"
    )

    local_tf = st.multiselect("टाइमफ्रेम चुनें", list(TIMEFRAMES.keys()), default=signal_timeframes, key="signals_tf_local")
    is_after_close = now_ist().hour >= 16
    if is_after_close:
        st.info("बाज़ार बंद — Intraday (15m/1h) सिग्नल आज के लिए hide हैं। सिर्फ Daily दिखेगा।")

    rows = []
    existing_keys = {a["key"] for a in st.session_state.alerts}
    for tf_key in local_tf:
        cfg = TIMEFRAMES[tf_key]
        if cfg["intraday"] and is_after_close:
            continue
        tf_data = fetch_tf_data(tf_key, tuple(selected_stocks))
        for stock, df in tf_data.items():
            price, bar_time = df["Close"].iloc[-1], df.index[-1]
            cross, vr = check_ema_cross(df), check_volume_spike(df, vol_mult)
            if not cross and not vr:
                continue

            is_daily_spike = (tf_key == "Daily" and vr is not None)
            type_parts = []
            if cross:
                type_parts.append("🟢 EMA UP" if cross == "UP" else "🔴 EMA DOWN")
            if vr:
                type_parts.append(f"⚡ Volume {vr:.1f}x")
            stars = "🔥" if is_daily_spike else ("⭐⭐" if (cross and vr) else "⭐")
            bar_time_str = bar_time.strftime("%H:%M %d-%b")

            rows.append({
                "सिग्नल": stars, "स्टॉक": stock, "टाइमफ्रेम": tf_key,
                "टाइप": " + ".join(type_parts), "LTP": round(price, 2), "समय": bar_time_str,
                "Chart": tv_link(tv_symbol_for_stock(stock)),
            })

            alert_key = f"{stock}|{tf_key}|{'+'.join(type_parts)}|{bar_time_str}"
            if alert_key not in existing_keys:
                st.session_state.alerts.append({
                    "key": alert_key, "stock": stock, "tf": tf_key, "type": " + ".join(type_parts),
                    "stars": stars, "time": bar_time_str, "logged_at": now_ist().strftime("%H:%M:%S"),
                    "chart": tv_link(tv_symbol_for_stock(stock)),
                })
                existing_keys.add(alert_key)

    if not rows:
        st.success("अभी कोई नया सिग्नल नहीं है।")
    else:
        sig_df = pd.DataFrame(rows)
        sort_rank = {"🔥": 3, "⭐⭐": 2, "⭐": 1}
        sig_df["_sort"] = sig_df["सिग्नल"].map(sort_rank)
        sig_df = sig_df.sort_values(["_sort", "समय"], ascending=[False, False]).drop(columns="_sort")

        def hl(row):
            if row["सिग्नल"] == "🔥":
                base = f"background-color:{COLOR_SPIKE_BG}"
            elif row["सिग्नल"] == "⭐⭐":
                base = "background-color:#e8d4f8"
            elif "UP" in row["टाइप"]:
                base = f"background-color:{COLOR_POS_BG}"
            elif "DOWN" in row["टाइप"]:
                base = f"background-color:{COLOR_NEG_BG}"
            else:
                base = "background-color:#fff2cc"
            return [base] * len(row)

        st.dataframe(
            sig_df.style.apply(hl, axis=1), use_container_width=True, hide_index=True,
            column_config={"Chart": st.column_config.LinkColumn("Chart", display_text="📈 खोलें")},
        )

# ---------- TAB: ALERTS ----------
with tab_alerts:
    st.subheader("🔔 Signal Alerts / Notifications")
    st.caption(f"रोज़ रात {ALERT_CLEAR_HOUR_IST}:00 बजे यह लिस्ट अपने-आप खाली हो जाती है। "
               f"🔥 = Daily Volume Spike · 🟢 = EMA UP · 🔴 = EMA DOWN")
    alerts = sorted(st.session_state.alerts, key=lambda a: a["logged_at"], reverse=True)
    st.metric("कुल Active Alerts", len(alerts))
    if not alerts:
        st.info("अभी कोई अलर्ट नहीं है। Signals टैब में नया signal मिलते ही यहां अपने-आप जुड़ जाएगा।")
    else:
        adf = pd.DataFrame(alerts)[["stars", "stock", "tf", "type", "time", "logged_at", "chart"]]
        adf.columns = ["सिग्नल", "स्टॉक", "टाइमफ्रेम", "टाइप", "बार टाइम", "Alert मिला", "Chart"]

        def hl_alert(row):
            if row["सिग्नल"] == "🔥":
                base = f"background-color:{COLOR_SPIKE_BG}"
            elif "UP" in row["टाइप"]:
                base = f"background-color:{COLOR_POS_BG}"
            elif "DOWN" in row["टाइप"]:
                base = f"background-color:{COLOR_NEG_BG}"
            else:
                base = "background-color:#fff2cc"
            return [base] * len(row)

        st.dataframe(
            adf.style.apply(hl_alert, axis=1), use_container_width=True, hide_index=True,
            column_config={"Chart": st.column_config.LinkColumn("Chart", display_text="📈 खोलें")},
        )
        if st.button("🗑️ सभी Alerts अभी साफ करें"):
            st.session_state.alerts = []
            st.rerun()

# ---------- TAB: ECONOMIC CALENDAR ----------
with tab_calendar:
    st.subheader("🗓️ Global + India Economic Calendar")
    event_count = fetch_economic_event_count_today()
    if event_count is not None:
        st.metric("🔔 आज के Medium/High Importance Events", event_count)
    else:
        st.caption("आज के events की संख्या अभी लोड नहीं हो पाई — नीचे calendar में देखें।")
    st.caption("सिर्फ़ ⭐⭐/⭐⭐⭐ (Medium + High) importance वाले events दिख रहे हैं।")
    components.html("""
        <div class="tradingview-widget-container">
          <div class="tradingview-widget-container__widget"></div>
          <script type="text/javascript"
            src="https://s3.tradingview.com/external-embedding/embed-widget-events.js" async>
          {"colorTheme": "light", "isTransparent": false, "width": "100%", "height": "600",
           "locale": "en", "importanceFilter": "0,1", "countryFilter": "us,in,cn,jp,gb,eu"}
          </script>
        </div>""", height=620)

# ---------- TAB: FII/DII + NIFTY OUTLOOK ----------
with tab_fii:
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
            else:
                st.caption("Insight निकालने के लिए डाटा फॉर्मेट पहचाना नहीं जा सका — नीचे raw table देखें।")

            with st.expander("📅 पिछले 5 दिन का पूरा डाटा देखें"):
                st.dataframe(fii_df, use_container_width=True, hide_index=True)
            st.caption(f"Source: {source}")
        else:
            st.warning("किसी भी सोर्स से live FII/DII data नहीं मिल पाया।")
            st.markdown("- [StockEdge Link](https://sedg.in/p8nximtd)")
            st.markdown("- [NSE FII/DII Reports](https://www.nseindia.com/reports-indices-historical-index-data)")

    with col_nifty:
        st.markdown("### 🎯 Nifty 50 — Data-Driven Outlook")
        oc_data = fetch_nse_json("/api/option-chain-indices?symbol=NIFTY")
        if oc_data:
            try:
                records, spot = oc_data["records"]["data"], oc_data["records"]["underlyingValue"]
                call_oi, put_oi = {}, {}
                for r in records:
                    strike = r["strikePrice"]
                    if "CE" in r:
                        call_oi[strike] = r["CE"]["openInterest"]
                    if "PE" in r:
                        put_oi[strike] = r["PE"]["openInterest"]
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
                    bias = "Mildly Bullish" if pcr > 1.1 else "Mildly Bearish" if pcr < 0.8 else "Range-bound / Neutral"
                    st.info(f"📌 **{bias}** (PCR={pcr}). Support ~{support}, Resistance ~{resistance}. "
                            f"यह सिर्फ़ मौजूदा Option-OI डेटा है, guaranteed prediction नहीं।")
            except Exception:
                st.warning("Option-chain data parse नहीं हो पाया।")
        else:
            st.warning("NSE Option-Chain data नहीं मिला।")
            st.markdown("- [NSE Option Chain](https://www.nseindia.com/option-chain)")
        st.caption("यह पैनल का OI डेटा असली NSE से है, वित्तीय सलाह नहीं है।")

# ---------- TAB: DELIVERY % (2-दिन Compare) + BULK/BLOCK DEALS ----------
with tab_delivery:
    st.subheader("📦 Delivery % — पिछले 2 दिन (Compare के लिए)")
    st.caption("Data source: NSE Bhavcopy · हरा = delivery% बढ़ा · लाल = घटा")
    with st.spinner("पिछले 2 दिन का delivery data देखा जा रहा है..."):
        deliv_date, deliv_rows = get_delivery_2day_compare(selected_stocks)

    if deliv_date is None:
        st.warning("NSE Archives से data नहीं मिल पाया (cloud IP block संभव)। सीधे देखें:")
        st.markdown("- [NSE Historical Delivery Data](https://www.nseindia.com/report-detail/eq_security)")
    elif not deliv_rows:
        st.info("इस watchlist के लिए delivery data नहीं मिला।")
    else:
        ddf = pd.DataFrame(deliv_rows).sort_values("बदलाव", ascending=False)

        def hl_change(val):
            if val > 0:
                return f"background-color:{COLOR_POS_BG}; color:{COLOR_POS_TEXT}; font-weight:600;"
            if val < 0:
                return f"background-color:{COLOR_NEG_BG}; color:{COLOR_NEG_TEXT}; font-weight:600;"
            return ""

        styler = _styler_apply_map(ddf.style, hl_change, ["बदलाव"])
        st.dataframe(
            styler, use_container_width=True, hide_index=True,
            column_config={"Chart": st.column_config.LinkColumn("Chart", display_text="📈 खोलें")},
        )

    st.markdown("---")
    st.subheader("🏦 Bulk / Block Deals (आपकी Watchlist में)")
    deals_data = fetch_bulk_block_deals()
    if not deals_data:
        st.caption("आज का Bulk/Block deal data अभी उपलब्ध नहीं (NSE से fetch नहीं हो पाया)।")
    else:
        bulk = filter_deals_for_watchlist(deals_data.get("BULK_DEALS_DATA", []), selected_stocks)
        block = filter_deals_for_watchlist(deals_data.get("BLOCK_DEALS_DATA", []), selected_stocks)
        if bulk is not None and not bulk.empty:
            st.markdown("**Bulk Deals**")
            st.dataframe(bulk, use_container_width=True, hide_index=True)
        if block is not None and not block.empty:
            st.markdown("**Block Deals**")
            st.dataframe(block, use_container_width=True, hide_index=True)
        if (bulk is None or bulk.empty) and (block is None or block.empty):
            st.caption("आपकी watchlist के किसी स्टॉक में आज कोई Bulk/Block deal नहीं मिला।")

# ---------- TAB: TOP GAINERS / LOSERS ----------
with tab_movers:
    st.subheader("🏆 Top 5 Gainers & Top 5 Losers")
    st.caption("🟢▲ = ऊपर · 🔴▼ = नीचे")
    quotes = get_quotes([yf_ticker_for_stock(s) for s in selected_stocks])
    mv_rows = []
    for s in selected_stocks:
        qd = quotes.get(yf_ticker_for_stock(s))
        if qd:
            mv_rows.append({"Stock": s, "LTP": qd["price"], "pct": qd["pct"], "chg": qd.get("chg"),
                             "Chart": tv_link(tv_symbol_for_stock(s))})
    mv_df = pd.DataFrame(mv_rows)
    if mv_df.empty:
        st.info("डेटा लोड हो रहा है, थोड़ी देर में refresh करें।")
    else:
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

# ---------- TAB: NEWS (सुबह 8:30 - रात 8:30 विंडो) ----------
with tab_news:
    st.subheader("📰 News (सुबह 8:30 – रात 8:30 · Institutional-style)")
    st.caption("🏦 पहले NSE Direct Corporate Filings, फिर टारगेटेड कीवर्ड सर्च — हर खबर पर sentiment टैग। "
               "रात 8:30 के बाद यह टैब अपने-आप खाली हो जाता है और सुबह 8:30 से फिर शुरू होता है।")

    if not news_window_active():
        st.info("⏰ अभी News विंडो (8:30 AM – 8:30 PM) के बाहर है — कल सुबह 8:30 बजे फिर से news दिखेगी।")
    else:
        st.markdown("### 🏦 Direct Exchange Filings (NSE Corporate Announcements)")
        with st.spinner("NSE filings लाई जा रही हैं..."):
            filings = fetch_nse_corporate_announcements()
        watch_set = set(selected_stocks)
        relevant_filings = [f for f in filings if f["symbol"] in watch_set]
        if not relevant_filings:
            st.info("आपकी watchlist से जुड़ी कोई ताज़ा corporate filing अभी नहीं मिली।")
        else:
            for f in relevant_filings[:15]:
                link = f["attachment"] or "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
                st.markdown(f"- **{f['symbol']}** — [{f['subject']}]({link})  \n  _{f['time']}_")

        st.markdown("---")

        @st.cache_data(ttl=300, show_spinner=False)
        def fetch_keyword_news(keyword):
            if feedparser is None:
                return []
            query = urllib.parse.quote_plus(f"{keyword} when:1d")
            url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
            try:
                feed = feedparser.parse(requests.get(url, timeout=15).content)
            except Exception:
                return []
            cutoff = news_cutoff_utc()
            items = []
            for e in feed.entries[:10]:
                pub = e.get("published_parsed")
                if pub:
                    pub_dt = datetime(*pub[:6], tzinfo=timezone.utc)
                    if pub_dt >= cutoff:
                        items.append({"title": e.title, "link": e.link, "published": pub_dt, "keyword": keyword})
            return items

        if feedparser is None:
            st.error("`feedparser` install नहीं है।")
        else:
            st.markdown("### 🎯 टारगेटेड मार्केट-मूविंग न्यूज़ (सुबह 8:30 से अब तक)")
            with st.spinner("कीवर्ड-आधारित न्यूज़ लाई जा रही है..."):
                all_items = []
                for kw in NEWS_KEYWORDS:
                    all_items.extend(fetch_keyword_news(kw))
                seen, dedup_items = set(), []
                for it in all_items:
                    if it["link"] not in seen:
                        seen.add(it["link"])
                        dedup_items.append(it)
                dedup_items.sort(key=lambda x: x["published"], reverse=True)

            if not dedup_items:
                st.info("आज सुबह 8:30 से अब तक कोई नई मार्केट-मूविंग खबर नहीं मिली।")
            else:
                for it in dedup_items[:25]:
                    t = it["published"].astimezone(IST).strftime("%d-%b %H:%M")
                    st.markdown(f"- {tag_news(it['title'])} — [{it['title']}]({it['link']})  \n  _कीवर्ड: {it['keyword']} · {t} IST_")
            st.caption("सभी headlines Google News की live RSS feed से हैं।")
