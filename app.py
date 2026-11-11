
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

state_lock = threading.Lock()

state = {
    "cash": STARTING_BALANCE,
    "realized_pnl": 0.0,
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

    if len(candles) < 30:
        raise RuntimeError("Not enough completed candles yet")

    closes = [float(candle[4]) for candle in candles]
    candle_time = int(candles[-1][0])

    ema9 = calculate_ema(closes[-80:], 9)
    ema21 = calculate_ema(closes[-80:], 21)

    previous_ema9 = calculate_ema(closes[-81:-1], 9)
    previous_ema21 = calculate_ema(closes[-81:-1], 21)

    if ema9 > ema21:
        signal = "BUY"
    elif ema9 < ema21:
        signal = "SELL"
    else:
        signal = "HOLD"

    return {
        "symbol": "BTC/USDT",
        "price": price,
        "ema9": ema9,
        "ema21": ema21,
        "previous_ema9": previous_ema9,
        "previous_ema21": previous_ema21,
        "signal": signal,
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

        elif market["signal"] == "BUY":
            budget = state["cash"] * TRADE_FRACTION

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
            "cash_balance": round(cash, 2),
            "equity": round(equity, 2),
            "starting_balance": STARTING_BALANCE,
            "realized_pnl": round(realized, 2),
            "unrealized_pnl": round(unrealized, 2),
            "total_pnl": round(equity - STARTING_BALANCE, 2),
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
<title>NIGHTSHIFT Trading</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#090d16;color:#edf2ff;font-family:Arial,sans-serif}
main{max-width:1050px;margin:auto;padding:22px}
header{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
h1{letter-spacing:4px;margin:8px 0}
.muted{color:#91a0b9;font-size:13px}
.pill{border:1px solid #355b50;color:#7de0b0;padding:8px 12px;border-radius:20px;font-size:12px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:13px;margin-top:22px}
.card{background:#111a2a;border:1px solid #243149;border-radius:14px;padding:18px;min-width:0}
.label{color:#91a0b9;font-size:12px;text-transform:uppercase;letter-spacing:1px}
.value{font-size:25px;font-weight:bold;margin-top:12px;overflow-wrap:anywhere}
.green{color:#66dfa5}.red{color:#ff7c87}.yellow{color:#f5ca70}
section{margin-top:18px}
h2{font-size:16px;margin:0 0 14px}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{text-align:left;padding:11px 7px;border-bottom:1px solid #263249}
th{color:#91a0b9;font-weight:normal}
.notice{margin-top:18px;padding:14px;border-radius:10px;background:#241f13;color:#e7c77b;font-size:13px;line-height:1.5}
.error{color:#ff9aa2}
@media(max-width:500px){main{padding:15px}.value{font-size:22px}}
</style>
</head>
<body>
<main>
<header>
  <div>
    <h1>NIGHTSHIFT</h1>
    <div class="muted">BTC/USDT MARKET SYSTEM</div>
  </div>
  <div class="pill">PAPER MODE · NO REAL ORDERS</div>
</header>

<div class="grid">
  <div class="card"><div class="label">BTC/USDT price</div><div class="value" id="price">Loading...</div></div>
  <div class="card"><div class="label">Market signal</div><div class="value yellow" id="signal">WAITING</div></div>
  <div class="card"><div class="label">Cash balance</div><div class="value" id="cash">$10,000.00</div></div>
  <div class="card"><div class="label">Account equity</div><div class="value" id="equity">$10,000.00</div></div>
  <div class="card"><div class="label">Realized P&L</div><div class="value" id="realized">$0.00</div></div>
  <div class="card"><div class="label">Unrealized P&L</div><div class="value" id="unrealized">$0.00</div></div>
</div>

<section class="card">
  <h2>Market indicators</h2>
  <div class="grid" style="margin-top:0">
    <div><div class="label">EMA 9</div><div class="value" id="ema9">--</div></div>
    <div><div class="label">EMA 21</div><div class="value" id="ema21">--</div></div>
    <div><div class="label">Total P&L</div><div class="value" id="total">$0.00</div></div>
  </div>
</section>

<section class="card">
  <h2>Open paper position</h2>
  <div id="position" class="muted">No open position</div>
</section>

<section class="card">
  <h2>Recent activity</h2>
  <div style="overflow-x:auto">
  <table>
    <thead><tr><th>Action</th><th>Price</th><th>Quantity</th><th>P&L</th></tr></thead>
    <tbody id="trades"><tr><td colspan="4">No trades yet</td></tr></tbody>
  </table>
  </div>
</section>

<div class="notice">
  Educational paper-trading test only. Trades are simulated, not sent to an exchange.
  EMA signals are not a guarantee of profit. This strategy has not been proven profitable.
  Market data is supplied by Kraken's public API.
</div>
<div class="muted" style="margin-top:15px" id="updated">Connecting to market...</div>
<div class="muted error" style="margin-top:8px" id="error"></div>
</main>

<script>
const money = n => n == null ? "--" :
  new Intl.NumberFormat("en-US",{style:"currency",currency:"USD"}).format(n);
const number = n => n == null ? "--" :
  Number(n).toLocaleString("en-US",{maximumFractionDigits:2});

function setValue(id, value, pnl=false){
  const el=document.getElementById(id);
  el.textContent=value;
  if(pnl){
    el.className="value "+(Number(value.replace(/[^0-9.-]/g,""))>=0?"green":"red");
  }
}

async function refresh(){
  try{
    const response=await fetch("/api/status",{cache:"no-store"});
    const data=await response.json();

    if(data.price != null) setValue("price",money(data.price));
    document.getElementById("signal").textContent=data.signal || "WAITING";
    document.getElementById("signal").className="value "+
      (data.signal==="BUY"?"green":data.signal==="SELL"?"red":"yellow");

    setValue("cash",money(data.cash_balance));
    setValue("equity",money(data.equity));
    setValue("realized",money(data.realized_pnl),true);
    setValue("unrealized",money(data.unrealized_pnl),true);
    setValue("total",money(data.total_pnl),true);
    document.getElementById("ema9").textContent=number(data.ema9);
    document.getElementById("ema21").textContent=number(data.ema21);

    const p=data.position;
    document.getElementById("position").innerHTML=p
      ? "Quantity: <b>"+p.quantity+"</b><br>Entry price: <b>"+money(p.entry_price)+
        "</b><br>Market value: <b>"+money(p.market_value)+"</b>"
      : "No open position";

    const rows=data.trades || [];
    document.getElementById("trades").innerHTML=rows.length
      ? rows.map(t=>"<tr><td>"+t.action+"</td><td>"+money(t.price)+
        "</td><td>"+t.quantity+"</td><td>"+
        (t.pnl==null?"--":money(t.pnl))+"</td></tr>").join("")
      : '<tr><td colspan="4">No trades yet</td></tr>';

    document.getElementById("updated").textContent=
      "Last update: "+(data.last_update || "Waiting")+" · Refreshes every 30 seconds";
    document.getElementById("error").textContent=
      data.market_error ? "Market data error: "+data.market_error : "";
  }catch(error){
    document.getElementById("error").textContent="Connection error: "+error.message;
  }
}
refresh();
setInterval(refresh,30000);
</script>

<section id="deriv-panel" style="background:#111827;color:#fff;padding:18px;margin:20px;border:1px solid #374151;border-radius:12px;">
  <h2>NIGHTSHIFT — DERIV MARKETS</h2>
  <p id="deriv-status">Connecting to Deriv...</p>
  <p>Available instruments: <strong id="deriv-count">--</strong></p>
  <pre id="deriv-data" style="white-space:pre-wrap;overflow-wrap:anywhere;color:#9ca3af;">Loading market data...</pre>
  <button onclick="loadDerivMarkets()" style="padding:10px 16px;border-radius:8px;cursor:pointer;">Refresh Markets</button>
</section>

<script>
async function loadDerivMarkets() {
  const status = document.getElementById("deriv-status");
  const count = document.getElementById("deriv-count");
  const output = document.getElementById("deriv-data");

  status.textContent = "Connecting...";

  try {
    const response = await fetch("/api/deriv/markets");

    if (!response.ok) {
      throw new Error("HTTP " + response.status);
    }

    const data = await response.json();

    status.textContent = data.ok
      ? "Deriv feed connected"
      : "Feed reported an error";

    count.textContent = data.count ?? "--";

    output.textContent = JSON.stringify(
      data.markets ?? data,
      null,
      2
    );

  } catch (error) {
    status.textContent = "Connection failed: " + error.message;
    count.textContent = "--";
    output.textContent = "Unable to load market data.";
  }
}

loadDerivMarkets();
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
            "subscribe": 0,
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


