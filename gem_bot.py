import os
import json
import yfinance as yf
from datetime import datetime

# -------------------------
# Load environment variables
# -------------------------

TICKERS = json.loads(os.getenv("GEM_TICKERS_JSON"))
RISK_ASSETS = json.loads(os.getenv("GEM_RISK_ASSETS_JSON"))
BONDS_NAME = os.getenv("GEM_BONDS_NAME")
CAPITAL = float(os.getenv("GEM_CAPITAL_EUR", "583"))

STATE_FILE = "state.json"
MESSAGE_FILE = "gem_message.txt"

# -------------------------
# Load & Save state.json
# -------------------------

def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "current_position": None,
            "enter_date": None,
            "portfolio_value": CAPITAL
        }
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=4)

# -------------------------
# Momentum calculation
# -------------------------

def get_momentum(ticker):
    try:
        # Pobieramy 13 miesięcy, żeby mieć punkt odniesienia dokładnie sprzed roku
        data = yf.download(ticker, period="13mo", interval="1mo", progress=False)
        
        if data.empty or len(data) < 2:
            return None
            
        current_price = float(data["Close"].iloc[-1])
        
        # PANCERNA LOGIKA LUCJANA:
        start_price = float(data["Close"].iloc[-13]) if len(data) > 12 else float(data["Close"].iloc[0])
        
        return round((current_price / start_price - 1) * 100, 2)
    except Exception as e:
        print(f"Błąd pobierania {ticker}: {e}")
        return None

# -------------------------
# Main GEM logic
# -------------------------

def generate_gem_signal():
    state = load_state()

    # Calculate momentum for each asset
    momentum = {}
    for name, ticker in TICKERS.items():
        m = get_momentum(ticker)
        momentum[name] = m

    # Bezpieczna funkcja do sortowania (traktuje None jako błąd/strata)
    def safe_val(x): return x if x is not None else -999.0

    # Sort by momentum
    sorted_momentum = sorted(momentum.items(), key=lambda x: safe_val(x[1]), reverse=True)

    # Best risk asset
    risk_data = [(a, momentum[a]) for a in RISK_ASSETS if a in momentum]
    best_risk = max(risk_data, key=lambda x: safe_val(x[1]))

    # Bonds momentum
    bonds_m = momentum.get(BONDS_NAME)

    # Determine mode
    mode = "RISK-ON ✅" if safe_val(best_risk[1]) > safe_val(bonds_m) else "RISK-OFF 🛡️"

    # Determine GEM position
    new_position = best_risk[0] if safe_val(best_risk[1]) > safe_val(bonds_m) else BONDS_NAME
    changed = new_position != state["current_position"]

    # Portfolio logic init
    if state["current_position"] is None:
        state["current_position"] = new_position
        state["enter_date"] = datetime.now().strftime("%Y-%m")
        state["portfolio_value"] = CAPITAL

    # -------------------------
    # Build momentum ranking with medals & trend arrows
    # -------------------------
    ranking_lines = []
    medals = ["🥇", "🥈", "🥉"]

    for i, (name, m) in enumerate(sorted_momentum):
        # Medale dla top 3, liczby dla reszty
        prefix = medals[i] if i < 3 else f"{i+1}️⃣"
        
        # Zabezpieczenie przed None przy wyświetlaniu
        if m is None:
            ranking_lines.append(f"{prefix} {name} — BŁĄD DANYCH ⚠️")
        else:
            # Strzałka trendu
            trend = "⬆️" if m > 0 else "⬇️"
            ranking_lines.append(f"{prefix} {name} — {m}% {trend}")

    ranking_text = "\n".join(ranking_lines)
    
    # Wyświetlanie obligacji (zabezpieczenie)
    bonds_display = f"{bonds_m}%" if bonds_m is not None else "BŁĄD ⚠️"

    # -------------------------
    # Build final message
    # -------------------------
    msg = f"""📌 GEM SIGNAL — {datetime.now().strftime("%Y-%m-%d")}

📊 MOMENTUM RANKING:
{ranking_text}

🚦 TRYB: {mode}
🏆 LIDER: {best_risk[0]} ({safe_val(best_risk[1])}%)
🛡️ OBLIGACJE: {bonds_display}

🎯 AKCJA: {"ZMIANA 🔁" if changed else "TRZYMAJ 🟦"}
📌 AKTUALNIE W: {state["current_position"]}

📈 WARTOŚĆ: {round(state["portfolio_value"], 2)} EUR
🕒 W POZYCJI OD: {state["enter_date"]}

Status: Portfel OK
"""

    # Write message for Telegram
    with open(MESSAGE_FILE, "w", encoding="utf-8") as f:
        f.write(msg)

    # Update state (only position if changed)
    if changed:
        state["current_position"] = new_position
        state["enter_date"] = datetime.now().strftime("%Y-%m")

    save_state(state)

# -------------------------
# Run GEM bot
# -------------------------
# UWAGA: Tutaj musi być podwójne podkreślenie!
if __name__ == "__main__":
    generate_gem_signal()
