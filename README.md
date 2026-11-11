
# NIGHTSHIFT

Mobile cloud trading dashboard.

## Features
- FastAPI backend
- Mobile dashboard
- Account status
- Paper trading mode
- Health monitoring

## Deployment

Platform: Render

Runtime: Python

Build command:
pip install -r requirements.txt

Start command:
uvicorn app:app --host 0.0.0.0 --port $PORT

Health check:
/healthz

## Safety

Paper trading only.
No live exchange connection.
No real-money orders.
