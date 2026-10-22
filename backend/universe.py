"""Instrument universe. Edit freely; providers resolve instrument keys from these symbols."""

INDICES = {
    "NIFTY": {"name": "NIFTY 50", "aliases": ["nifty 50"], "yahoo": "^NSEI"},
    "BANKNIFTY": {"name": "BANK NIFTY", "aliases": ["nifty bank"], "yahoo": "^NSEBANK"},
    "SENSEX": {"name": "SENSEX", "aliases": ["sensex", "s&p bse sensex"], "yahoo": "^BSESN"},
    "FINNIFTY": {"name": "FINNIFTY", "aliases": ["nifty fin service", "nifty financial services"],
                 "yahoo": "NIFTY_FIN_SERVICE.NS"},
    "MIDCPNIFTY": {"name": "MIDCAP SELECT", "aliases": ["nifty midcap select", "nifty mid select"], "yahoo": None},
    "INDIAVIX": {"name": "INDIA VIX", "aliases": ["india vix"], "yahoo": "^INDIAVIX"},
}

SECTORS = {
    "Banks": ["HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK"],
    "IT": ["TCS", "INFY", "HCLTECH", "WIPRO", "TECHM"],
    "Energy & Power": ["RELIANCE", "ONGC", "NTPC", "POWERGRID"],
    "Auto": ["MARUTI", "M&M", "BAJAJ-AUTO", "EICHERMOT"],
    "FMCG": ["HINDUNILVR", "ITC", "NESTLEIND"],
    "Pharma": ["SUNPHARMA", "DRREDDY", "CIPLA"],
    "Metals": ["TATASTEEL", "JSWSTEEL", "HINDALCO"],
    "Financials": ["BAJFINANCE", "BAJAJFINSV"],
    "Infra & Telecom": ["LT", "BHARTIARTL"],
}

STOCK_SECTOR = {s: sec for sec, syms in SECTORS.items() for s in syms}
STOCKS = list(STOCK_SECTOR)
INDEX_UNDERLYINGS = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"]
OPTION_UNDERLYINGS = INDEX_UNDERLYINGS + STOCKS

GLOBAL_YAHOO = [
    ("S&P 500", "^GSPC"), ("Nasdaq", "^IXIC"), ("Dow Jones", "^DJI"), ("Nikkei 225", "^N225"),
    ("Hang Seng", "^HSI"), ("US VIX", "^VIX"), ("USD/INR", "INR=X"), ("Brent Crude", "BZ=F"), ("Gold", "GC=F"),
]
