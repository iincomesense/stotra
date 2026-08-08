import os
import requests
import yfinance as yf
import pandas as pd
import streamlit as st
import datetime
import pytz
import xml.etree.ElementTree as ET
from urllib.parse import quote
from email.utils import parsedate_to_datetime

# -----------------------------------------------------------------------------
# 1. Page Configuration & Senior Mobile/Tablet UX CSS
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="PRO LIVE MARKET ALERTS & NEWS TERMINAL",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Custom Responsive Dark Theme CSS for Mobile & Tablet
st.markdown("""
<style>
    .stApp { background-color: #0d1117; color: #c9d1d9; }
    
    .header-box {
        background: linear-gradient(135deg, #1f2937 0%, #111827 100%);
        padding: 15px; border-radius: 12px; border: 1px solid #374151;
        text-align: center; margin-bottom: 20px;
    }
    
    /* Responsive Alert Cards */
    .alert-card {
        background-color: #161b22;
        border-left: 5px solid #ef4444;
        border-radius: 8px; padding: 12px; margin-bottom: 12px;
        border-top: 1px solid #30363d; border-right: 1px solid #30363d; border-bottom: 1px solid #30363d;
    }
    .alert-bullish { border-left-color: #22c55e !important; }
    .alert-bearish { border-left-color: #ef4444 !important; }
    .alert-volume  { border-left-color: #eab308 !important; }

    .tag {
        display: inline-block; padding: 2px 8px; border-radius: 4px;
        font-size: 0.75rem; font-weight: bold; color: #ffffff; margin-right: 5px;
    }
    .tag-time { background-color: #3b82f6; }
    .tag-type { background-color: #8b5cf6; }

    .btn-link {
        display: inline-block; padding: 6px 12px; font-size: 0.8rem;
        font-weight: 600; color: #ffffff !important; background-color: #2563eb;
        border-radius: 6px; text-decoration: none !important; margin-top: 8px;
    }
    .btn-link:hover { background-color: #1d4ed8; }
</style>
""", unsafe_allow_html=True)

# -----------------------------------------------------------------------------
# 2. Time & Session Management (4:00 PM Auto-Delete/Reset Logic)
# -----------------------------------------------------------------------------
ist_tz = pytz.timezone('Asia/Kolkata')
now_ist = datetime.datetime.now(ist_tz)

if 'alerts_history' not in st.session_state:
    st.session_state.alerts_history = []
if 'last_reset_date' not in st.session_state:
    st.session_state.last_reset_date = now_ist.date()

# शाम 4:00 PM (16:00 IST) के बाद या नए दिन की शुरुआत में अलर्ट डिलीट/रीसेट
if now_ist.hour >= 16 or st.session_state.last_reset_date != now_ist.date():
    st.session_state.alerts_history = []
    st.session_state.last_reset_date = now_ist.date()

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

WATCHLIST = ["TCS", "M&M", "HCLTECH", "SBIN", "INFY", "RELIANCE", "BHARTIARTL", "BEL", "ONGC", "TATAMOTORS", "HDFCBANK", "ICICIBANK", "BAJFINANCE"]

def send_telegram(msg):
    if BOT_TOKEN and CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            requests.post(url, data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=5)
        except Exception:
            pass

# -----------------------------------------------------------------------------
# 3. Multi-Timeframe EMA & Volume Alert Engine
# -----------------------------------------------------------------------------
def analyze_stock_timeframe(stock_sym, df, tf_label):
    if len(df) < 50:
        return []

    alerts = []
    df = df.copy()
    
    # EMA 20 & EMA 50 Calculation
    df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
    df['EMA50'] = df['Close'].ewm(span=50, adjust=False).mean()
    
    # 20-Period Average Volume Calculation
    df['VolAvg20'] = df['Volume'].rolling(window=20).mean()

    curr = df.iloc[-1]
    prev = df.iloc[-2]

    curr_price = curr['Close']
    curr_vol = curr['Volume']
    avg_vol = curr['VolAvg20']

    # 1. EMA 20/50 Crossover Check (Cross Up & Cross Down)
    if prev['EMA20'] <= prev['EMA50'] and curr['EMA20'] > curr['EMA50']:
        alerts.append({
            "type": "EMA CROSS UP",
            "stock": stock_sym,
            "tf": tf_label,
            "msg": f"🚀 *EMA 20/50 CROSS UP ({tf_label})*: `{stock_sym}`\n▫ EMA 20 ने EMA 50 को ऊपर की ओर काटा\n▫ LTP: ₹{curr_price:.2f}",
            "bullish": True
        })
    elif prev['EMA20'] >= prev['EMA50'] and curr['EMA20'] < curr['EMA50']:
        alerts.append({
            "type": "EMA CROSS DOWN",
            "stock": stock_sym,
            "tf": tf_label,
            "msg": f"🚨 *EMA 20/50 CROSS DOWN ({tf_label})*: `{stock_sym}`\n▫ EMA 20 ने EMA 50 को नीचे की ओर काटा\n▫ LTP: ₹{curr_price:.2f}",
            "bullish": False
        })

    # 2. Volume > 2x 20-Period Average Volume Check
    if avg_vol > 0 and (curr_vol / avg_vol) >= 2.0:
        ratio = curr_vol / avg_vol
        alerts.append({
            "type": "2X VOLUME SPIKE",
            "stock": stock_sym,
            "tf": tf_label,
            "msg": f"⚡ *2X VOLUME SPIKE ({tf_label})*: `{stock_sym}`\n▫ वॉल्यूम उछाल: {ratio:.1f}x (20d Avg से दोगुना)\n▫ LTP: ₹{curr_price:.2f}",
            "bullish": None
        })

    return alerts

def scan_all_timeframes():
    symbols = [f"{s}.NS" for s in WATCHLIST]
    new_alerts = []

    try:
        # 1. Daily Data Fetch
        df_daily = yf.download(symbols, period="100d", interval="1d", progress=False)
        # 2. 1-Hour Data Fetch
        df_1h = yf.download(symbols, period="30d", interval="1h", progress=False)
        # 3. 5-Min Data Fetch (Resampled to 10-Min)
        df_5m = yf.download(symbols, period="5d", interval="5m", progress=False)

        for stock in WATCHLIST:
            sym = f"{stock}.NS"

            # Process Daily
            if 'Close' in df_daily and sym in df_daily['Close']:
                d_df = pd.DataFrame({'Close': df_daily['Close'][sym], 'Volume': df_daily['Volume'][sym]}).dropna()
                new_alerts.extend(analyze_stock_timeframe(stock, d_df, "Daily"))

            # Process 1-Hour
            if 'Close' in df_1h and sym in df_1h['Close']:
                h_df = pd.DataFrame({'Close': df_1h['Close'][sym], 'Volume': df_1h['Volume'][sym]}).dropna()
                new_alerts.extend(analyze_stock_timeframe(stock, h_df, "1 Hour"))

            # Process 10-Min (Resample 5m to 10m)
            if 'Close' in df_5m and sym in df_5m['Close']:
                m_df = pd.DataFrame({'Close': df_5m['Close'][sym], 'Volume': df_5m['Volume'][sym]}).dropna()
                m10_df = m_df.resample('10min').agg({'Close': 'last', 'Volume': 'sum'}).dropna()
                new_alerts.extend(analyze_stock_timeframe(stock, m10_df, "10 Min"))

    except Exception as e:
        print(f"Error scanning market: {e}")

    # Add to session history & avoid duplicates
    for alt in new_alerts:
        alt["time"] = datetime.datetime.now(ist_tz).strftime("%I:%M %p")
        if not any(x['msg'] == alt['msg'] for x in st.session_state.alerts_history):
            st.session_state.alerts_history.insert(0, alt)
            send_telegram(alt['msg'])

# -----------------------------------------------------------------------------
# 4. Strict Zero-Cache Real-Time News Engine (< 24 Hours Filter)
# -----------------------------------------------------------------------------
def fetch_strict_24h_news(stock_name):
    """
    Fetches real-time RSS news strictly from:
    moneycontrol.com, bloomberg.com, investing.com, tradingeconomics.com, nseindia.com, stockedge.com
    Excludes any news older than 24 hours. Zero caching.
    """
    sites = "site:moneycontrol.com OR site:bloomberg.com OR site:investing.com OR site:tradingeconomics.com OR site:nseindia.com OR site:stockedge.com"
    query = f"{stock_name} share news ({sites})"
    rss_url = f"https://news.google.com/rss/search?q={quote(query)}&hl=en-IN&gl=IN&ceid=IN:en"

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Cache-Control': 'no-cache, no-store, must-revalidate',
        'Pragma': 'no-cache',
        'Expires': '0'
    }

    fresh_news = []
    now = datetime.datetime.now(datetime.timezone.utc)

    try:
        res = requests.get(rss_url, headers=headers, timeout=6)
        if res.status_code == 200:
            root = ET.fromstring(res.content)
            for item in root.findall('./channel/item')[:5]:
                title = item.find('title').text if item.find('title') is not None else 'Market News'
                link = item.find('link').text if item.find('link') is not None else '#'
                pub_date_str = item.find('pubDate').text if item.find('pubDate') is not None else None

                if pub_date_str:
                    pub_dt = parsedate_to_datetime(pub_date_str)
                    # Check if news is strictly within the last 24 hours (86400 seconds)
                    time_diff = (now - pub_dt).total_seconds()
                    if time_diff <= 86400:
                        hrs_ago = int(time_diff // 3600)
                        mins_ago = int((time_diff % 3600) // 60)
                        time_label = f"{hrs_ago}h {mins_ago}m ago" if hrs_ago > 0 else f"{mins_ago}m ago"
                        fresh_news.append({"title": title, "link": link, "time": time_label})
    except Exception as e:
        pass

    return fresh_news

# -----------------------------------------------------------------------------
# 5. UI Layout & Dashboard Render
# -----------------------------------------------------------------------------
st.markdown("""
<div class="header-box">
    <h2 style="margin:0; color:#60a5fa;">⚡ LIVE MARKET WATCHLIST TERMINAL</h2>
    <p style="margin:5px 0 0 0; font-size:0.85rem; color:#9ca3af;">
        EMA 20/50 Crossovers | 2x Volume Spikes | 10m, 1h, Daily Timeframes | Auto-Clears at 4:00 PM IST
    </p>
</div>
""", unsafe_allow_html=True)

col_left, col_right = st.columns([1.2, 1])

with col_left:
    st.subheader("🔔 आज के लाइव अलर्ट्स (4 PM तक स्टोर रहेंगे)")
    
    if st.button("🔄 स्कैन करें / रिफ्रेश करें"):
        with st.spinner("10m, 1h और Daily डेटा स्कैन हो रहा है..."):
            scan_all_timeframes()

    if now_ist.hour >= 16:
        st.warning("⏰ शाम के 4:00 PM बज चुके हैं। आज के सभी अलर्ट्स रीसेट कर दिए गए हैं।")
    elif not st.session_state.alerts_history:
        st.info("फ़िलहाल कोई अलर्ट ट्रिगर नहीं हुआ है। मार्केट ऑवर्स के दौरान स्कैन बटन दबाएं या ऑटो-रन होने दें।")
    else:
        for alt in st.session_state.alerts_history:
            cls_name = "alert-volume"
            if alt["bullish"] is True:
                cls_name = "alert-bullish"
            elif alt["bullish"] is False:
                cls_name = "alert-bearish"

            tv_url = f"https://in.tradingview.com/chart/?symbol=NSE:{alt['stock']}"

            st.markdown(f"""
            <div class="alert-card {cls_name}">
                <div>
                    <span class="tag tag-time">🕒 {alt['time']}</span>
                    <span class="tag tag-type">⏳ {alt['tf']}</span>
                    <b style="font-size:1.1rem; color:#f3f4f6;">{alt['stock']}</b>
                </div>
                <div style="margin-top:8px; font-size:0.9rem; color:#e5e7eb;">
                    {alt['msg']}
                </div>
                <a href="{tv_url}" target="_blank" class="btn-link">📈 Open Live Chart</a>
            </div>
            """, unsafe_allow_html=True)

with col_right:
    st.subheader("📰 ताज़ा real-time ख़बरें (Strictly < 24 Hours)")
    st.caption("स्रोत: Moneycontrol, Bloomberg, Investing, TradingEconomics, NSE, StockEdge")

    selected_stock = st.selectbox("वॉचलिस्ट से स्टॉक चुनें:", WATCHLIST)
    
    with st.spinner(f"{selected_stock} की लाइव ख़बरें लोड हो रही हैं..."):
        news_items = fetch_strict_24h_news(selected_stock)

    if news_items:
        for item in news_items:
            st.markdown(f"""
            <div style="background-color:#161b22; padding:10px; border-radius:6px; margin-bottom:8px; border:1px solid #30363d;">
                <a href="{item['link']}" target="_blank" style="color:#60a5fa; font-size:0.88rem; font-weight:600; text-decoration:none;">
                    🔗 {item['title']}
                </a>
                <div style="font-size:0.75rem; color:#9ca3af; margin-top:4px;">⏱️ पोस्ट हुआ: {item['time']}</div>
            </div>
            """, unsafe_allow_html=True)
    else:
        st.write("पिछले 24 घंटों के दौरान इस स्टॉक पर इन सिलेक्टेड सोर्स से कोई नई ख़बर पोस्ट नहीं हुई है।")
