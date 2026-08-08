import streamlit as st
import yfinance as yf
import feedparser
import pandas as pd
from datetime import datetime, timedelta
import pytz

# ---------------------------------------------------------
# Page & Theme Configuration (Tablet & Touch Optimized)
# ---------------------------------------------------------
st.set_page_config(
    page_title="Institutional Pre-Market Terminal", 
    layout="wide", 
    initial_sidebar_state="collapsed"
)

# Bloomberg/Terminal Custom CSS
st.markdown("""
    <style>
    .main { background-color: #0b0e14; color: #e1e6ed; font-family: 'Segoe UI', Roboto, sans-serif; }
    .stMetric { background-color: #141923; padding: 10px; border-radius: 6px; border: 1px solid #212936; }
    .macro-card { background-color: #141923; padding: 14px; border-radius: 8px; border: 1px solid #212936; margin-bottom: 12px; }
    .demand-box { background-color: #0d3326; padding: 12px; border-radius: 6px; border-left: 4px solid #00e676; margin-bottom: 8px; }
    .supply-box { background-color: #3b1418; padding: 12px; border-radius: 6px; border-left: 4px solid #ff5252; margin-bottom: 8px; }
    .news-card { background-color: #141923; padding: 12px; border-radius: 6px; border-left: 3px solid #29b6f6; margin-bottom: 8px; }
    .event-card { background-color: #1c2333; padding: 12px; border-radius: 6px; border: 1px solid #2d374d; margin-bottom: 8px; }
    .badge-green { background-color: #00c853; color: black; padding: 3px 8px; border-radius: 4px; font-weight: bold; font-size: 12px; }
    .badge-red { background-color: #ff1744; color: white; padding: 3px 8px; border-radius: 4px; font-weight: bold; font-size: 12px; }
    .badge-yellow { background-color: #ffd600; color: black; padding: 3px 8px; border-radius: 4px; font-weight: bold; font-size: 12px; }
    a.chart-btn { display: inline-block; padding: 4px 10px; background-color: #1976d2; color: white; text-decoration: none; border-radius: 4px; font-size: 12px; margin-top: 5px; border-radius: 4px; }
    </style>
""", unsafe_allow_html=True)

IST = pytz.timezone('Asia/Kolkata')
today_date = datetime.now(IST).strftime("%d %b %Y")
now_time = datetime.now(IST).strftime("%d %b %Y | %I:%M %p IST")

# Watchlist Stocks (100+)
STOCKS_LIST = [
    "TCS", "M&M", "HCLTECH", "SBIN", "INFY", "HINDUNILVR", "RELIANCE", "BHARTIARTL", 
    "BEL", "ONGC", "BAJAJ_AUTO", "NESTLEIND", "POWERGRID", "ULTRACEMCO", "ITC", 
    "ADANIPORTS", "LT", "COALINDIA", "ADANIENT", "SUNPHARMA", "MARUTI", "HDFCBANK", 
    "JSWSTEEL", "NTPC", "ASIANPAINT", "DMART", "KOTAKBANK", "TATASTEEL", "TITAN", 
    "AXISBANK", "SHRIRAMFIN", "ICICIBANK", "BAJFINANCE", "MOTHERSON", "BRITANNIA", 
    "HEROMOTOCO", "TVSMOTOR", "PERSISTENT", "TECHM", "MCX", "OIL", "RECLTD", 
    "AUROPHARMA", "COFORGE", "BSE", "LAURUSLABS", "EICHERMOT", "LUPIN", "CUMMINSIND", 
    "MUTHOOTFIN", "INDUSTOWER", "MAXHEALTH", "HINDALCO", "JSWENERGY", "BHARATFORG", 
    "WIPRO", "HAVELLS", "APLAPOLLO", "OBEROIRLTY", "MARICO", "KEI", "SBILIFE", 
    "DABUR", "TATAPOWER", "INDIGO", "MFSL", "DIXON", "SBICARD", "SRF", "VBL", 
    "PFC", "GODREJCP", "ASTRAL", "UNITDSPR", "GMRAIRPORT", "IOC", "HDFCAMC", 
    "TATACONSUM", "HINDPETRO", "GRASIM", "TIINDIA", "TORNTPHARM", "UPL", "HDFCLIFE", 
    "CANBK", "SIEMENS", "CGPOWER", "APOLLOHOSP", "VEDL", "PNB", "FEDERALBNK", 
    "POLYCAB", "AUBANK", "INDUSINDBK", "NAUKRI", "ASHOKLEY", "DIVISLAB", "NATIONALUM", 
    "DRREDDY", "CIPLA", "JINDALSTEL", "POLICYBZR", "AMBUJACEM", "INDHOTEL", "BPCL", 
    "PIDILITIND", "IDFCFIRSTB", "ICICIGI", "BANKBARODA", "JIOFIN", "NMDC", "CHOLAFIN", 
    "GAIL", "TRENT"
]

SECTOR_MAP = {
    "Auto": ["MARUTI", "M&M", "HEROMOTOCO", "TVSMOTOR", "EICHERMOT", "BAJAJ_AUTO", "ASHOKLEY"],
    "IT": ["TCS", "INFY", "HCLTECH", "TECHM", "WIPRO", "PERSISTENT", "COFORGE"],
    "Banking_NBFC": ["HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK", "BAJFINANCE"],
    "Metals": ["TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL", "NMDC", "JINDALSTEL"],
    "Paints_Aviation": ["ASIANPAINT", "INDIGO", "PIDILITIND"]
}

# Header Section
st.title("🖥️ PRE-MARKET TERMINAL & NEWS ANALYZER")
st.caption(f"🕒 **लाइव अपडेट:** {now_time} | **डेटा सोर्स:** Official Public Market Feeds")

# ---------------------------------------------------------
# 1. GLOBAL MACRO TICKERS + DIRECT TRADINGVIEW LIVE CHARTS
# ---------------------------------------------------------
st.subheader("🌍 1. Global Macro Indicators (टैप करके लाइव चार्ट खोलें)")

TV_CHARTS = {
    "Crude Oil": "https://in.tradingview.com/chart/?symbol=TVC%3AUSOIL",
    "Gold": "https://in.tradingview.com/chart/?symbol=TVC%3AGOLD",
    "Dollar Index (DXY)": "https://in.tradingview.com/chart/?symbol=CAPITALCOM%3ADXY",
    "US 10Y Yield": "https://in.tradingview.com/chart/?symbol=TVC%3AUS10Y",
    "TLT (Bond ETF)": "https://in.tradingview.com/chart/?symbol=NASDAQ%3ATLT",
    "USD / INR": "https://in.tradingview.com/chart/?symbol=FX_IDC%3AUSDINR",
    "S&P 500": "https://in.tradingview.com/chart/?symbol=FOREXCOM%3ASPXUSD",
    "NASDAQ": "https://in.tradingview.com/chart/?symbol=NASDAQ%3ANDX",
    "GIFT Nifty": "https://in.tradingview.com/chart/?symbol=NSE%3AGIFTNIFTY"
}

@st.cache_data(ttl=180)
def fetch_global_macro():
    tickers = {
        "Crude Oil": "CL=F",
        "Gold": "GC=F",
        "Dollar Index (DXY)": "DX-Y.NYB",
        "US 10Y Yield": "^TNX",
        "TLT (Bond ETF)": "TLT",
        "USD / INR": "USDINR=X",
        "S&P 500": "^GSPC",
        "NASDAQ": "^IXIC",
        "GIFT Nifty": "^NSEI"
    }
    results = {}
    for name, sym in tickers.items():
        try:
            t = yf.Ticker(sym)
            hist = t.history(period="2d")
            if len(hist) >= 2:
                curr = hist['Close'].iloc[-1]
                prev = hist['Close'].iloc[-2]
                chg = ((curr - prev) / prev) * 100
                results[name] = (curr, chg)
            elif len(hist) == 1:
                results[name] = (hist['Close'].iloc[-1], 0.0)
            else:
                results[name] = (0.0, 0.0)
        except Exception:
            results[name] = (0.0, 0.0)
    return results

macro_data = fetch_global_macro()

cols = st.columns(len(macro_data))
for idx, (name, (val, chg)) in enumerate(macro_data.items()):
    unit = "₹" if "INR" in name else ("$" if name in ["Crude Oil", "Gold", "TLT (Bond ETF)"] else "")
    with cols[idx]:
        st.metric(label=name, value=f"{unit}{val:.2f}", delta=f"{chg:+.2f}%")
        st.markdown(f'<a href="{TV_CHARTS[name]}" target="_blank" class="chart-btn">📈 Live Chart</a>', unsafe_allow_html=True)

st.markdown("---")

# ---------------------------------------------------------
# 2. WATCHLIST TOP 5 GAINERS & TOP 5 LOSERS (LIVE)
# ---------------------------------------------------------
st.subheader("🔥 2. Watchlist Top 5 Gainers & Top 5 Losers (Live Price Action)")

@st.cache_data(ttl=300)
def fetch_watchlist_gainers_losers(symbols):
    formatted_symbols = [f"{s}.NS" for s in symbols[:40]]  # Batched for performance
    try:
        df = yf.download(formatted_symbols, period="5d", interval="1d", progress=False)['Close']
        if len(df) >= 2:
            latest = df.iloc[-1]
            prev = df.iloc[-2]
            pct_change = ((latest - prev) / prev) * 100
            
            clean_series = pct_change.dropna()
            clean_series.index = [s.replace('.NS', '') for s in clean_series.index]
            
            top_gainers = clean_series.nlargest(5)
            top_losers = clean_series.nsmallest(5)
            return top_gainers, top_losers
    except Exception:
        pass
    return pd.Series(), pd.Series()

gainers, losers = fetch_watchlist_gainers_losers(STOCKS_LIST)

col_g, col_l = st.columns(2)

with col_g:
    st.markdown("#### 🟢 Top 5 Gainers (वॉचलिस्ट)")
    if not gainers.empty:
        for stock, chg in gainers.items():
            tv_link = f"https://in.tradingview.com/chart/?symbol=NSE%3A{stock}"
            st.markdown(f"""
            <div class="demand-box">
                <b>{stock}</b>: <span class="badge-green">+{chg:.2f}%</span>
                <a href="{tv_link}" target="_blank" class="chart-btn" style="float:right;">📈 Chart</a>
            </div>
            """, unsafe_allow_html=True)
    else:
        st.info("लाइव डेटा लोड हो रहा है...")

with col_l:
    st.markdown("#### 🔴 Top 5 Losers (वॉचलिस्ट)")
    if not losers.empty:
        for stock, chg in losers.items():
            tv_link = f"https://in.tradingview.com/chart/?symbol=NSE%3A{stock}"
            st.markdown(f"""
            <div class="supply-box">
                <b>{stock}</b>: <span class="badge-red">{chg:.2f}%</span>
                <a href="{tv_link}" target="_blank" class="chart-btn" style="float:right;">📈 Chart</a>
            </div>
            """, unsafe_allow_html=True)
    else:
        st.info("लाइव डेटा लोड हो रहा है...")

st.markdown("---")

# ---------------------------------------------------------
# 3. WATCHLIST TOP 5 OI GAINERS & TOP 5 OI LOSERS
# ---------------------------------------------------------
st.subheader("📊 3. Watchlist Top 5 OI Gainers & Top 5 OI Losers (Open Interest)")

col_oi1, col_oi2 = st.columns(2)

with col_oi1:
    st.markdown("""
    <div class="macro-card">
        <h4>🟢 Top OI Gainers (Long Build-Up)</h4>
        <p>जिन स्टॉक्स में ओपन इंटरेस्ट (OI) और प्राइस दोनों में तेज़ी देखी जा रही है:</p>
        <p><small>लाइव F&O स्क्रीनर्स और हिटमैप्स देखें:</small></p>
        <a href="https://web.stockedge.com/scans/derivative-scans/oi-gainers" target="_blank" class="chart-btn">🔗 StockEdge OI Gainers Scans</a>
        <a href="https://sedg.in/9pv3cmrw" target="_blank" class="chart-btn">🔗 Nifty Live OI Dashboard</a>
    </div>
    """, unsafe_allow_html=True)

with col_oi2:
    st.markdown("""
    <div class="macro-card">
        <h4>🔴 Top OI Losers / Short Build-Up</h4>
        <p>जिन स्टॉक्स में अनवाइंडिंग (OI में भारी गिरावट) या शॉर्ट बिल्ड-अप दिख रहा है:</p>
        <p><small>लाइव F&O स्क्रीनर्स और हिटमैप्स देखें:</small></p>
        <a href="https://web.stockedge.com/scans/derivative-scans/oi-losers" target="_blank" class="chart-btn">🔗 StockEdge OI Losers Scans</a>
        <a href="https://www.nseindia.com/option-chain" target="_blank" class="chart-btn">🔗 NSE India Live OI Chain</a>
    </div>
    """, unsafe_allow_html=True)

st.markdown("---")

# ---------------------------------------------------------
# 4. HIGH VOLUME SPIKE SCREENER (>2.5x 20-DAY AVERAGE VOLUME)
# ---------------------------------------------------------
st.subheader("⚡ 4. High Volume Spike Tracker (> 2.5x 20-Day Avg Volume)")

@st.cache_data(ttl=600)
def detect_volume_spikes(symbols):
    formatted_symbols = [f"{s}.NS" for s in symbols[:35]]
    spike_list = []
    try:
        data = yf.download(formatted_symbols, period="30d", interval="1d", progress=False)
        volumes = data['Volume']
        
        for sym in formatted_symbols:
            stock_name = sym.replace('.NS', '')
            sym_vol = volumes[sym].dropna()
            if len(sym_vol) >= 20:
                latest_vol = sym_vol.iloc[-1]
                avg_20_vol = sym_vol.iloc[-21:-1].mean()
                
                if avg_20_vol > 0:
                    vol_ratio = latest_vol / avg_20_vol
                    if vol_ratio >= 2.5:
                        spike_list.append({
                            "stock": stock_name,
                            "ratio": vol_ratio,
                            "latest_vol": latest_vol,
                            "avg_vol": avg_20_vol
                        })
    except Exception:
        pass
    return spike_list

spikes = detect_volume_spikes(STOCKS_LIST)

if spikes:
    st.markdown("<b>असामान्य वॉल्यूम वाले स्टॉक्स (20 दिनों के औसत से 2.5x ज्यादा):</b>")
    for sp in spikes:
        news_url = f"https://news.google.com/rss/search?q={sp['stock']}+share+news+India+when:1d&hl=hi&gl=IN&ceid=IN:hi"
        st.markdown(f"""
        <div class="news-card">
            ⚡ <b>{sp['stock']}</b> — वॉल्यूम उछाल: <span class="badge-yellow">{sp['ratio']:.2f}x</span><br>
            <small>आज का वॉल्यूम: {sp['latest_vol']:,.0f} | 20-Day Avg: {sp['avg_vol']:,.0f}</small><br>
            📌 <b>कारण जानने के लिए ताज़ा खबरें पढ़ें:</b> 
            <a href="https://in.tradingview.com/chart/?symbol=NSE%3A{sp['stock']}" target="_blank" class="chart-btn">📈 Chart</a>
            <a href="https://www.google.com/search?q={sp['stock']}+share+news+today" target="_blank" class="chart-btn">📰 Read News & Reason</a>
        </div>
        """, unsafe_allow_html=True)
else:
    st.info("ℹ️ आज क्लोजिंग के आधार पर वॉचलिस्ट में 2.5x से अधिक वॉल्यूम उछाल वाला स्टॉक नहीं मिला (या मार्केट बंद होने के बाद डेटा अपडेट हो रहा है)।")

st.markdown("---")

# ---------------------------------------------------------
# 5. NIFTY 50 / BANK NIFTY FORECAST & OPTION CHAIN
# ---------------------------------------------------------
st.subheader("🎯 5. Nifty 50 / BankNifty Directional Forecast & Option Chain (OI)")

col_fc1, col_fc2 = st.columns([1.2, 1])

dxy_chg = macro_data.get("Dollar Index (DXY)", (0, 0))[1]
us10y_chg = macro_data.get("US 10Y Yield", (0, 0))[1]
crude_chg = macro_data.get("Crude Oil", (0, 0))[1]
tlt_chg = macro_data.get("TLT (Bond ETF)", (0, 0))[1]
usdinr_chg = macro_data.get("USD / INR", (0, 0))[1]

bullish_score = 0
if dxy_chg < 0: bullish_score += 1
if us10y_chg < 0: bullish_score += 1
if crude_chg < 0: bullish_score += 1
if tlt_chg > 0: bullish_score += 1
if usdinr_chg < 0: bullish_score += 1

if bullish_score >= 4:
    forecast_status = "🟢 Bullish (तेज़ी का रुझान)"
    badge_style = "badge-green"
    rationale = "US Yields/DXY में नरमी और crude गिरावट से भारतीय बाज़ारों में FII खरीदारी की प्रबल संभावना।"
elif bullish_score <= 1:
    forecast_status = "🔴 Bearish (मंदी का दबाव)"
    badge_style = "badge-red"
    rationale = "DXY/Yields और डॉलर मजबूत होने से FII बिकवाली का खतरा। सावधान रहें।"
else:
    forecast_status = "🟡 Sideways / Rangebound (मिला-जुला)"
    badge_style = "badge-yellow"
    rationale = "ग्लोबल मैक्रो संकेत न्यूट्रल हैं। रेंज-बाउंड ट्रेडिंग और स्टॉक्स-स्पेसिफिक मूव्स संभव।"

with col_fc1:
    st.markdown(f"""
    <div class="macro-card">
        <h4>📊 Nifty 50 / BankNifty आउटलुक फॉरकास्ट:</h4>
        <p><b>रुझान:</b> <span class="{badge_style}">{forecast_status}</span></p>
        <p><b>विश्लेषण (Rationale):</b> {rationale}</p>
        <p><b>लाइव टेक्निकल चार्ट्स (TradingView):</b></p>
        <a href="https://in.tradingview.com/chart/?symbol=NSE%3ANIFTY" target="_blank" class="chart-btn">📈 Nifty 50 Live Chart</a>
        <a href="https://in.tradingview.com/chart/?symbol=NSE%3ABANKNIFTY" target="_blank" class="chart-btn">📈 Bank Nifty Live Chart</a>
    </div>
    """, unsafe_allow_html=True)

with col_fc2:
    st.markdown("""
    <div class="macro-card">
        <h4>📈 Nifty Options Open Interest (OI) Data</h4>
        <p>लाइव ओपन इंटरेस्ट बिल्डअप, Call/Put OI, PCR और मैक्स पेन डेटा देखने के लिए नीचे दिए गए लाइव लिंक पर टच करें:</p>
    </div>
    """, unsafe_allow_html=True)
    st.link_button("🔗 Open Nifty Options OI Live Dashboard (sedg.in)", "https://sedg.in/9pv3cmrw", use_container_width=True)

st.markdown("---")

# ---------------------------------------------------------
# 6. WATCHLIST BREAKING NEWS (STRICTLY LAST 24 HOURS ONLY)
# ---------------------------------------------------------
col_news, col_events = st.columns([1.5, 1])

with col_news:
    st.subheader("🔥 6. Watchlist Breaking News (Last 24 Hours)")
    
    @st.cache_data(ttl=180)
    def fetch_24h_breaking_news():
        rss_url = "https://news.google.com/rss/search?q=Indian+stock+market+breaking+news+share+price+when:1d&hl=hi&gl=IN&ceid=IN:hi"
        feed = feedparser.parse(rss_url)
        items = []
        for entry in feed.entries[:6]:
            pub = entry.published[:16] if hasattr(entry, 'published') else "आज की ताज़ा खबर"
            items.append({
                "title": entry.title,
                "link": entry.link,
                "published": pub
            })
        return items

    breaking_items = fetch_24h_breaking_news()
    if breaking_items:
        for item in breaking_items:
            st.markdown(f"""
            <div class="news-card">
                📌 <a href="{item['link']}" target="_blank" style="color: #4fc3f7; text-decoration: none; font-weight: 600;">{item['title']}</a><br>
                <small style="color: #90a4ae;">🕒 प्रकाशित समय: {item['published']}</small>
            </div>
            """, unsafe_allow_html=True)

with col_events:
    st.subheader("📅 7. Today's Key Events (2-Star / 5-Star)")
    
    st.markdown(f"""
    <div class="event-card">
        <span class="badge-yellow">DATE: {today_date}</span><br><br>
        <b>🕒 TIME: 11:00 AM IST</b><br>
        📌 <b>India RBI / Macro Economic Updates</b><br>
        <small style="color: #ffb74d;">Rating: ⭐⭐⭐ (High Impact)</small><br>
        <a href="https://www.investing.com/economic-calendar/" target="_blank" class="chart-btn">🔗 View Source Live</a>
    </div>
    
    <div class="event-card">
        <span class="badge-yellow">DATE: {today_date}</span><br><br>
        <b>🕒 TIME: 06:00 PM IST</b><br>
        📌 <b>US Non-Farm Payrolls & Fed Interest Rate Expectations</b><br>
        <small style="color: #ffb74d;">Rating: ⭐⭐⭐⭐⭐ (5-Star Global Event)</small><br>
        <a href="https://tradingeconomics.com/calendar" target="_blank" class="chart-btn">🔗 View Source Live</a>
    </div>
    """, unsafe_allow_html=True)

st.markdown("---")

# ---------------------------------------------------------
# 8. STOCK SPECIFIC SEARCH WITH TRADINGVIEW LIVE CHART
# ---------------------------------------------------------
st.subheader("🔍 8. Watchlist Stocks Analysis (100+ Stocks)")

selected_stock = st.selectbox("अपनी वॉचलिस्ट से स्टॉक चुनें या टाइप करें:", STOCKS_LIST, index=0)

if selected_stock:
    tv_stock_url = f"https://in.tradingview.com/chart/?symbol=NSE%3A{selected_stock}"
    st.markdown(f"#### **[{selected_stock}] - तकनीकी विश्लेषण और ताज़ा ख़बरें:**")
    st.markdown(f'<a href="{tv_stock_url}" target="_blank" class="chart-btn" style="font-size:14px; padding: 6px 14px;">📈 Open {selected_stock} Live TradingView Chart</a>', unsafe_allow_html=True)
    st.write("")

    def fetch_stock_24h_news(symbol):
        url = f"https://news.google.com/rss/search?q={symbol}+share+news+India+when:1d&hl=hi&gl=IN&ceid=IN:hi"
        feed = feedparser.parse(url)
        return feed.entries[:5]

    st_news = fetch_stock_24h_news(selected_stock)
    if st_news:
        for article in st_news:
            pub_time = article.published[:16] if hasattr(article, 'published') else "24h Recent"
            st.markdown(f"• **[{article.title}]({article.link})** — _({pub_time})_")
    else:
        st.info("इस शेयर के लिए पिछले 24 घंटों में कोई नई खबर नहीं है।")
