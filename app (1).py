"""
Upstox Options Dashboard Backend
----------------------------------
This Flask server acts as a bridge between your dashboard (frontend)
and Upstox's API. It solves the CORS problem by making API calls
server-side, then returning clean JSON to your dashboard.

Endpoints:
  GET  /                          -> health check
  POST /api/set-token             -> store your daily access token
  GET  /api/live-data             -> fetch live Nifty + option chain + computed signal
"""

import os
import requests
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # allow your dashboard (any origin) to call this backend

# In-memory token storage (resets if server restarts — fine for daily tokens)
STATE = {"access_token": None}

UPSTOX_BASE = "https://api.upstox.com/v2"
NIFTY_KEY = "NSE_INDEX|Nifty 50"


def get_nearest_tuesday():
    # NSE moved Nifty weekly expiry from Thursday to Tuesday effective Sept 2, 2025.
    today = datetime.now()
    days_ahead = (1 - today.weekday()) % 7  # Tuesday = weekday 1
    if days_ahead == 0 and today.hour >= 15 and today.minute >= 30:
        days_ahead = 7
    return today + timedelta(days=days_ahead)


@app.route("/")
def health():
    return jsonify({"status": "ok", "message": "Upstox backend is running"})


@app.route("/api/set-token", methods=["POST"])
def set_token():
    data = request.get_json(force=True)
    token = data.get("access_token", "").strip()
    if not token:
        return jsonify({"error": "No token provided"}), 400
    STATE["access_token"] = token
    return jsonify({"status": "ok", "message": "Token saved for this session"})


def upstox_headers():
    return {
        "Authorization": f"Bearer {STATE['access_token']}",
        "Accept": "application/json",
    }


@app.route("/api/live-data")
def live_data():
    if not STATE["access_token"]:
        return jsonify({"error": "No access token set. Call /api/set-token first."}), 401

    try:
        # 1. Fetch Nifty price
        quote_url = f"{UPSTOX_BASE}/market-quote/quotes"
        quote_res = requests.get(
            quote_url, headers=upstox_headers(), params={"instrument_key": NIFTY_KEY}, timeout=10
        )
        quote_data = quote_res.json()
        if quote_res.status_code != 200:
            return jsonify({"error": "Upstox quote error", "detail": quote_data}), quote_res.status_code

        quote_obj = list(quote_data.get("data", {}).values())
        if not quote_obj:
            return jsonify({"error": "No quote data returned"}), 500
        quote_obj = quote_obj[0]
        price = quote_obj.get("last_price", 0)
        prev_close = quote_obj.get("ohlc", {}).get("close", price)
        change_pct = round(((price - prev_close) / prev_close) * 100, 2) if prev_close else 0

        # 2. Fetch option chain for nearest weekly expiry
        expiry_dt = get_nearest_tuesday()
        expiry = expiry_dt.strftime("%Y-%m-%d")
        chain_url = f"{UPSTOX_BASE}/option/chain"
        chain_res = requests.get(
            chain_url,
            headers=upstox_headers(),
            params={"instrument_key": NIFTY_KEY, "expiry_date": expiry},
            timeout=10,
        )
        chain_data = chain_res.json()
        if chain_res.status_code != 200:
            return jsonify({"error": "Upstox option chain error", "detail": chain_data}), chain_res.status_code

        chain = chain_data.get("data", [])

        # Fallback: if empty (holiday shift, or today's expiry already settled), try next Tuesday
        if not chain:
            expiry_dt = expiry_dt + timedelta(days=7)
            expiry = expiry_dt.strftime("%Y-%m-%d")
            chain_res = requests.get(
                chain_url,
                headers=upstox_headers(),
                params={"instrument_key": NIFTY_KEY, "expiry_date": expiry},
                timeout=10,
            )
            chain_data = chain_res.json()
            chain = chain_data.get("data", [])

        if not chain:
            return jsonify({
                "error": "Empty option chain",
                "detail": f"No data for {expiry}. Market may be closed or instrument key incorrect.",
            }), 500

        # 3. Compute OI metrics
        total_call_oi = 0
        total_put_oi = 0
        max_call_oi = 0
        max_call_strike = 0
        max_put_oi = 0
        max_put_strike = 0
        strikes = []

        for item in chain:
            strike = item.get("strike_price", 0)
            call_oi = (item.get("call_options") or {}).get("market_data", {}).get("oi", 0) or 0
            put_oi = (item.get("put_options") or {}).get("market_data", {}).get("oi", 0) or 0
            call_chg = (item.get("call_options") or {}).get("market_data", {}).get("oi_day_change", 0) or 0
            put_chg = (item.get("put_options") or {}).get("market_data", {}).get("oi_day_change", 0) or 0

            total_call_oi += call_oi
            total_put_oi += put_oi

            if call_oi > max_call_oi:
                max_call_oi = call_oi
                max_call_strike = strike
            if put_oi > max_put_oi:
                max_put_oi = put_oi
                max_put_strike = strike

            strikes.append(
                {"strike": strike, "call_oi": call_oi, "put_oi": put_oi, "call_chg": call_chg, "put_chg": put_chg}
            )

        # 4. Calculate Max Pain
        max_pain_strike = 0
        min_total_loss = float("inf")
        for row in strikes:
            total_loss = 0
            for s in strikes:
                total_loss += max(0, row["strike"] - s["strike"]) * s["call_oi"]
                total_loss += max(0, s["strike"] - row["strike"]) * s["put_oi"]
            if total_loss < min_total_loss:
                min_total_loss = total_loss
                max_pain_strike = row["strike"]

        pcr = round(total_put_oi / total_call_oi, 2) if total_call_oi else 0

        # 5. Compute signal (same 5-layer logic as dashboard)
        bull_score = 0
        bear_score = 0
        layers = []

        # Layer 1: PCR
        if pcr > 1.2:
            bull_score += 2
            layers.append({"name": "PCR", "value": f"{pcr} Bullish", "bias": "bull"})
        elif pcr < 0.7:
            bear_score += 2
            layers.append({"name": "PCR", "value": f"{pcr} Bearish", "bias": "bear"})
        else:
            layers.append({"name": "PCR", "value": f"{pcr} Neutral", "bias": "neutral"})

        # Layer 2: Max Pain vs price
        if price > max_pain_strike + 75:
            bear_score += 1
            layers.append({"name": "Max Pain", "value": f"Above {max_pain_strike}", "bias": "bear"})
        elif price < max_pain_strike - 75:
            bull_score += 1
            layers.append({"name": "Max Pain", "value": f"Below {max_pain_strike}", "bias": "bull"})
        else:
            layers.append({"name": "Max Pain", "value": f"Near {max_pain_strike}", "bias": "neutral"})

        # Layer 3: Call OI wall proximity
        dist_call = max_call_strike - price
        if 0 < dist_call < 200:
            bear_score += 1
            layers.append({"name": "Call OI Wall", "value": f"Resistance @{max_call_strike}", "bias": "bear"})
        else:
            layers.append({"name": "Call OI Wall", "value": f"Clear @{max_call_strike}", "bias": "neutral"})

        # Layer 4: Put OI wall proximity
        dist_put = price - max_put_strike
        if 0 < dist_put < 200:
            bull_score += 1
            layers.append({"name": "Put OI Wall", "value": f"Support @{max_put_strike}", "bias": "bull"})
        else:
            layers.append({"name": "Put OI Wall", "value": f"Floor @{max_put_strike}", "bias": "neutral"})

        # Final signal
        if pcr <= 1.2 and pcr >= 0.7:
            signal = "SKIP"
            reason = f"PCR neutral at {pcr}. No high-probability setup."
        elif bull_score >= 3:
            signal = "BUY CALL"
            reason = f"Bullish signal ({bull_score}/4 layers). Support near {max_put_strike}."
        elif bear_score >= 3:
            signal = "BUY PUT"
            reason = f"Bearish signal ({bear_score}/4 layers). Resistance near {max_call_strike}."
        elif bull_score > bear_score:
            signal = "CALL - WAIT"
            reason = f"Leaning bullish ({bull_score}/4). Wait for confirmation."
        elif bear_score > bull_score:
            signal = "PUT - WAIT"
            reason = f"Leaning bearish ({bear_score}/4). Wait for confirmation."
        else:
            signal = "SKIP"
            reason = "Conflicting signals. No trade."

        # Top strikes for table (closest to current price)
        strikes_sorted = sorted(strikes, key=lambda x: abs(x["strike"] - price))[:8]
        strikes_sorted = sorted(strikes_sorted, key=lambda x: x["strike"])

        return jsonify(
            {
                "price": price,
                "change_pct": change_pct,
                "pcr": pcr,
                "total_call_oi": total_call_oi,
                "total_put_oi": total_put_oi,
                "max_call_strike": max_call_strike,
                "max_put_strike": max_put_strike,
                "max_pain": max_pain_strike,
                "expiry": expiry,
                "signal": signal,
                "reason": reason,
                "bull_score": bull_score,
                "bear_score": bear_score,
                "layers": layers,
                "strikes": strikes_sorted,
                "timestamp": datetime.now().strftime("%H:%M:%S"),
            }
        )

    except requests.exceptions.RequestException as e:
        return jsonify({"error": "Network error contacting Upstox", "detail": str(e)}), 502
    except Exception as e:
        return jsonify({"error": "Server error", "detail": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
