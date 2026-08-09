"""
Full Market Dashboard (Streamlit) - Mobile/Tablet Friendly
=============================================================
Global Markets + TradingView charts | Stock Watchlist (110+) with
news+charts | EMA/Volume Signals | Economic Calendar | FII/DII
(NSE -> Moneycontrol fallback) | Nifty Option-OI Outlook |
Macro Sector Impact | 3-Day Rising Delivery% Highlight |
Top Gainers/Losers | Important News.

Deploy: share.streamlit.io -> connect GitHub repo -> main file: app.py
"""

import concurrent.futures
import io
import urllib.parse
from datetime import datetime, timedelta, timezone

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
CLEAR_HOUR_IST = 16

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

NEWS_SOURCES = [
    "bloomberg.com", "investing.com", "tradingeconomics.com",
    "moneycontrol.com", "nseindia.com", "stockedge.com",
]
NEWS_MAX_AGE_HOURS = 24

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}

# ============================== PAGE SETUP (mobile friendly) ==============================
st.set_page_config(page_title="Full Market Dashboard", layout="wide",
                    page_icon="📈", initial_sidebar_state="collapsed")

st.markdown("""
<style>
@media (max-width: 768px) {
    .block-container {padding-left: 0.6rem; padding-right: 0.6rem; padding-top: 1rem;}
    div[data-testid="stMetricValue"] {font-size: 1.1rem;}
    h1 {font-size: 1.4rem !important;}
    h2, h3 {font-size: 1.1rem !important;}
}
</style>
""", unsafe_allow_html=True)


def now_ist():
    return datetime.now(IST)


def tv_link(symbol):
    return f"https://www.tradingview.com/chart/?symbol={urllib.parse.quote(symbol)}"


def tv_symbol_for_stock(stock):
    return f"NSE:{TV_FIX.get(stock, stock)}"


def yf_ticker_for_stock(stock):
    return f"{YF_FIX.get(stock, stock)}.NS"


# ============================== SIDEBAR ==============================
st.sidebar.header("⚙️ Settings")
refresh_min = st.sidebar.slider("Auto-Refresh हर (मिनट)", 2, 15, 5)
if HAS_AUTOREFRESH:
    st_autorefresh(interval=refresh_min * 60 * 1000, key="auto_refresh")
else:
    st.sidebar.warning("`streamlit-autorefresh` install करें auto-refresh के लिए।")

st.sidebar.markdown(f"🕒 IST: **{now_ist().strftime('%d-%b-%Y %H:%M:%S')}**")
if st.sidebar.button("🔄 अभी Refresh करें"):
    st.cache_data.clear()
    st.rerun()

selected_stocks = st.sidebar.multiselect(
    "Watchlist", WATCHLIST_DEFAULT, default=WATCHLIST_DEFAULT,
)
vol_mult = st.sidebar.slider("Volume Spike Multiplier", 1.5, 5.0, 2.0, 0.5)
signal_timeframes = st.sidebar.multiselect(
    "Signal Scan Timeframes", ["10 Min", "1 Hour", "Daily"], default=["1 Hour", "Daily"],
)

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
                out[t] = {"price": last, "pct": (last - prev) / prev * 100}
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
        price = intraday.get(t, d["price"])
        quotes[t] = {"price": price, "pct": d["pct"]}
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
def fetch_fii_dii_stockedge():
    """StockEdge se FII/DII activity (priority source). JS-heavy site hai isliye
    best-effort — fail hone par NSE/Moneycontrol par fallback hoga."""
    urls = [
        "https://web.stockedge.com/share/fii-dii-activity",
        "https://web.stockedge.com/fii-dii-trading-activity",
    ]
    for url in urls:
        try:
            r = requests.get(url, headers=NSE_HEADERS, timeout=10)
            tables = pd.read_html(io.StringIO(r.text))
            for t in tables:
                if t.shape[1] >= 3 and t.shape[0] >= 3:
                    return t.head(5)
        except Exception:
            continue
    return None


@st.cache_data(ttl=900, show_spinner=False)
def fetch_fii_dii_moneycontrol():
    """NSE aur StockEdge dono block ho to Moneycontrol se fallback scrape."""
    url = "https://www.moneycontrol.com/stocks/marketstats/fii_dii_activity/index.php"
    try:
        r = requests.get(url, headers=NSE_HEADERS, timeout=10)
        tables = pd.read_html(io.StringIO(r.text))
        for t in tables:
            if t.shape[1] >= 3 and t.shape[0] >= 3:
                return t.head(5)
    except Exception:
        pass
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


def get_last_n_trading_bhavcopies(n=4, lookback_days=15):
    results = []
    cursor = now_ist().date() - timedelta(days=1)
    tries = 0
    while len(results) < n and tries < lookback_days:
        df = fetch_bhavcopy(cursor.strftime("%d%m%Y"))
        if df is not None:
            results.append((cursor, df))
        cursor -= timedelta(days=1)
        tries += 1
    return list(reversed(results))  # oldest -> newest


def find_delivery_rising(stocks):
    data = get_last_n_trading_bhavcopies(4)
    if len(data) < 3:
        return None  # data hi nahi mila (NSE archives block)
    result = []
    for stock in stocks:
        series = []
        for date, df in data:
            try:
                deliv_cols = [c for c in df.columns if "DELIV_PER" in c.upper()]
                if not deliv_cols:
                    continue
                dcol = deliv_cols[0]
                row = df[(df["SYMBOL"].astype(str).str.strip() == stock) &
                         (df["SERIES"].astype(str).str.strip() == "EQ")]
                if not row.empty:
                    val = row.iloc[0][dcol]
                    val = float(str(val).strip())
                    series.append((date, val))
            except Exception:
                continue
        if len(series) >= 3:
            last3 = series[-3:]
            vals = [v for _, v in last3]
            if vals[0] < vals[1] < vals[2]:
                result.append({
                    "Stock": stock,
                    f"{last3[0][0].strftime('%d-%b')}": round(vals[0], 2),
                    f"{last3[1][0].strftime('%d-%b')}": round(vals[1], 2),
                    f"{last3[2][0].strftime('%d-%b')}": round(vals[2], 2),
                    "बढ़ोतरी": f"{vals[2]-vals[0]:+.2f} pts",
                    "Chart": tv_link(tv_symbol_for_stock(stock)),
                })
    return result


# ============================== TABS ==============================
(tab_global, tab_stocks, tab_signals, tab_calendar, tab_fii,
 tab_sector, tab_delivery, tab_movers, tab_news) = st.tabs([
    "🌍 Global", "📋 Watchlist", "📊 Signals", "🗓️ Calendar",
    "💰 FII/DII+Nifty", "🏭 Sector Impact", "📦 Delivery% Rising",
    "🏆 Gainers/Losers", "📰 News",
])

# ---------- TAB: GLOBAL MARKETS ----------
with tab_global:
    st.subheader("🌍 Global Markets, Currencies, Commodities & Indices — Live (TradingView)")
    st.caption("यह widget सीधे TradingView के live data से चलता है — पेज खोलते ही असल-समय भाव दिखेगा।")

    ticker_items = ",".join(
        '{"proName": "%s", "title": "%s"}' % (tvs, sym) for sym, _, _, tvs in GLOBAL_INSTRUMENTS
    )
    components.html(
        f"""
        <div class="tradingview-widget-container">
          <div class="tradingview-widget-container__widget"></div>
          <script type="text/javascript"
            src="https://s3.tradingview.com/external-embedding/embed-widget-ticker-tape.js" async>
          {{
            "symbols": [{ticker_items}],
            "showSymbolLogo": true, "isTransparent": false, "displayMode": "adaptive",
            "colorTheme": "light", "locale": "en"
          }}
          </script>
        </div>
        """, height=80,
    )

    st.markdown("&nbsp;")
    st.markdown("**हर instrument का पूरा live chart खोलने के लिए नीचे से क्लिक करें:**")
    ref_rows = [{"Symbol": sym, "Name": name, "Chart": tv_link(tvs)}
                for sym, name, _, tvs in GLOBAL_INSTRUMENTS]
    st.dataframe(
        pd.DataFrame(ref_rows), use_container_width=True, hide_index=True,
        column_config={"Chart": st.column_config.LinkColumn("Chart", display_text="📈 Live Chart खोलें")},
    )

# ---------- TAB: STOCK WATCHLIST ----------
@st.cache_data(ttl=1800, show_spinner=False)
def fetch_stock_quick_news_link(stock_name):
    """Watchlist column ke liye — sirf 24h ke andar ki latest news ka link."""
    if feedparser is None:
        return None
    query = urllib.parse.quote_plus(f"{stock_name} NSE when:1d")
    url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
    try:
        resp = requests.get(url, timeout=10)
        feed = feedparser.parse(resp.content)
    except Exception:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(hours=NEWS_MAX_AGE_HOURS)
    for e in feed.entries[:5]:
        pub = e.get("published_parsed")
        if not pub:
            continue
        pub_dt = datetime(*pub[:6], tzinfo=timezone.utc)
        if pub_dt >= cutoff:
            return e.link
    return None


def fetch_news_links_parallel(stocks):
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        futures = {ex.submit(fetch_stock_quick_news_link, s): s for s in stocks}
        for fut in concurrent.futures.as_completed(futures):
            s = futures[fut]
            try:
                results[s] = fut.result()
            except Exception:
                results[s] = None
    return results


with tab_stocks:
    st.subheader(f"📋 Stock Watchlist ({len(selected_stocks)} स्टॉक्स)")
    yf_tickers = [yf_ticker_for_stock(s) for s in selected_stocks]
    s_quotes = get_quotes(yf_tickers)

    with st.spinner("हर स्टॉक की ताज़ा news (24h) चेक हो रही है..."):
        news_links = fetch_news_links_parallel(selected_stocks)

    rows = []
    for s in selected_stocks:
        q = s_quotes.get(yf_ticker_for_stock(s))
        rows.append({
            "Stock": s,
            "LTP": f"{q['price']:.2f}" if q else "—",
            "% Chg": f"{q['pct']:+.2f}%" if q else "—",
            "Chart": tv_link(tv_symbol_for_stock(s)),
            "News (24h)": news_links.get(s),
        })
    sdf = pd.DataFrame(rows)
    st.dataframe(
        sdf, use_container_width=True, hide_index=True, height=460,
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
            resp = requests.get(url, timeout=15)
            feed = feedparser.parse(resp.content)
        except Exception:
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=NEWS_MAX_AGE_HOURS)
        items = []
        for e in feed.entries[:10]:
            pub = e.get("published_parsed")
            if not pub:
                continue
            pub_dt = datetime(*pub[:6], tzinfo=timezone.utc)
            if pub_dt < cutoff:
                continue
            items.append({"title": e.title, "link": e.link, "published": pub_dt})
        return items

    with st.spinner("News fetch हो रही है..."):
        s_news = fetch_stock_news(news_stock)
    if not s_news:
        st.info(f"{news_stock} के लिए पिछले 24 घंटे में कोई ताज़ा खबर नहीं मिली।")
    else:
        for it in s_news:
            t = it["published"].astimezone(IST).strftime("%d-%b %H:%M")
            st.markdown(f"- [{it['title']}]({it['link']})  \n  _{t} IST_")

# ---------- TAB: EMA/VOLUME SIGNALS ----------
TIMEFRAMES = {
    "10 Min": {"interval": "5m", "period": "5d", "resample": "10min", "intraday": True},
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
                {"Open": "first", "High": "max", "Low": "min",
                 "Close": "last", "Volume": "sum"}).dropna()
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
    st.caption("⭐⭐ = EMA Cross और Volume Spike दोनों एक साथ (मज़बूत सिग्नल) · ⭐ = सिर्फ एक सिग्नल")

    local_tf = st.multiselect(
        "टाइमफ्रेम चुनें", list(TIMEFRAMES.keys()),
        default=signal_timeframes, key="signals_tf_local",
    )

    is_after_close = now_ist().hour >= CLEAR_HOUR_IST
    if is_after_close:
        st.info("बाज़ार बंद — Intraday (10m/1h) सिग्नल आज के लिए hide हैं। सिर्फ Daily दिखेगा।")

    rows = []
    for tf_key in local_tf:
        cfg = TIMEFRAMES[tf_key]
        if cfg["intraday"] and is_after_close:
            continue
        tf_data = fetch_tf_data(tf_key, tuple(selected_stocks))
        for stock, df in tf_data.items():
            price = df["Close"].iloc[-1]
            bar_time = df.index[-1]
            cross = check_ema_cross(df)
            vr = check_volume_spike(df, vol_mult)
            if not cross and not vr:
                continue

            type_parts = []
            if cross:
                type_parts.append("🟢 EMA UP" if cross == "UP" else "🔴 EMA DOWN")
            if vr:
                type_parts.append(f"⚡ Volume {vr:.1f}x")
            stars = "⭐⭐" if (cross and vr) else "⭐"

            rows.append({
                "सिग्नल": stars, "स्टॉक": stock, "टाइमफ्रेम": tf_key,
                "टाइप": " + ".join(type_parts), "LTP": round(price, 2),
                "समय": bar_time.strftime("%H:%M %d-%b"),
                "Chart": tv_link(tv_symbol_for_stock(stock)),
            })

    if not rows:
        st.success("अभी कोई नया सिग्नल नहीं है।")
    else:
        sig_df = pd.DataFrame(rows)
        sig_df["_sort"] = sig_df["सिग्नल"].apply(lambda x: 2 if x == "⭐⭐" else 1)
        sig_df = sig_df.sort_values(["_sort", "समय"], ascending=[False, False]).drop(columns="_sort")

        def hl(row):
            base = "background-color:#e8d4f8" if row["सिग्नल"] == "⭐⭐" else (
                "background-color:#d4f8d4" if "UP" in row["टाइप"] else
                "background-color:#f8d4d4" if "DOWN" in row["टाइप"] else
                "background-color:#fff2cc")
            return [base] * len(row)

        st.dataframe(
            sig_df.style.apply(hl, axis=1), use_container_width=True, hide_index=True,
            column_config={"Chart": st.column_config.LinkColumn("Chart", display_text="📈 खोलें")},
        )

# ---------- TAB: ECONOMIC CALENDAR ----------
with tab_calendar:
    st.subheader("🗓️ Global + India Economic Calendar")
    st.caption("सिर्फ़ ⭐⭐/⭐⭐⭐ (Medium + High) importance वाले events दिख रहे हैं — 1-star events छुपे हैं।")
    components.html(
        """
        <div class="tradingview-widget-container">
          <div class="tradingview-widget-container__widget"></div>
          <script type="text/javascript"
            src="https://s3.tradingview.com/external-embedding/embed-widget-events.js" async>
          {
            "colorTheme": "light", "isTransparent": false, "width": "100%",
            "height": "600", "locale": "en", "importanceFilter": "0,1",
            "countryFilter": "us,in,cn,jp,gb,eu"
          }
          </script>
        </div>
        """, height=620,
    )

# ---------- TAB: FII/DII + NIFTY OUTLOOK ----------
with tab_fii:
    col_fii, col_nifty = st.columns(2)

    with col_fii:
        st.markdown("### 💰 FII / DII Activity (5 दिन)")
        se_df = fetch_fii_dii_stockedge()
        if se_df is not None:
            st.dataframe(se_df, use_container_width=True, hide_index=True)
            st.caption("Source: StockEdge (live)")
        else:
            fii_data = fetch_nse_json("/api/fiidiiTradeReact")
            if fii_data:
                fdf = pd.DataFrame(fii_data).head(5)
                st.dataframe(fdf, use_container_width=True, hide_index=True)
                st.caption("Source: NSE (fallback — StockEdge उपलब्ध नहीं)")
            else:
                mc_df = fetch_fii_dii_moneycontrol()
                if mc_df is not None:
                    st.dataframe(mc_df, use_container_width=True, hide_index=True)
                    st.caption("Source: Moneycontrol (fallback — StockEdge/NSE दोनों blocked)")
                else:
                    st.warning("StockEdge, NSE और Moneycontrol — तीनों से live data नहीं मिल पाया। सीधे देखें:")
                    st.markdown("- [StockEdge FII/DII](https://web.stockedge.com/share/fii-dii-activity)")
                    st.markdown("- [NSE FII/DII Reports](https://www.nseindia.com/reports-indices-historical-index-data)")
                    st.markdown("- [Moneycontrol FII/DII](https://www.moneycontrol.com/stocks/marketstats/fii_dii_activity/index.php)")

    with col_nifty:
        st.markdown("### 🎯 Nifty 50 — Data-Driven Outlook")
        oc_data = fetch_nse_json("/api/option-chain-indices?symbol=NIFTY")
        if oc_data:
            try:
                records = oc_data["records"]["data"]
                spot = oc_data["records"]["underlyingValue"]
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
                    bias = ("Mildly Bullish" if pcr > 1.1 else
                            "Mildly Bearish" if pcr < 0.8 else "Range-bound / Neutral")
                    st.info(f"📌 **{bias}** (PCR={pcr}). Support ~{support}, Resistance ~{resistance}. "
                            f"यह सिर्फ़ मौजूदा Option-OI डेटा है, guaranteed prediction नहीं।")
            except Exception:
                st.warning("Option-chain data parse नहीं हो पाया।")
        else:
            st.warning("NSE Option-Chain data नहीं मिला। सीधे देखें:")
            st.markdown("- [NSE Option Chain](https://www.nseindia.com/option-chain)")
            st.markdown("- [Moneycontrol Option Chain](https://www.moneycontrol.com/indices/fno/optionchain/nifty)")

        st.markdown("**🔗 Live OI + Macro + Top Analyst Summary (सीधे खोलें):**")
        st.markdown(
            "- [Sensibull — Nifty OI Analysis](https://web.sensibull.com/option-chain?tradingsymbol=NIFTY)\n"
            "- [Trendlyne — Nifty Analysis](https://trendlyne.com/equity/1897/NIFTY/nifty-50/)\n"
            "- [Upstox — Nifty Option Chain](https://upstox.com/option-chain/nse/nifty-50/)\n"
            "- [StockEdge — Market Outlook](https://web.stockedge.com/)"
        )
        st.caption("'टॉप एनालिस्ट राय' reliably auto-scrape करना संभव नहीं (JS-heavy sites) — ऊपर दिए लिंक खोलते ही "
                   "उन-उन platforms का live analysis/summary दिखेगा। यह पैनल का OI डेटा असली NSE से है, वित्तीय सलाह नहीं है।")

# ---------- TAB: SECTOR / STOCK MACRO IMPACT ----------
with tab_sector:
    st.subheader("🏭 Global + India Macro के आधार पर सेक्टर/स्टॉक Impact")
    st.caption("Rule-based heuristic — सिर्फ जानकारी के लिए, यह वित्तीय सलाह नहीं है।")

    quotes_map = get_quotes([g[2] for g in GLOBAL_INSTRUMENTS if g[2]])

    def q(yft):
        return quotes_map.get(yft)

    impact_rows = []
    dxy, usdinr, us10y = q("DX-Y.NYB"), q("INR=X"), q("^TNX")
    crude, gold, copper, natgas = q("CL=F"), q("GC=F"), q("HG=F"), q("NG=F")

    if usdinr and abs(usdinr["pct"]) >= 0.15:
        if usdinr["pct"] > 0:
            impact_rows.append({"sector": "IT / Export",
                                 "stocks": ["TCS", "INFY", "HCLTECH", "WIPRO", "TECHM", "COFORGE", "PERSISTENT"],
                                 "signal": "🟢 Positive",
                                 "reason": f"रुपया {usdinr['pct']:+.2f}% कमज़ोर — export revenue का rupee-value बढ़ता है"})
            impact_rows.append({"sector": "Oil Importers / OMC", "stocks": ["BPCL", "IOC", "HINDPETRO"],
                                 "signal": "🔴 Negative", "reason": "Import bill महंगा पड़ेगा"})
        else:
            impact_rows.append({"sector": "IT / Export",
                                 "stocks": ["TCS", "INFY", "HCLTECH", "WIPRO", "TECHM", "COFORGE", "PERSISTENT"],
                                 "signal": "🔴 Negative",
                                 "reason": f"रुपया {abs(usdinr['pct']):.2f}% मज़बूत — export margin पर दबाव"})
            impact_rows.append({"sector": "Oil Importers / OMC", "stocks": ["BPCL", "IOC", "HINDPETRO"],
                                 "signal": "🟢 Positive", "reason": "Import cost घटेगा"})

    if crude and abs(crude["pct"]) >= 0.5:
        if crude["pct"] > 0:
            impact_rows.append({"sector": "Upstream Oil (ONGC, OIL)", "stocks": ["ONGC", "OIL"],
                                 "signal": "🟢 Positive", "reason": f"Crude {crude['pct']:+.2f}% — realisation बेहतर"})
            impact_rows.append({"sector": "OMC / Aviation / Paints",
                                 "stocks": ["BPCL", "IOC", "HINDPETRO", "INDIGO", "ASIANPAINT"],
                                 "signal": "🔴 Negative", "reason": "इनपुट कॉस्ट/ATF महंगा"})
        else:
            impact_rows.append({"sector": "OMC / Aviation", "stocks": ["BPCL", "IOC", "HINDPETRO", "INDIGO"],
                                 "signal": "🟢 Positive", "reason": f"Crude {crude['pct']:+.2f}% — इनपुट कॉस्ट घटेगा"})
            impact_rows.append({"sector": "Upstream Oil", "stocks": ["ONGC", "OIL"],
                                 "signal": "🔴 Negative", "reason": "Realisation घटेगा"})

    if us10y and abs(us10y["pct"]) >= 1.0:
        tag = "🔴 Negative" if us10y["pct"] > 0 else "🟢 Positive"
        impact_rows.append({"sector": "Banks / NBFC / High-Valuation Stocks",
                             "stocks": ["HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK", "BAJFINANCE"],
                             "signal": tag,
                             "reason": f"US 10Y yield {us10y['pct']:+.2f}% — global risk-appetite/FII flow पर असर"})

    if copper and abs(copper["pct"]) >= 0.5:
        tag = "🟢 Positive" if copper["pct"] > 0 else "🔴 Negative"
        impact_rows.append({"sector": "Metals",
                             "stocks": ["HINDALCO", "VEDL", "NATIONALUM", "TATASTEEL", "JSWSTEEL", "JINDALSTEL"],
                             "signal": tag, "reason": f"Copper {copper['pct']:+.2f}% — base-metal sentiment"})

    if gold and abs(gold["pct"]) >= 0.5:
        tag = "🟢 Positive" if gold["pct"] > 0 else "🔴 Negative"
        impact_rows.append({"sector": "Gold-linked", "stocks": ["TITAN"],
                             "signal": tag, "reason": f"Gold {gold['pct']:+.2f}%"})

    if natgas and abs(natgas["pct"]) >= 1.0:
        tag = "🟢 Positive" if natgas["pct"] > 0 else "🔴 Negative"
        impact_rows.append({"sector": "Gas Utility", "stocks": ["GAIL"],
                             "signal": tag, "reason": f"Natural Gas {natgas['pct']:+.2f}%"})

    if dxy and abs(dxy["pct"]) >= 0.2:
        tag = "🔴 Negative" if dxy["pct"] > 0 else "🟢 Positive"
        impact_rows.append({"sector": "Broad Nifty / EM Risk Sentiment", "stocks": [],
                             "signal": tag,
                             "reason": f"DXY {dxy['pct']:+.2f}% — डॉलर की मज़बूती/कमज़ोरी का global risk-appetite पर असर"})

    if not impact_rows:
        st.info("आज कोई भी macro driver threshold से ऊपर move नहीं हुआ — कोई स्पष्ट सेक्टर bias नहीं।")
    else:
        for row in impact_rows:
            st.markdown(f"**{row['sector']}** — {row['signal']}")
            st.caption(row["reason"])
            if row["stocks"]:
                links_md = " &nbsp;|&nbsp; ".join(
                    f"[{s}]({tv_link(tv_symbol_for_stock(s))})" for s in row["stocks"]
                )
                st.markdown(links_md, unsafe_allow_html=True)
            st.markdown("---")

# ---------- TAB: DELIVERY % RISING (3 DAYS) ----------
with tab_delivery:
    st.subheader("📦 लगातार 3 दिन Delivery % बढ़ने वाले स्टॉक्स")
    st.caption("Data source: NSE Bhavcopy (Delivery Position). Accumulation का संकेत हो सकता है — गारंटी नहीं।")
    with st.spinner("पिछले कुछ ट्रेडिंग दिनों का delivery data देखा जा रहा है..."):
        rising = find_delivery_rising(selected_stocks)

    if rising is None:
        st.warning("NSE Archives से data नहीं मिल पाया (cloud IP block संभव)। सीधे देखें:")
        st.markdown("- [NSE Historical Delivery Data](https://www.nseindia.com/report-detail/eq_security)")
        st.markdown("- [Moneycontrol Delivery Data](https://www.moneycontrol.com/stocks/marketstats/high-delivery-vol/)")
    elif not rising:
        st.info("आपकी watchlist में अभी कोई स्टॉक लगातार 3 दिन delivery% बढ़ाता नहीं मिला।")
    else:
        rdf = pd.DataFrame(rising)
        st.dataframe(
            rdf, use_container_width=True, hide_index=True,
            column_config={"Chart": st.column_config.LinkColumn("Chart", display_text="📈 खोलें")},
        )

# ---------- TAB: TOP GAINERS / LOSERS ----------
with tab_movers:
    st.subheader("🏆 Top 5 Gainers & Top 5 Losers")
    yf_tickers = [yf_ticker_for_stock(s) for s in selected_stocks]
    quotes = get_quotes(yf_tickers)
    mv_rows = []
    for s in selected_stocks:
        qd = quotes.get(yf_ticker_for_stock(s))
        if qd:
            mv_rows.append({"Stock": s, "LTP": qd["price"], "% Chg": qd["pct"],
                             "Chart": tv_link(tv_symbol_for_stock(s))})
    mv_df = pd.DataFrame(mv_rows)
    if mv_df.empty:
        st.info("डेटा लोड हो रहा है, थोड़ी देर में refresh करें।")
    else:
        gainers = mv_df.sort_values("% Chg", ascending=False).head(5)
        losers = mv_df.sort_values("% Chg", ascending=True).head(5)
        st.markdown("#### 🟢 Top 5 Gainers")
        st.dataframe(
            gainers.style.format({"LTP": "{:.2f}", "% Chg": "{:+.2f}%"}),
            use_container_width=True, hide_index=True,
            column_config={"Chart": st.column_config.LinkColumn("Chart", display_text="📈")},
        )
        st.markdown("#### 🔴 Top 5 Losers")
        st.dataframe(
            losers.style.format({"LTP": "{:.2f}", "% Chg": "{:+.2f}%"}),
            use_container_width=True, hide_index=True,
            column_config={"Chart": st.column_config.LinkColumn("Chart", display_text="📈")},
        )

# ---------- TAB: NEWS (Current + Analyst/Important) ----------
with tab_news:
    st.subheader("📰 न्यूज़ (सिर्फ़ पिछले 24 घंटे — पूरी तरह Live)")

    @st.cache_data(ttl=600, show_spinner=False)
    def fetch_source_news(domain):
        if feedparser is None:
            return []
        query = urllib.parse.quote_plus(f"stock market site:{domain} when:1d")
        url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
        try:
            resp = requests.get(url, timeout=15)
            feed = feedparser.parse(resp.content)
        except Exception:
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=NEWS_MAX_AGE_HOURS)
        items = []
        for e in feed.entries[:12]:
            pub = e.get("published_parsed")
            if not pub:
                continue
            pub_dt = datetime(*pub[:6], tzinfo=timezone.utc)
            if pub_dt < cutoff:
                continue
            items.append({"title": e.title, "link": e.link, "domain": domain, "published": pub_dt})
            if len(items) >= 4:
                break
        return items

    @st.cache_data(ttl=600, show_spinner=False)
    def fetch_analyst_news():
        """Brokerage/analyst target-price/rating type headlines, when:1d only."""
        if feedparser is None:
            return []
        query = urllib.parse.quote_plus(
            "brokerage OR analyst target price rating Nifty OR Sensex OR stocks India when:1d")
        url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
        try:
            resp = requests.get(url, timeout=15)
            feed = feedparser.parse(resp.content)
        except Exception:
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=NEWS_MAX_AGE_HOURS)
        items = []
        for e in feed.entries[:15]:
            pub = e.get("published_parsed")
            if not pub:
                continue
            pub_dt = datetime(*pub[:6], tzinfo=timezone.utc)
            if pub_dt < cutoff:
                continue
            items.append({"title": e.title, "link": e.link, "published": pub_dt})
            if len(items) >= 8:
                break
        return items

    if feedparser is None:
        st.error("`feedparser` install नहीं है।")
    else:
        with st.spinner("ताज़ा न्यूज़ लाई जा रही है..."):
            all_items = []
            for d in NEWS_SOURCES:
                all_items.extend(fetch_source_news(d))
            analyst_items = fetch_analyst_news()

        st.markdown("### 🔴 करेंट / ब्रेकिंग न्यूज़ (Latest पहले)")
        if not all_items:
            st.info("पिछले 24 घंटों में कोई नई खबर नहीं मिली।")
        else:
            all_items_sorted = sorted(all_items, key=lambda x: x["published"], reverse=True)
            for it in all_items_sorted:
                t = it["published"].astimezone(IST).strftime("%d-%b %H:%M")
                st.markdown(f"- [{it['title']}]({it['link']})  \n  _{it['domain']} · {t} IST_")

        st.markdown("---")
        st.markdown("### ⭐ महत्वपूर्ण न्यूज़ / टॉप एनालिस्ट स्टेटमेंट")
        if not analyst_items:
            st.info("पिछले 24 घंटों में कोई analyst/brokerage headline नहीं मिली।")
        else:
            for it in analyst_items:
                t = it["published"].astimezone(IST).strftime("%d-%b %H:%M")
                st.markdown(f"- [{it['title']}]({it['link']})  \n  _{t} IST_")

        st.caption("सभी headlines Google News की live RSS feed से हैं और 24 घंटे से पुरानी कोई भी खबर filter करके हटा दी जाती है।")
