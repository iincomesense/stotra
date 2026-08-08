import streamlit as st
import yfinance as yf
import feedparser
import pandas as pd
from datetime import datetime
import pytz

# Page Configuration for Tablet View
st.set_page_config(page_title="Indian Market Macro & News Terminal", layout="wide")

# Custom Dark Theme Styling (Bloomberg/Terminal Look)
st.markdown("""
    <style>
    .main { background-color: #0e1117; color: #ffffff; }
    .stMetric { background-color: #1e222d; padding: 12px; border-radius: 8px; border: 1px solid #2a2e39; }
    .demand-box { background-color: #0e3a2f; padding: 12px; border-radius: 8px; border-left: 5px solid #00c853; margin-bottom: 10px; }
    .supply-box { background-color: #3e1818; padding: 12px; border-radius: 8px; border-left: 5px solid #ff5252; margin-bottom: 10px; }
    </style>
""", unsafe_allow_html=True)

# 1. List of 100+ Stocks provided by user
STOCKS_LIST = [
    "TCS", "M&M", "HCLTECH", "SBIN", "INFY", "HINDUNILVR", "RELIANCE", "BHARTIARTL", 
    "BEL", "ONGC", "BAJAJ_AUTO", "NESTLEIND", "POWERGRID", "ULTRACEMCO", "ITC", 
    "ADANIPORTS", "LT", "COALINDIA", "ADANIENT", "SUNPHARMA", "MARUTI", "ETERNAL", 
    "HDFCBANK", "JSWSTEEL", "NTPC", "ASIANPAINT", "DMART", "KOTAKBANK", "TATASTEEL", 
    "TITAN", "AXISBANK", "SHRIRAMFIN", "ICICIBANK", "BAJFINANCE", "MOTHERSON", 
    "BRITANNIA", "HEROMOTOCO", "TVSMOTOR", "PERSISTENT", "TECHM", "MCX", "OIL", 
    "RECLTD", "AUROPHARMA", "COFORGE", "BSE", "LAURUSLABS", "EICHERMOT", "LUPIN", 
    "CUMMINSIND", "MUTHOOTFIN", "INDUSTOWER", "MAXHEALTH", "HINDALCO", "JSWENERGY", 
    "BHARATFORG", "WIPRO", "HAVELLS", "APLAPOLLO", "TMPV", "OBEROIRLTY", "MARICO", 
    "KEI", "SBILIFE", "DABUR", "TATAPOWER", "INDIGO", "MFSL", "DIXON", "SBICARD", 
    "SRF", "VBL", "PFC", "GODREJCP", "ASTRAL", "UNITDSPR", "GMRAIRPORT", "IOC", 
    "HDFCAMC", "TATACONSUM", "HINDPETRO", "LODHA", "GRASIM", "TIINDIA", "TORNTPHARM", 
    "UPL", "HDFCLIFE", "CANBK", "SIEMENS", "CGPOWER", "APOLLOHOSP", "VEDL", "PNB", 
    "FEDERALBNK", "POLYCAB", "PHOENIXLTD", "AUBANK", "INDUSINDBK", "NAUKRI", 
    "ASHOKLEY", "DIVISLAB", "NATIONALUM", "DRREDDY", "CIPLA", "JINDALSTEL", 
    "POLICYBZR", "AMBUJACEM", "INDHOTEL", "BPCL", "PIDILITIND", "IDFCFIRSTB", 
    "ICICIGI", "BANKBARODA", "TMCV", "JIOFIN", "NMDC", "CHOLAFIN", "GAIL", "TRENT"
]

# Sector Mapping for Demand/Supply Analysis
SECTOR_MAP = {
    "Auto": ["MARUTI", "M&M", "HEROMOTOCO", "TVSMOTOR", "EICHERMOT", "BAJAJ_AUTO", "ASHOKLEY", "MOTHERSON", "BHARATFORG"],
    "IT": ["TCS", "INFY", "HCLTECH", "TECHM", "WIPRO", "PERSISTENT", "COFORGE"],
    "Banking_NBFC": ["HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK", "BAJFINANCE", "PNB", "CANBK", "INDUSINDBK"],
    "Metals": ["TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL", "NMDC", "NATIONALUM", "JINDALSTEL"],
    "Paints_Aviation_Oil": ["ASIANPAINT", "INDIGO", "BPCL", "HPCL", "IOC", "BERGEPAINT"]
}

IST = pytz.timezone('Asia/Kolkata')
now_time = datetime.now(IST).strftime("%d %b %Y | %I:%M %p IST")

# Header Section
st.title("🖥️ PRE-MARKET TERMINAL & NEWS ANALYZER")
st.caption(f"Last Refreshed: {now_time} | Public Data Feed (Zero Broker Access)")

# --- SECTION 1: GLOBAL MACRO TICKERS ---
st.subheader("🌍 1. Global Macro Indicators (Bonds, Currency, Commodities, Indices)")

@st.cache_data(ttl=300)
def fetch_global_data():
    tickers = {
        "Crude Oil": "CL=F",
        "Gold": "GC=F",
        "Dollar Index (DXY)": "DX-Y.NYB",
        "US 10Y Yield": "^TNX",
        "S&P 500": "^GSPC",
        "NASDAQ": "^IXIC",
        "GIFT Nifty": "^NSEI"
    }
    data = {}
    for name, sym in tickers.items():
        try:
            t = yf.Ticker(sym)
            hist = t.history(period="2d")
            if len(hist) >= 2:
                curr = hist['Close'].iloc[-1]
                prev = hist['Close'].iloc[-2]
                chg = ((curr - prev) / prev) * 100
                data[name] = (curr, chg)
            elif len(hist) == 1:
                data[name] = (hist['Close'].iloc[-1], 0.0)
            else:
                data[name] = (0.0, 0.0)
        except Exception:
            data[name] = (0.0, 0.0)
    return data

macro_data = fetch_global_data()

cols = st.columns(len(macro_data))
for idx, (name, (val, chg)) in enumerate(macro_data.items()):
    cols[idx].metric(label=name, value=f"{val:.2f}", delta=f"{chg:+.2f}%")

st.markdown("---")

# --- SECTION 2: DEMAND VS SUPPLY SECTOR ANALYSIS ---
st.subheader("📊 2. Pre-Market Impact Analysis (Demand vs Supply Zones)")

crude_chg = macro_data.get("Crude Oil", (0, 0))[1]
nasdaq_chg = macro_data.get("NASDAQ", (0, 0))[1]
yield_chg = macro_data.get("US 10Y Yield", (0, 0))[1]

col_left, col_right = st.columns(2)

with col_left:
    st.markdown("### 🟢 HIGH DEMAND SECTORS & STOCKS")
    
    # Logic for Crude Drop -> Positive for Auto, Paints, Aviation
    if crude_chg < 0:
        st.markdown(f"""
        <div class="demand-box">
            <b>🟢 Auto, Paints & Aviation Sector</b><br>
            <i>कारण:</i> क्रूड ऑयल में गिरावट (-{abs(crude_chg):.2f}%) कच्चा माल सस्ता करती है।<br>
            <b>प्रभावित स्टॉक्स:</b> {", ".join(SECTOR_MAP["Auto"][:5])}, ASIANPAINT, INDIGO
        </div>
        """, unsafe_allow_html=True)
        
    # Logic for NASDAQ Gain -> Positive for IT
    if nasdaq_chg > 0:
        st.markdown(f"""
        <div class="demand-box">
            <b>🟢 IT & Tech Sector</b><br>
            <i>कारण:</i> अमेरिकी टेक इंडेक्स NASDAQ में तेज़ी (+{nasdaq_chg:.2f}%) भारतीय IT स्टॉक्स के लिए पॉजिटिव संकेत है।<br>
            <b>प्रभावित स्टॉक्स:</b> {", ".join(SECTOR_MAP["IT"])}
        </div>
        """, unsafe_allow_html=True)

with col_right:
    st.markdown("### 🔴 HIGH SUPPLY SECTORS & STOCKS")
    
    # Logic for US Yield / DXY Rise -> Supply in Banking & Emerging Markets
    if yield_chg > 0:
        st.markdown(f"""
        <div class="supply-box">
            <b>🔴 Banking & Financials (FII Selling Pressure)</b><br>
            <i>कारण:</i> US 10Y Bond Yield में उछाल (+{yield_chg:.2f}%) से FII बिकवाली का दबाव बनता है।<br>
            <b>प्रभावित स्टॉक्स:</b> {", ".join(SECTOR_MAP["Banking_NBFC"][:5])}
        </div>
        """, unsafe_allow_html=True)
        
    if crude_chg > 1.5:
        st.markdown(f"""
        <div class="supply-box">
            <b>🔴 Paint & Tile Manufacturers</b><br>
            <i>कारण:</i> कच्चे तेल में अचानक उछाल (+{crude_chg:.2f}%) मार्जिन पर दबाव डालेगा।<br>
            <b>प्रभावित स्टॉक्स:</b> ASIANPAINT, PIDILITIND
        </div>
        """, unsafe_allow_html=True)

st.markdown("---")

# --- SECTION 3: WATCHLIST STOCK NEWS & EVENT TRACKER ---
st.subheader("📰 3. Watchlist News & Breaking Events Tracker")

selected_stock = st.selectbox("अपनी वॉचलिस्ट से स्टॉक चुनें (100+ Stocks):", STOCKS_LIST)

def get_stock_news(symbol):
    rss_url = f"https://news.google.com/rss/search?q={symbol}+share+news+India&hl=en-IN&gl=IN&ceid=IN:en"
    feed = feedparser.parse(rss_url)
    articles = []
    for entry in feed.entries[:5]:
        articles.append({"title": entry.title, "link": entry.link, "published": entry.published[:16]})
    return articles

if selected_stock:
    news_items = get_stock_news(selected_stock)
    st.write(f"**[{selected_stock}] की हालिया महत्वपूर्ण खबरें:**")
    if news_items:
        for item in news_items:
            st.markdown(f"• **[{item['title']}]({item['link']})** _({item['published']})_")
    else:
        st.info("इस शेयर के लिए कोई नई ब्रेकिंग न्यूज़ नहीं मिली।")

# --- SECTION 4: GLOBAL MACRO NEWS FEED ---
st.subheader("🌐 4. Macro Headlines (TradingEconomics RSS Feed)")
def get_macro_news():
    feed = feedparser.parse("https://tradingeconomics.com/rss/news.aspx")
    return feed.entries[:4]

macro_news = get_macro_news()
for news in macro_news:
    st.markdown(f"📌 **{news.title}**")
