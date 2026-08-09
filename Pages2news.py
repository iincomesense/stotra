"""
Global Leaders & Market-Moving News (AI Digest)
==================================================
Financial market ko prabhavit karne wali important news — Bharat,
top countries ke chiefs/leaders, aur badi companies ke CEO/leaders
se judi sirf pichhle 24 ghante ki taaza khabaren, high-rated sources
se. Agar ANTHROPIC_API_KEY diya ho to ek AI-generated market-impact
hypothesis bhi dikhta hai.

Yeh file `pages/` folder mein hai isliye Streamlit ise apne aap
sidebar mein ek naye page ke roop mein dikhayega.
"""

import os
import urllib.parse
from datetime import datetime, timedelta, timezone

import requests
import streamlit as st
import yfinance as yf

try:
    import feedparser
except ImportError:
    feedparser = None

IST = timezone(timedelta(hours=5, minutes=30))
NEWS_MAX_AGE_HOURS = 24

# Currency, Bond, Commodity, Global Index snapshot (AI hypothesis ke liye base data)
MACRO_INSTRUMENTS = [
    ("USD Index (DXY)", "DX-Y.NYB"), ("USD/INR", "INR=X"), ("US 10Y Yield", "^TNX"),
    ("Gold", "GC=F"), ("Silver", "SI=F"), ("Crude Oil (WTI)", "CL=F"),
    ("Copper", "HG=F"), ("Natural Gas", "NG=F"),
    ("Dow Jones", "^DJI"), ("S&P 500", "^GSPC"), ("Nikkei 225", "^N225"),
    ("FTSE 100", "^FTSE"), ("Shanghai Composite", "000001.SS"), ("Nifty 50", "^NSEI"),
]

# Top-cap Indian stocks (AI hypothesis ke stock-level analysis ke liye)
TOP_STOCKS = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "SBIN", "BHARTIARTL", "ITC",
    "LT", "KOTAKBANK", "AXISBANK", "HINDUNILVR", "BAJFINANCE", "MARUTI", "SUNPHARMA",
    "TATAMOTORS", "TATASTEEL", "ONGC", "NTPC", "POWERGRID", "ADANIENT", "ADANIPORTS",
    "HCLTECH", "WIPRO", "ASIANPAINT",
]


@st.cache_data(ttl=600, show_spinner=False)
def fetch_pct_changes(tickers_tuple):
    tickers = list(tickers_tuple)
    try:
        data = yf.download(tickers, period="5d", interval="1d",
                            group_by="ticker", progress=False, threads=True)
    except Exception:
        return {}
    out = {}
    for t in tickers:
        try:
            df = data[t].dropna() if len(tickers) > 1 else data.dropna()
            if len(df) >= 2:
                last, prev = df["Close"].iloc[-1], df["Close"].iloc[-2]
                out[t] = (last - prev) / prev * 100
        except Exception:
            continue
    return out


st.set_page_config(page_title="Global Leaders & Market News", page_icon="🌐", layout="wide")
st.title("🌐 Global Leaders & Market-Moving News")
st.caption("भारत + टॉप देशों के Chiefs/Leaders और बड़ी Companies के CEOs से जुड़ी सिर्फ़ पिछले 24 घंटे "
           "की ख़बरें — high-rated sources से, live। 24 घंटे से पुरानी कोई भी खबर यहाँ नहीं दिखेगी।")

CATEGORIES = {
    "🇮🇳 भारत — सरकार / RBI / Finance Ministry":
        "India PM OR Finance Minister OR RBI Governor economy market policy",
    "🇺🇸 US — President / Fed / Treasury":
        "US President OR Federal Reserve Chair OR Treasury Secretary economy market policy",
    "🇨🇳 China — Leadership / PBOC":
        "China President OR PBOC economy market policy",
    "🌍 अन्य बड़े देश (UK / EU / Japan)":
        "UK Prime Minister OR ECB President OR Bank of Japan economy market policy",
    "🏢 बड़ी Companies के CEO / Leaders":
        "CEO statement OR announcement stock market impact",
}

HIGH_RATED_SOURCES = [
    "reuters.com", "bloomberg.com", "ft.com", "wsj.com", "cnbc.com",
    "economictimes.indiatimes.com", "business-standard.com", "livemint.com",
]


@st.cache_data(ttl=900, show_spinner=False)
def fetch_category_news(query):
    """Pehle high-rated sources try karta hai; kam mile to broader Google News,
    par har haal mein 24-ghante ka cutoff sakht laagu hota hai."""
    if feedparser is None:
        return []
    site_filter = " OR ".join(f"site:{d}" for d in HIGH_RATED_SOURCES)
    queries_to_try = [f"({query}) when:1d ({site_filter})", f"{query} when:1d"]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=NEWS_MAX_AGE_HOURS)
    best_items = []
    for q in queries_to_try:
        url = f"https://news.google.com/rss/search?q={urllib.parse.quote_plus(q)}&hl=en-IN&gl=IN&ceid=IN:en"
        try:
            resp = requests.get(url, timeout=15)
            feed = feedparser.parse(resp.content)
        except Exception:
            continue
        items = []
        for e in feed.entries[:15]:
            pub = e.get("published_parsed")
            if not pub:
                continue
            pub_dt = datetime(*pub[:6], tzinfo=timezone.utc)
            if pub_dt < cutoff:          # 24h se purani khabar -> hard skip
                continue
            items.append({"title": e.title, "link": e.link, "published": pub_dt})
            if len(items) >= 6:
                break
        if len(items) > len(best_items):
            best_items = items
        if len(best_items) >= 4:
            break
    return best_items


if feedparser is None:
    st.error("`feedparser` install नहीं है — requirements.txt चेक करें।")
    st.stop()

with st.spinner("सभी categories से ताज़ा (24h) headlines लाई जा रही हैं..."):
    category_items = {label: fetch_category_news(q) for label, q in CATEGORIES.items()}

# ============================== MACRO SNAPSHOT (for AI grounding) ==============================
macro_pct = fetch_pct_changes(tuple(t for _, t in MACRO_INSTRUMENTS))
stock_pct = fetch_pct_changes(tuple(f"{s}.NS" for s in TOP_STOCKS))

st.markdown("---")
st.subheader("📊 Currency / Bond / Commodity / Global Index — आज का Snapshot")
cols = st.columns(4)
for i, (name, ticker) in enumerate(MACRO_INSTRUMENTS):
    pct = macro_pct.get(ticker)
    with cols[i % 4]:
        st.metric(name, f"{pct:+.2f}%" if pct is not None else "—")

# ============================== AI HYPOTHESIS (optional) ==============================
st.markdown("---")
st.subheader("🤖 AI Hypothesis — Sector-wise / Stock-wise / Country-wise (हिंदी में)")

api_key = os.environ.get("ANTHROPIC_API_KEY")
all_headlines = [f"- [{label}] {it['title']}" for label, items in category_items.items() for it in items]

macro_summary = "\n".join(
    f"- {name}: {macro_pct[t]:+.2f}%" for name, t in MACRO_INSTRUMENTS if t in macro_pct
)
stock_summary = "\n".join(
    f"- {s}: {stock_pct[f'{s}.NS']:+.2f}%" for s in TOP_STOCKS if f"{s}.NS" in stock_pct
)

if not api_key:
    st.info(
        "AI-generated hypothesis के लिए Streamlit **App Settings → Secrets** में यह जोड़ें "
        "(यह key सिर्फ़ आप ही अपने Anthropic अकाउंट से बना सकते हैं — मैं इसे आपकी तरफ़ से बना/जोड़ नहीं सकता):\n\n"
        '```\nANTHROPIC_API_KEY = "sk-ant-आपकी-key"\n```\n\n'
        "जोड़ने के बाद App अपने-आप restart होकर यहाँ Sector/Stock/Country-wise हिंदी हाइपोथेसिस दिखाना शुरू कर देगी। "
        "अभी नीचे सिर्फ़ raw डेटा और headlines दिख रही हैं।"
    )
elif not all_headlines and not macro_summary:
    st.info("पर्याप्त डेटा नहीं मिला, इसलिए hypothesis नहीं बन सकता।")
else:
    with st.spinner("AI मैक्रो डेटा + headlines को analyse कर रहा है..."):
        try:
            prompt = f"""तुम एक वरिष्ठ (senior) news-cum-financial-markets एक्सपर्ट हो। नीचे दिया गया डेटा
सिर्फ़ पिछले 24 घंटे का है:

## 1) Currency / Bond / Commodity / Global Index (आज का % बदलाव)
{macro_summary if macro_summary else "डेटा उपलब्ध नहीं"}

## 2) टॉप भारतीय स्टॉक्स (आज का % बदलाव)
{stock_summary if stock_summary else "डेटा उपलब्ध नहीं"}

## 3) सरकार/नेता/नीति/बड़ी कंपनियों के CEO से जुड़ी पिछले 24 घंटे की headlines
{chr(10).join(all_headlines[:40]) if all_headlines else "कोई प्रमुख headline नहीं"}

इस पूरे डेटा को आपस में जोड़कर हिंदी में एक structured hypothesis दो, बिल्कुल इन 3 हेडिंग के तहत:

### 🏭 Sector-based Hypothesis
कौन से sectors पर positive/negative असर पड़ सकता है — currency, bond yield, commodity movements
और policy news को आपस में जोड़कर तर्क (reasoning) सहित बताओ।

### 📈 Stock-based Hypothesis
ऊपर दी गई स्टॉक लिस्ट में से किन specific स्टॉक्स पर सबसे ज़्यादा असर पड़ सकता है — नाम लेकर, कारण सहित।

### 🌍 Country / Macro-based Hypothesis
भारत (Nifty/Sensex) और बड़े वैश्विक बाज़ारों की संभावित दिशा — सरकारी नीति और global macro movements
के आधार पर।

हर सेक्शन छोटा और स्पष्ट रखो। अंत में साफ़ लिखो कि यह एक data-based hypothesis है, गारंटीड
भविष्यवाणी या वित्तीय सलाह नहीं है।"""

            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={"model": "claude-sonnet-5", "max_tokens": 1200,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=45,
            )
            data = resp.json()
            if "content" in data:
                text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
                st.success(text)
            else:
                err = data.get("error", {}).get("message", "unknown error")
                st.warning(f"AI से response नहीं मिला: {err}")
        except Exception as e:
            st.warning(f"AI call fail हुआ: {e}")

st.caption("⚠️ यह AI-generated hypothesis है — guaranteed prediction या वित्तीय सलाह नहीं। हमेशा अपनी खुद की research करें।")

# ============================== RAW HEADLINES BY CATEGORY ==============================
st.markdown("---")
st.subheader("📰 सभी Headlines — Category के हिसाब से (पिछले 24 घंटे)")

for label, items in category_items.items():
    st.markdown(f"### {label}")
    if not items:
        st.caption("पिछले 24 घंटे में इस category में कोई खबर नहीं मिली।")
    else:
        for it in items:
            t = it["published"].astimezone(IST).strftime("%d-%b %H:%M")
            st.markdown(f"- [{it['title']}]({it['link']})  \n  _{t} IST_")
    st.markdown("")
