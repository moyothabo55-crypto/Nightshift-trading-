
services:
  - type: web
    name: nightshift
    runtime: python
    plan: free
    buildCommand: pip install -r requirements.txt
    startCommand: uvicorn app:app --host 0.0.0.0 --port $PORT
    healthCheckPath: /healthz

fastapi>=0.115,<1.0
uvicorn[standard]>=0.30,<1.0

from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

app = FastAPI(title="NIGHTSHIFT")

state = {
    "mode": "paper",
    "balance": 10000.00,
    "equity": 10000.00,
    "positions": [],
    "events": [
        "NIGHTSHIFT initialized in paper-trading mode."
    ]
}

PAGE = """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NIGHTSHIFT</title>
<style>
body {
  background:#090d12;
  color:#eef3f8;
  font-family:Arial,sans-serif;
  padding:20px;
  max-width:700px;
  margin:auto;
}
.brand {color:#65e0b1;letter-spacing:4px}
.card {
  background:#121a23;
  border:1px solid #263341;
  border-radius:15px;
  padding:18px;
  margin:12px 0;
}
.grid {
  display:grid;
  grid-template-columns:1fr 1fr;
  gap:10px;
}
.value {font-size:25px;font-weight:bold}
.muted {color:#93a1b0}
button {
  background:#65e0b1;
  border:0;
  padding:14px;
  border-radius:10px;
  width:100%;
  font-weight:bold;
}
</style>
</head>
<body>

<div class="brand">NIGHTSHIFT / TRADING SYSTEM</div>
<h1>Command Center</h1>
<p class="muted">Mobile cloud dashboard</p>

<div class="card">
PAPER MODE — NO LIVE ORDERS
</div>

<div class="grid">
<div class="card">
<p class="muted">ACCOUNT BALANCE</p>
<div class="value" id="balance">$10,000</div>
</div>

<div class="card">
<p class="muted">EQUITY</p>
<div class="value" id="equity">$10,000</div>
</div>

<div class="card">
<p class="muted">OPEN POSITIONS</p>
<div class="value" id="positions">0</div>
</div>

<div class="card">
<p class="muted">SYSTEM STATUS</p>
<div class="value" id="status">Ready</div>
</div>
</div>

<div class="card">
<h3>Trading controls</h3>
<p class="muted">
Simulation mode only. No real-money trading.
</p>
<button onclick="refresh()">REFRESH SYSTEM</button>
</div>

<div class="card">
<h3>Activity</h3>
<div id="events">Loading...</div>
</div>

<script>
async function refresh() {
 try {
  const response = await fetch('/api/status');
  const data = await response.json();

  document.getElementById('balance').textContent =
   '$' + data.balance.toLocaleString();

  document.getElementById('equity').textContent =
   '$' + data.equity.toLocaleString();

  document.getElementById('positions').textContent =
   data.positions.length;

  document.getElementById('status').textContent = 'Online';

  document.getElementById('events').innerHTML =
   data.events.map(x => '<p>' + x + '</p>').join('');

 } catch(error) {
  document.getElementById('status').textContent = 'Offline';
 }
}

refresh();
</script>

</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
def dashboard():
    return PAGE

@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "service": "NIGHTSHIFT",
        "mode": "paper"
    }

@app.get("/api/status")
def status():
    return {
        **state,
        "updated_at": datetime.now(timezone.utc).isoformat()

