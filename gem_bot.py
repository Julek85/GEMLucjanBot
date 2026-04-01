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
CAPITAL_START = float(os.getenv("GEM_CAPITAL_EUR", "583")) # Twoja pierwotna wpłata

STATE_FILE = "state.json"
MESSAGE_FILE = "gem_message.txt"

# -------------------------
# Load & Save state.json
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
            # Uzupełnianie brakujących kluczy (np. po aktualizacji skryptu)
            for key, value in default_state.items():
                if key not in state:
                    state[key] = value
            return state
        except json.JSONDecodeError:
            return default_state

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=4)

# -------------------------
# Momentum calculation (Lucjan's Logic)
# -------------------------
def get_momentum(ticker):
    try:
        ticker_obj = yf.Ticker(ticker)
        data = ticker_obj.history(period="13mo", interval="1mo")
        
        if data.empty or len(data) < 2:
            return None
            
        current_price = float(data["Close"].iloc[-1])
        # Logika: bierzemy cenę sprzed 12 miesięcy (iloc[0]) lub 13-stą od końca
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
    
    # 1. AKTUALIZACJA WARTOŚCI PORTFELA
    # Sprawdzamy o ile zmienił się obecny ETF od ostatniego miesiąca
    current_pos_name = state.get("current_position")
    if current_pos_name in TICKERS:
        ticker_symbol = TICKERS[current_pos_name]
        try:
            current_asset_data = yf.Ticker(ticker_symbol).history(period="5d")
            if not current_asset_data.empty:
                new_price = float(current_asset_data["Close"].iloc[-1])
                
                # Jeśli mamy zapisaną cenę z poprzedniego miesiąca, liczymy zysk/stratę
                if state.get("last_price") and state["last_price"] > 0:
                    profit_ratio = new_price / state["last_price"]
                    state["portfolio_value"] *= profit_ratio
                    print(f"Aktualizacja wartości: {state['portfolio_value']:.2f} EUR (cena: {new_price})")
                
                state["last_price"] = new_price
        except Exception as e:
            print(f"Błąd aktualizacji wyceny: {e}")

    # 2. OBLICZANIE SYGNAŁÓW GEM
    momentum = {name: get_momentum(ticker) for name, ticker in TICKERS.items()}
    
    def safe_val(x): return x if x is not None else -999.0
    sorted_momentum = sorted(momentum.items(), key=lambda x: safe_val(x[1]), reverse=True)
    
    # Najlepszy aktyw ryzykowny
    risk_data = [(a, momentum[a]) for a in RISK_ASSETS if a in momentum]
    best_risk = max(risk_data, key=lambda x: safe_val(x[1]))
    bonds_m = momentum.get(BONDS_NAME)

    # Decyzja
    mode = "RISK-ON ✅" if safe_val(best_risk[1]) > safe_val(bonds_m) else "RISK-OFF 🛡️"
    new_position = best_risk[0] if safe_val(best_risk[1]) > safe_val(bonds_m) else BONDS_NAME
    
    changed = new_position != state.get("current_position")

    # 3. LOGIKA ZMIANY (REBALANCING)
    if changed:
        print(f"ZMIANA POZYCJI: {state['current_position']} -> {new_position}")
        state["current_position"] = new_position
        state["enter_date"] = datetime.now().strftime("%Y-%m")
        # Pobieramy cenę wejścia dla nowego instrumentu
        try:
            new_ticker_data = yf.Ticker(TICKERS[new_position]).history(period="5d")
            state["last_price"] = float(new_ticker_data["Close"].iloc[-1])
        except:
            state["last_price"] = None

    # 4. BUDOWANIE RAPORTU
    total_return_pct = (state["portfolio_value"] / CAPITAL_START - 1) * 100
    
    ranking_lines = []
    medals = ["🥇", "🥈", "🥉"]
    for i, (name, m) in enumerate(sorted_momentum):
        prefix = medals[i] if i < 3 else f"{i+1}️⃣"
        trend = "⬆️" if safe_val(m) > 0 else "⬇️"
        val_display = f"{m}% {trend}" if m is not None else "BŁĄD ⚠️"
        ranking_lines.append(f"{prefix} {name} — {val_display}")

    msg = f"""📌 GEM SIGNAL — {datetime.now().strftime("%Y-%m-%d")}

📊 MOMENTUM RANKING:
{"\n".join(ranking_lines)}

🚦 TRYB: {mode}
🏆 LIDER: {best_risk[0]} ({safe_val(best_risk[1])}%)
🛡️ OBLIGACJE: {bonds_m if bonds_m is not None else "BŁĄD"}%

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
