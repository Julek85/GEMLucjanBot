import os
import json
import yfinance as yf
import math
from datetime import datetime

# -------------------------
# 1. Konfiguracja
# -------------------------
TICKERS = json.loads(os.getenv("GEM_TICKERS_JSON"))
RISK_ASSETS = json.loads(os.getenv("GEM_RISK_ASSETS_JSON"))
BONDS_NAME = os.getenv("GEM_BONDS_NAME")
CAPITAL_START = float(os.getenv("GEM_CAPITAL_EUR", "544"))

STATE_FILE = "state.json"
MESSAGE_FILE = "gem_message.txt"

# -------------------------
# 2. Zarządzanie Stanem
# -------------------------
def load_state():
    default_state = {
        "current_position": None,
        "enter_date": datetime.now().strftime("%Y-%m"),
        "portfolio_value": CAPITAL_START,
        "last_price": None
    }
    if not os.path.exists(STATE_FILE):
        return default_state
    with open(STATE_FILE, "r") as f:
        try:
            state = json.load(f)
            for key, value in default_state.items():
                if key not in state: state[key] = value
            return state
        except json.JSONDecodeError:
            return default_state

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=4)

# -------------------------
# 3. Logika Momentum (Pancerna)
# -------------------------
def get_momentum(ticker):
    try:
        ticker_obj = yf.Ticker(ticker)
        # Pobieramy 14 miesięcy, by mieć zapas po usunięciu pustych dni
        data = ticker_obj.history(period="14mo", interval="1mo")
        
        # KLUCZOWE: Usuwamy puste wiersze (np. dzisiejszy niepełny bar)
        data = data.dropna(subset=["Close"])
        
        if len(data) < 2:
            return None
            
        current_price = float(data["Close"].iloc[-1])
        # Logika Lucjana: punkt sprzed roku (13-sty wiersz od końca lub pierwszy dostępny)
        idx = -13 if len(data) >= 13 else 0
        start_price = float(data["Close"].iloc[idx])
        
        if start_price == 0: return None
        
        momentum_val = (current_price / start_price - 1) * 100
        return round(momentum_val, 2) if not math.isnan(momentum_val) else None
    except Exception:
        return None

# -------------------------
# 4. Główny Algorytm GEM
# -------------------------
def generate_gem_signal():
    state = load_state()
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    # AKTUALIZACJA WYCENY
    current_pos_name = state.get("current_position")
    if current_pos_name in TICKERS:
        try:
            current_asset_data = yf.Ticker(TICKERS[current_pos_name]).history(period="5d").dropna()
            if not current_asset_data.empty:
                new_price = float(current_asset_data["Close"].iloc[-1])
                if state.get("last_price") and state["last_price"] > 0:
                    state["portfolio_value"] *= (new_price / state["last_price"])
                state["last_price"] = new_price
        except:
            print("Błąd aktualizacji wyceny")

    # RANKING MOMENTUM
    momentum = {name: get_momentum(ticker) for name, ticker in TICKERS.items()}
    
    def safe_val(x): 
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return -999.0
        return x

    sorted_momentum = sorted(momentum.items(), key=lambda x: safe_val(x[1]), reverse=True)
    risk_data = [(a, momentum[a]) for a in RISK_ASSETS if a in momentum]
    best_risk = max(risk_data, key=lambda x: safe_val(x[1]))
    bonds_m = momentum.get(BONDS_NAME)

    # LOGIKA SYGNAŁU (Zabezpieczona przed brakiem danych)
    if best_risk[1] is None or math.isnan(safe_val(best_risk[1])):
        mode = "BŁĄD DANYCH ⚠️"
        new_position = state.get("current_position")
        changed = False
    else:
        mode = "RISK-ON ✅" if safe_val(best_risk[1]) > safe_val(bonds_m) else "RISK-OFF 🛡️"
        new_position = best_risk[0] if safe_val(best_risk[1]) > safe_val(bonds_m) else BONDS_NAME
        changed = new_position != state.get("current_position")

    if changed:
        state["current_position"] = new_position
        state["enter_date"] = datetime.now().strftime("%Y-%m")
        try:
            new_ticker_data = yf.Ticker(TICKERS[new_position]).history(period="5d").dropna()
            state["last_price"] = float(new_ticker_data["Close"].iloc[-1])
        except:
            state["last_price"] = None

    # RAPORT
    total_return_pct = (state["portfolio_value"] / CAPITAL_START - 1) * 100
    ranking_lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, (name, m) in enumerate(sorted_momentum):
        prefix = medals[i] if i < 3 else f"{i+1}️⃣"
        is_valid = m is not None and not (isinstance(m, float) and math.isnan(m))
        val_display = f"{m}% {'⬆️' if m > 0 else '⬇️'}" if is_valid else "BŁĄD DANYCH ⚠️"
        ranking_lines.append(f"{prefix} {name} — {val_display}")

    ranking_text = "\n".join(ranking_lines)
    lider_val = f"{best_risk[1]}%" if best_risk[1] is not None else "BRAK"
    bonds_display = f"{bonds_m}%" if bonds_m is not None else "BRAK"

    msg = f"""📌 GEM SIGNAL — {today_str}

📊 MOMENTUM RANKING:
{ranking_text}

🚦 TRYB: {mode}
🏆 LIDER: {best_risk[0]} ({lider_val})
🛡️ OBLIGACJE: {bonds_display}

🎯 AKCJA: {"ZMIANA 🔁" if changed else "TRZYMAJ 🟦"}
📌 AKTUALNIE W: {state['current_position']}

📈 WARTOŚĆ: {round(state['portfolio_value'], 2)} EUR ({total_return_pct:+.2f}%)
🕒 W POZYCJI OD: {state['enter_date']}

Status: Portfel OK
"""
    with open(MESSAGE_FILE, "w", encoding="utf-8") as f:
        f.write(msg)
    save_state(state)

if __name__ == "__main__":
    generate_gem_signal()
