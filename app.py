
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="NIGHTSHIFT")

# ---------------------------------------------------------
# NIGHTSHIFT: BTC/USDT live market data + PAPER trading only
# No exchange account, API keys, or real-money orders.
# ---------------------------------------------------------

STARTING_BALANCE = 10_000.0
TRADE_FRACTION = 0.10
FEE_RATE = 0.001       # Simulated 0.1% fee per side
STOP_LOSS = 0.02       # 2%
TAKE_PROFIT = 0.03     # 3%
RISK_PER_TRADE = 0.005       # Risk 0.5% of equity per trade
MAX_POSITION_FRACTION = 0.25 # Maximum 25% of equity in one position
DAILY_LOSS_LIMIT = 0.02      # Stop new entries after 2% daily loss
MAX_DRAWDOWN = 0.08          # Stop new entries after 8% drawdown

state_lock = threading.Lock()

state = {
    "cash": STARTING_BALANCE,
    "realized_pnl": 0.0,
    "day_start_equity": STARTING_BALANCE,
    "day_key": datetime.now(timezone.utc).date().isoformat(),
    "peak_equity": STARTING_BALANCE,
    "risk_halted": False,
    "position": None,
    "last_action": "Waiting for market data",
    "last_update": None,
    "last_processed_candle": None,
    "trades": [],
}


def fetch_json(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "NIGHTSHIFT/1.0"}
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def get_market_data():
    ticker_url = (
        "https://api.kraken.com/0/public/Ticker?"
        + urllib.parse.urlencode({"pair": "XBTUSDT"})
    )
    ohlc_url = (
        "https://api.kraken.com/0/public/OHLC?"
        + urllib.parse.urlencode({"pair": "XBTUSDT", "interval": 5})
    )

    ticker_data = fetch_json(ticker_url)
    ohlc_data = fetch_json(ohlc_url)

    if ticker_data.get("error"):
        raise RuntimeError(", ".join(ticker_data["error"]))
    if ohlc_data.get("error"):
        raise RuntimeError(", ".join(ohlc_data["error"]))

    ticker_result = ticker_data["result"]
    ticker_key = next(iter(ticker_result))
    price = float(ticker_result[ticker_key]["c"][0])

    ohlc_result = ohlc_data["result"]
    candle_key = next(key for key in ohlc_result if key != "last")
    raw_candles = ohlc_result[candle_key]

    # Kraken includes the currently forming candle as the final row.
    # Use completed candles only for the EMA signal.
    candles = raw_candles[:-1]

    if len(candles) < 200:
        raise RuntimeError("Waiting for at least 200 completed candles")

    closes = [float(candle[4]) for candle in candles]
    candle_time = int(candles[-1][0])

    ema9 = calculate_ema(closes[-80:], 9)
    ema21 = calculate_ema(closes[-80:], 21)

    previous_ema9 = calculate_ema(closes[-81:-1], 9)
    previous_ema21 = calculate_ema(closes[-81:-1], 21)

    adaptive = adaptive_strategy({
        "price": price,
        "ema9": ema9,
        "ema21": ema21,
        "closes": closes,
        "raw_candles": candles,
    })

    return {
        "symbol": "BTC/USDT",
        "price": price,
        "ema9": ema9,
        "ema21": ema21,
        "previous_ema9": previous_ema9,
        "previous_ema21": previous_ema21,
        "signal": adaptive["signal"],
        "regime": adaptive["regime"],
        "confidence": adaptive["confidence"],
        "rsi": adaptive["rsi"],
        "adx": adaptive["adx"],
        "atr": adaptive["atr"],
        "reason": adaptive["reason"],
        "candle_time": candle_time,
        "candles": len(candles),
        "source": "Kraken public market data",
    }

def calculate_ema(values, period):
    if len(values) < period:
        raise ValueError("Not enough prices to calculate EMA")

    multiplier = 2 / (period + 1)
    ema = sum(values[:period]) / period

    for price in values[period:]:
        ema = (price * multiplier) + (ema * (1 - multiplier))

    return ema

def calculate_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0

    changes = [
        closes[i] - closes[i - 1]
        for i in range(1, len(closes))
    ]

    gains = [max(change, 0) for change in changes[-period:]]
    losses = [max(-change, 0) for change in changes[-period:]]

    average_gain = sum(gains) / period
    average_loss = sum(losses) / period

    if average_loss == 0:
        return 100.0

    relative_strength = average_gain / average_loss
    return 100 - (100 / (1 + relative_strength))


def calculate_atr(candles, period=14):
    if len(candles) < period + 1:
        return 0.0

    ranges = []

    for i in range(1, len(candles)):
        high = float(candles[i][2])
        low = float(candles[i][3])
        previous_close = float(candles[i - 1][4])

        true_range = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        )
        ranges.append(true_range)

    return sum(ranges[-period:]) / period


def calculate_adx(candles, period=14):
    if len(candles) < period * 2:
        return 0.0

    plus_dm = []
    minus_dm = []
    true_ranges = []

    for i in range(1, len(candles)):
        high = float(candles[i][2])
        low = float(candles[i][3])
        previous_high = float(candles[i - 1][2])
        previous_low = float(candles[i - 1][3])
        previous_close = float(candles[i - 1][4])

        up_move = high - previous_high
        down_move = previous_low - low

        plus_dm.append(
            up_move if up_move > down_move and up_move > 0 else 0
        )
        minus_dm.append(
            down_move if down_move > up_move and down_move > 0 else 0
        )

        true_ranges.append(max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        ))

    tr = sum(true_ranges[-period:])
    if tr == 0:
        return 0.0

    plus_di = 100 * sum(plus_dm[-period:]) / tr
    minus_di = 100 * sum(minus_dm[-period:]) / tr

    denominator = plus_di + minus_di
    if denominator == 0:
        return 0.0

    return 100 * abs(plus_di - minus_di) / denominator   
def adaptive_strategy(market):
    price = market["price"]
    closes = market["closes"]
    candles = market["raw_candles"]

    ema9 = market["ema9"]
    ema21 = market["ema21"]
    ema50 = calculate_ema(closes[-80:], 50)
    ema200 = calculate_ema(closes[-200:], 200)

    rsi = calculate_rsi(closes)
    atr = calculate_atr(candles)
    adx = calculate_adx(candles)

    atr_percent = (atr / price) * 100 if price else 0

    trend_up = ema9 > ema21 and ema21 > ema50 and ema50 > ema200
    trend_down = ema9 < ema21 and ema21 < ema50 and ema50 < ema200

    if atr_percent > 2.0:
        return {
            "signal": "HOLD",
            "regime": "HIGH VOLATILITY",
            "confidence": 0,
            "reason": "Volatility filter",
            "rsi": round(rsi, 2),
            "adx": round(adx, 2),
            "atr": round(atr, 2),
        }

    if adx >= 25:
        if trend_up and 45 <= rsi <= 68:
            signal = "BUY"
            regime = "TRENDING UP"
            confidence = 85
        elif trend_down and rsi < 55:
            signal = "SELL"
            regime = "TRENDING DOWN"
            confidence = 85
        else:
            signal = "HOLD"
            regime = "TRENDING"
            confidence = 30

    else:
        if rsi <= 30:
            signal = "BUY"
            regime = "RANGING"
            confidence = 70
        elif rsi >= 70:
            signal = "SELL"
            regime = "RANGING"
            confidence = 70
        else:
            signal = "HOLD"
            regime = "RANGING"
            confidence = 25

    return {
        "signal": signal,
        "regime": regime,
        "confidence": confidence,
        "reason": "Adaptive indicator analysis",
        "rsi": round(rsi, 2),
        "adx": round(adx, 2),
        "atr": round(atr, 2),
    }
def record_trade(action, price, quantity, pnl=None):
    entry = {
        "time": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "price": round(price, 2),
        "quantity": round(quantity, 8),
        "pnl": round(pnl, 2) if pnl is not None else None,
    }
    state["trades"].insert(0, entry)
    state["trades"] = state["trades"][:20]
    state["last_action"] = action


def process_paper_strategy(market):
    price = market["price"]
    candle_time = market["candle_time"]

    with state_lock:
        # Do not process the same completed candle more than once.
        if state["last_processed_candle"] == candle_time:
            return

        state["last_processed_candle"] = candle_time
        position = state["position"]
        equity = state["cash"]
        if position:
            equity += position["quantity"] * price

        today = datetime.now(timezone.utc).date().isoformat()

        if state["day_key"] != today:
            state["day_key"] = today
            state["day_start_equity"] = equity
            state["risk_halted"] = False

        state["peak_equity"] = max(state["peak_equity"], equity)

        daily_loss = (
            (equity - state["day_start_equity"])
            / state["day_start_equity"]
        )
        drawdown = (
            (equity - state["peak_equity"])
            / state["peak_equity"]
        )

        if daily_loss <= -DAILY_LOSS_LIMIT:
            state["risk_halted"] = True
            state["last_action"] = "RISK HALT: Daily loss limit"

        if drawdown <= -MAX_DRAWDOWN:
            state["risk_halted"] = True
            state["last_action"] = "RISK HALT: Maximum drawdown"

        if position:
            entry_price = position["entry_price"]
            stop_price = entry_price * (1 - STOP_LOSS)
            target_price = entry_price * (1 + TAKE_PROFIT)

            exit_reason = None

            if price <= stop_price:
                exit_reason = "STOP LOSS"
            elif price >= target_price:
                exit_reason = "TAKE PROFIT"
            elif market["signal"] == "SELL":
                exit_reason = "EMA SELL"

            if exit_reason:
                quantity = position["quantity"]
                proceeds = quantity * price * (1 - FEE_RATE)
                pnl = proceeds - position["total_cost"]

                state["cash"] += proceeds
                state["realized_pnl"] += pnl
                state["position"] = None

                record_trade(
                    exit_reason,
                    price,
                    quantity,
                    pnl
                )

        elif market["signal"] == "BUY" and not state["risk_halted"]:
            risk_budget = equity * RISK_PER_TRADE
            risk_based_notional = risk_budget / STOP_LOSS

            max_notional = equity * MAX_POSITION_FRACTION
            available_notional = state["cash"] / (1 + FEE_RATE)

            budget = min(
                risk_based_notional,
                max_notional,
                available_notional
            )

            if budget > 10:
                quantity = budget / (price * (1 + FEE_RATE))
                total_cost = quantity * price * (1 + FEE_RATE)

                state["cash"] -= total_cost
                state["position"] = {
                    "entry_price": price,
                    "quantity": quantity,
                    "total_cost": total_cost,
                    "opened_at": datetime.now(timezone.utc).isoformat(),
                }

                record_trade("PAPER BUY", price, quantity)

        state["last_update"] = datetime.now(timezone.utc).isoformat()


def build_status(market=None, market_error=None):
    with state_lock:
        position = state["position"]
        cash = state["cash"]
        realized = state["realized_pnl"]
        trades = list(state["trades"])

        price = market["price"] if market else None
        quantity = position["quantity"] if position else 0.0
        market_value = quantity * price if price is not None else 0.0
        equity = cash + market_value
        unrealized = (
            market_value - position["total_cost"]
            if position and price is not None
            else 0.0
        )

        return {
            "ok": market is not None,
            "mode": "PAPER TRADING",
            "symbol": "BTC/USDT",
            "price": price,
            "signal": market["signal"] if market else "WAITING",
            "ema9": market["ema9"] if market else None,
            "ema21": market["ema21"] if market else None,
            "regime": market["regime"] if market else None,
            "confidence": market["confidence"] if market else None,
            "rsi": market["rsi"] if market else None,
            "adx": market["adx"] if market else None,
            "atr": market["atr"] if market else None,
            "signal_reason": market["reason"] if market else None,
            
            "cash_balance": round(cash, 2),
            "equity": round(equity, 2),
            "starting_balance": STARTING_BALANCE,
            "realized_pnl": round(realized, 2),
            "unrealized_pnl": round(unrealized, 2),
            "total_pnl": round(equity - STARTING_BALANCE, 2),
            "daily_loss_pct": round(
            (equity / state["day_start_equity"] - 1) * 100, 2
            ),
            "drawdown_pct": round(
            (equity / state["peak_equity"] - 1) * 100, 2
            ),
            "risk_halted": state["risk_halted"],
            "daily_loss_limit_pct": DAILY_LOSS_LIMIT * 100,
            "max_drawdown_pct": MAX_DRAWDOWN * 100,
            "position": {
                "quantity": round(quantity, 8),
                "entry_price": round(position["entry_price"], 2),
                "market_value": round(market_value, 2),
            } if position else None,
            "last_action": state["last_action"],
            "last_update": state["last_update"],
            "market_source": market["source"] if market else None,
            "market_error": market_error,
            "trades": trades,
            "strategy": {
                "indicators": "EMA 9 / EMA 21",
                "timeframe": "5 minutes",
                "trade_size": "10% of available cash",
                "stop_loss": "2%",
                "take_profit": "3%",
                "simulated_fee": "0.1% per side",
            },
        }


@app.get("/healthz")
def health():
    return {"ok": True, "service": "NIGHTSHIFT"}


@app.get("/api/market")
def market_endpoint():
    try:
        return {"ok": True, **get_market_data()}
    except Exception as error:
        return {"ok": False, "error": str(error)}


@app.get("/api/status")
def status_endpoint():
    try:
        market = get_market_data()
        process_paper_strategy(market)
        return build_status(market)
    except Exception as error:
        return build_status(market_error=str(error))



DASHBOARD = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NIGHTSHIFT | Trading Terminal</title>
<style>
:root {
  --bg:#080c14; --panel:#101827; --panel2:#151f30;
  --border:#26344a; --muted:#8999b1; --white:#edf3ff;
  --green:#35e59a; --red:#ff647c; --gold:#f4c66a;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{
  margin:0;background:var(--bg);color:var(--white);
  font-family:Inter,Arial,sans-serif;
}
button{font:inherit}
.layout{max-width:1450px;margin:auto;padding:22px}
.topbar{
  display:flex;justify-content:space-between;align-items:center;
  flex-wrap:wrap;gap:18px;padding:12px 0 24px;
  border-bottom:1px solid var(--border)
}
.brand{font-size:25px;font-weight:900;letter-spacing:5px}
.brand span{color:var(--green)}
.subtitle{color:var(--muted);font-size:11px;letter-spacing:2px;margin-top:6px}
.nav{display:flex;gap:20px;flex-wrap:wrap}
.nav a{color:var(--muted);text-decoration:none;font-size:13px}
.nav a:hover{color:var(--green)}
.status-pill{
  border:1px solid #246449;color:var(--green);
  border-radius:30px;padding:9px 13px;font-size:11px;
}
.hero{
  display:flex;justify-content:space-between;align-items:center;
  gap:15px;flex-wrap:wrap;padding:30px 0 20px
}
.hero h1{font-size:30px;margin:0 0 8px}
.muted{color:var(--muted);font-size:13px}
.grid{
  display:grid;grid-template-columns:repeat(4,minmax(0,1fr));
  gap:14px;margin:16px 0
}
.card{
  background:linear-gradient(145deg,var(--panel),#0d1421);
  border:1px solid var(--border);border-radius:15px;padding:19px;
  min-width:0;
}
.card-label{
  color:var(--muted);font-size:11px;
  letter-spacing:1.4px;text-transform:uppercase
}
.big{font-size:27px;font-weight:750;margin-top:13px;overflow-wrap:anywhere}
.small{font-size:12px;color:var(--muted);margin-top:9px}
.green{color:var(--green)} .red{color:var(--red)}
.gold{color:var(--gold)}
.section-title{
  display:flex;justify-content:space-between;align-items:center;
  gap:12px;margin:28px 0 13px
}
.section-title h2{font-size:16px;margin:0}
.market-grid{
  display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px
}
.market-price{font-size:25px;font-weight:750;margin:12px 0}
.chart-card{margin-top:16px}
.chart-wrap{height:260px;width:100%;position:relative}
canvas{width:100%;height:100%;display:block}
.chart-meta{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap}
.signal-box{
  display:flex;justify-content:space-between;align-items:center;
  gap:12px;flex-wrap:wrap
}
.signal-value{font-size:27px;font-weight:900;letter-spacing:2px}
.signal-note{font-size:12px;color:var(--muted);margin-top:7px}
.badge{
  padding:8px 12px;border-radius:8px;
  background:#1b2939;font-size:11px;color:var(--gold)
}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:13px 8px;border-bottom:1px solid var(--border)}
th{font-size:10px;color:var(--muted);letter-spacing:1px}
.table-wrap{overflow-x:auto}
.footer{
  padding:25px 0;color:var(--muted);font-size:11px;
  line-height:1.7;text-align:center
}
@media(max-width:850px){
 .grid{grid-template-columns:repeat(2,minmax(0,1fr))}
 .market-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
}
@media(max-width:520px){
 .layout{padding:14px}
 .grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}
 .market-grid{grid-template-columns:1fr}
 .card{padding:14px}
 .big{font-size:21px}
 .hero h1{font-size:25px}
 .nav{gap:13px}
}
</style>
</head>
<body>
<div class="layout">

<header class="topbar">
  <div>
    <div class="brand">NIGHT<span>SHIFT</span></div>
    <div class="subtitle">INTELLIGENT MARKET TERMINAL</div>
  </div>
  <nav class="nav">
    <a href="#overview">Overview</a>
    <a href="#markets">Markets</a>
    <a href="#signals">Signals</a>
    <a href="#portfolio">Portfolio</a>
    <a href="#activity">Activity</a>
  </nav>
  <div class="status-pill" id="connection">● CONNECTING</div>
</header>

<section class="hero" id="overview">
  <div>
    <h1>Market Overview</h1>
    <div class="muted">Your markets. Your strategy. One terminal.</div>
  </div>
  <div class="badge">PAPER TRADING · NO REAL ORDERS</div>
</section>

<div class="grid" id="portfolio">
  <div class="card">
    <div class="card-label">Account equity</div>
    <div class="big" id="equity">$10,000.00</div>
    <div class="small">Total simulated account value</div>
  </div>
  <div class="card">
    <div class="card-label">Available cash</div>
    <div class="big" id="cash">$10,000.00</div>
    <div class="small">Uninvested demo balance</div>
  </div>
  <div class="card">
    <div class="card-label">Total profit / loss</div>
    <div class="big" id="total">$0.00</div>
    <div class="small">Since starting balance</div>
  </div>
  <div class="card">
    <div class="card-label">Open position</div>
    <div class="big" id="position-state">NONE</div>
    <div class="small" id="position-detail">No active paper position</div>
  </div>
</div>

<div class="section-title" id="markets">
  <h2>Live Markets</h2>
  <span class="muted">Market data feeds</span>
</div>

<div class="market-grid">
  <div class="card">
    <div class="card-label">Bitcoin · BTC/USDT</div>
    <div class="market-price" id="price">Loading...</div>
    <div class="small green" id="btc-status">Waiting for Kraken</div>
  </div>
  <div class="card">
    <div class="card-label">Deriv · Volatility 100 (1s)</div>
    <div class="market-price" id="deriv-price">Loading...</div>
    <div class="small" id="deriv-status">Connecting to Deriv</div>
  </div>
  <div class="card">
    <div class="card-label">Gold · XAU/USD</div>
    <div class="market-price gold">COMING SOON</div>
    <div class="small">Gold feed not connected yet</div>
  </div>
</div>

<div class="card chart-card">
  <div class="chart-meta">
    <div>
      <div class="card-label">Bitcoin price movement</div>
      <div class="big" id="chart-price">Waiting for data</div>
    </div>
    <div class="badge">LIVE PRICE HISTORY</div>
  </div>
  <div class="chart-wrap">
    <canvas id="price-chart"></canvas>
  </div>
  <div class="small">Chart builds from incoming price updates during this session.</div>
</div>

<div class="section-title" id="signals">
  <h2>Trading Intelligence</h2>
  <span class="muted">EMA 9 / EMA 21 · 5-minute timeframe</span>
</div>

<div class="grid">
  <div class="card">
    <div class="card-label">Current market signal</div>
    <div class="signal-value gold" id="signal">WAITING</div>
    <div class="signal-note">Based on EMA crossover direction</div>
  </div>
  <div class="card">
    <div class="card-label">EMA 9</div>
    <div class="big" id="ema9">--</div>
    <div class="small">Short-term average</div>
  </div>
  <div class="card">
    <div class="card-label">EMA 21</div>
    <div class="big" id="ema21">--</div>
    <div class="small">Longer-term average</div>
  </div>
  <div class="card">
    <div class="card-label">Realized profit / loss</div>
    <div class="big" id="realized">$0.00</div>
    <div class="small">Closed paper trades</div>
  </div>
</div>

<div class="section-title">
  <h2>Adaptive Intelligence</h2>
  <span class="muted">Multi-indicator analysis</span>
</div>

<div class="grid">
  <div class="card">
    <div class="card-label">Market regime</div>
    <div class="big" id="regime">WAITING</div>
  </div>
  <div class="card">
    <div class="card-label">Signal confidence</div>
    <div class="big" id="confidence">--</div>
  </div>
  <div class="card">
    <div class="card-label">RSI · 14</div>
    <div class="big" id="rsi">--</div>
  </div>
  <div class="card">
    <div class="card-label">ADX · 14</div>
    <div class="big" id="adx">--</div>
  </div>
  <div class="card">
    <div class="card-label">ATR · 14</div>
    <div class="big" id="atr">--</div>
  </div>
  <div class="card">
    <div class="card-label">Strategy explanation</div>
    <div class="small" id="signal-reason">Waiting for analysis</div>
  </div>
</div>
<div class="section-title">
  <h2>Risk Monitor</h2>
  <span class="muted">Capital protection</span>
</div>

<div class="grid">
  <div class="card">
    <div class="card-label">Trading status</div>
    <div class="big" id="risk-status">ACTIVE</div>
  </div>
  <div class="card">
    <div class="card-label">Daily loss</div>
    <div class="big" id="daily-loss">0.00%</div>
    <div class="small">Limit: 2%</div>
  </div>
  <div class="card">
    <div class="card-label">Maximum drawdown</div>
    <div class="big" id="drawdown">0.00%</div>
    <div class="small">Limit: 8%</div>
  </div>
</div>
<div class="section-title">
  <h2>Portfolio Performance</h2>
  <span class="muted">Simulated account</span>
</div>
<div class="grid">
  <div class="card">
    <div class="card-label">Unrealized P&L</div>
    <div class="big" id="unrealized">$0.00</div>
  </div>
  <div class="card">
    <div class="card-label">Starting balance</div>
    <div class="big">$10,000.00</div>
  </div>
  <div class="card">
    <div class="card-label">Last strategy action</div>
    <div class="big" id="last-action" style="font-size:17px">Waiting</div>
  </div>
  <div class="card">
    <div class="card-label">Data source</div>
    <div class="big" style="font-size:17px">KRAKEN</div>
    <div class="small">Public market data</div>
  </div>
</div>

<div class="section-title" id="activity">
  <h2>Recent Paper Trades</h2>
  <span class="muted">Latest activity</span>
</div>
<div class="card table-wrap">
  <table>
    <thead>
      <tr><th>ACTION</th><th>PRICE</th><th>QUANTITY</th><th>PROFIT / LOSS</th></tr>
    </thead>
    <tbody id="trades">
      <tr><td colspan="4" class="muted">No trades yet</td></tr>
    </tbody>
  </table>
</div>

<div class="footer">
  NIGHTSHIFT TRADING TERMINAL · EDUCATIONAL PAPER TRADING ONLY<br>
  Simulated trades are not sent to an exchange. Signals do not guarantee profits.
  Market data availability depends on external providers.
</div>
</div>

<script>
const money = n => n == null ? "--" :
  new Intl.NumberFormat("en-US",{
    style:"currency",currency:"USD"
  }).format(Number(n));

const fmt = n => n == null ? "--" :
  Number(n).toLocaleString("en-US",{maximumFractionDigits:2});

function put(id,value){
  document.getElementById(id).textContent=value;
}

function pnl(id,value){
  const el=document.getElementById(id);
  el.textContent=money(value);
  el.className="big "+(Number(value)>=0?"green":"red");
}

let prices=[];
let livePriceConnected=false;
let liveSocket=null;

function connectLivePrice(){
  liveSocket=new WebSocket("wss://ws.kraken.com/");

  liveSocket.onopen=()=>{
    liveSocket.send(JSON.stringify({
      event:"subscribe",
      pair:["XBT/USDT"],
      subscription:{name:"ticker"}
    }));
  };

  liveSocket.onmessage=(event)=>{
    const message=JSON.parse(event.data);

    if(!Array.isArray(message)||!message.includes("ticker"))return;

    const price=Number(message[1]?.c?.[0]);
    if(!Number.isFinite(price))return;

    livePriceConnected=true;
    put("connection","● LIVE PRICE STREAM");
    put("btc-status","● Kraken live stream");
    put("price",money(price));
    put("chart-price",money(price));

    prices.push(price);
    if(prices.length>120)prices.shift();

    drawChart();
  };

  liveSocket.onclose=()=>{
    livePriceConnected=false;
    put("connection","● RECONNECTING");
    setTimeout(connectLivePrice,3000);
  };

  liveSocket.onerror=()=>{
    liveSocket.close();
  };
}

function drawChart(){
  const canvas=document.getElementById("price-chart");
  const rect=canvas.getBoundingClientRect();
  const ratio=window.devicePixelRatio||1;

  canvas.width=rect.width*ratio;
  canvas.height=rect.height*ratio;

  const ctx=canvas.getContext("2d");
  ctx.scale(ratio,ratio);

  const w=rect.width,h=rect.height;

  ctx.clearRect(0,0,w,h);
  ctx.strokeStyle="#26344a";
  ctx.lineWidth=1;

  for(let i=1;i<4;i++){
    const y=h*i/4;
    ctx.beginPath();
    ctx.moveTo(0,y);
    ctx.lineTo(w,y);
    ctx.stroke();
  }

  if(prices.length<2)return;

  const min=Math.min(...prices);
  const max=Math.max(...prices);
  const range=max-min||1;

  ctx.beginPath();

  prices.forEach((p,i)=>{
    const x=i*w/(prices.length-1);
    const y=h-((p-min)/range)*(h-24)-12;

    if(i===0)ctx.moveTo(x,y);
    else ctx.lineTo(x,y);
  });

  ctx.strokeStyle="#35e59a";
  ctx.lineWidth=2.5;
  ctx.stroke();
}

async function refresh(){
  try{
    const response=await fetch("/api/status",{cache:"no-store"});
    if(!response.ok)throw new Error("HTTP "+response.status);
    const data=await response.json();

    const connection=document.getElementById("connection");
    if(data.ok && data.price!=null){
      connection.textContent="● MARKET CONNECTED";
      connection.style.color="#35e59a";
      if(!livePriceConnected){
  put("btc-status","● Kraken REST fallback");
  put("price",money(data.price));
  put("chart-price",money(data.price));
  prices.push(Number(data.price));
  if(prices.length>120)prices.shift();
  drawChart();
}
    }else{
      connection.textContent="● MARKET UNAVAILABLE";
      connection.style.color="#ff647c";
      put("btc-status","Waiting for market data");
    }

    put("signal",data.signal||"WAITING");
    const signal=document.getElementById("signal");
    signal.className="signal-value "+
      (data.signal==="BUY"?"green":data.signal==="SELL"?"red":"gold");

    put("ema9",fmt(data.ema9));
    put("ema21",fmt(data.ema21));
    put("regime",data.regime||"WAITING");
    put("confidence",data.confidence==null?"--":data.confidence+"%");
    put("rsi",fmt(data.rsi));
    put("adx",fmt(data.adx));
    put("atr",fmt(data.atr));
    put("signal-reason",data.signal_reason||"Waiting for analysis");
    const halted = data.risk_halted === true;
put("risk-status", halted ? "HALTED" : "ACTIVE");
put("daily-loss", Math.max(0, -Number(data.daily_loss_pct || 0)).toFixed(2) + "%");
put("drawdown", Math.max(0, -Number(data.drawdown_pct || 0)).toFixed(2) + "%");

const riskStatus = document.getElementById("risk-status");
if (riskStatus) {
  riskStatus.style.color = halted ? "#ff647c" : "#35e59a";
}
    put("cash",money(data.cash_balance));
    put("equity",money(data.equity));
    pnl("total",data.total_pnl);
    pnl("realized",data.realized_pnl);
    pnl("unrealized",data.unrealized_pnl);
    put("last-action",data.last_action||"Waiting");

    if(data.position){
      put("position-state","OPEN");
      put("position-detail",
        "Entry: "+money(data.position.entry_price)+
        " · Value: "+money(data.position.market_value));
    }else{
      put("position-state","NONE");
      put("position-detail","No active paper position");
    }

    const tbody=document.getElementById("trades");
    tbody.replaceChildren();
    (data.trades||[]).forEach(t=>{
      const tr=document.createElement("tr");
      [t.action,money(t.price),t.quantity,
       t.pnl==null?"--":money(t.pnl)].forEach(v=>{
        const td=document.createElement("td");
        td.textContent=v;
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    if(!data.trades||data.trades.length===0){
      const tr=document.createElement("tr");
      const td=document.createElement("td");
      td.colSpan=4;td.textContent="No trades yet";
      td.className="muted";tr.appendChild(td);tbody.appendChild(tr);
    }
  }catch(error){
    put("connection","● CONNECTION ERROR");
    document.getElementById("connection").style.color="#ff647c";
    put("btc-status",error.message);
  }
}

async function updateDeriv(){
  try{
    const response=await fetch(
      "/api/deriv/tick?symbol=1HZ100V",{cache:"no-store"}
    );
    const data=await response.json();
    if(!data.ok)throw new Error(data.error||"Unavailable");
    put("deriv-price",fmt(data.price));
    put("deriv-status","● Deriv feed connected");
    document.getElementById("deriv-status").className="small green";
  }catch(error){
    put("deriv-price","Unavailable");
    put("deriv-status","Deriv: "+error.message);
    document.getElementById("deriv-status").className="small red";
  }
}

connectLivePrice();
refresh();
updateDeriv();
setInterval(refresh,5000);
setInterval(updateDeriv,20000);
window.addEventListener("resize",drawChart);
</script>
</body>
</html>
"""



@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(content=DASHBOARD)

# ---------------------------------------------------------
# DERIV MARKET SCANNER — PUBLIC DATA ONLY
# No account token and no real-money trading.
# ---------------------------------------------------------

def get_deriv_markets():
    import websocket

    url = "wss://api.derivws.com/trading/v1/options/ws/public"

    connection = websocket.create_connection(url, timeout=15)

    try:
        connection.send(json.dumps({
            "active_symbols": "brief",
            "req_id": 1
        }))

        while True:
            response = json.loads(connection.recv())

            if response.get("msg_type") == "active_symbols":
                symbols = response.get("active_symbols", [])

                return [
                    {
                        "symbol": item.get("underlying_symbol"),
                        "name": item.get("underlying_symbol_name"),
                        "market": item.get("market"),
                        "submarket": item.get("submarket"),
                        "type": item.get("underlying_symbol_type"),
                    }
                    for item in symbols
                ]

            if response.get("error"):
                raise RuntimeError(response["error"].get("message", "Deriv API error"))

    finally:
        connection.close()


@app.get("/api/deriv/markets")
def deriv_markets_endpoint():
    try:
        markets = get_deriv_markets()
        return {
            "ok": True,
            "source": "Deriv public API",
            "count": len(markets),
            "markets": markets,
        }
    except Exception as error:
        return {
            "ok": False,
            "error": str(error),
            "markets": [],
        }
        
@app.get("/api/deriv/tick")
def deriv_live_tick(symbol: str = "1HZ100V"):
    import websocket

    allowed_symbols = {"1HZ100V", "R_75", "BOOM1000"}

    if symbol not in allowed_symbols:
        return {"ok": False, "error": "Unsupported symbol"}

    connection = None

    try:
        connection = websocket.create_connection(
            "wss://api.derivws.com/trading/v1/options/ws/public",
            timeout=15
        )

        connection.send(json.dumps({
            "ticks": symbol,
            "req_id": 2
        }))

        while True:
            response = json.loads(connection.recv())

            if response.get("error"):
                return {
                    "ok": False,
                    "error": response["error"].get("message")
                }

            if response.get("msg_type") == "tick":
                tick = response["tick"]
                return {
                    "ok": True,
                    "symbol": symbol,
                    "price": tick.get("quote"),
                    "epoch": tick.get("epoch"),
                    "source": "Deriv"
                }

    except Exception as error:
        return {"ok": False, "error": str(error)}

    finally:
        if connection:
            connection.close()


