# GEM Bot – wersja produkcyjna v3.3 (Zabezpieczona + Debug)

import os
import re
import json
import time
from pathlib import Path
from datetime import date, datetime
import sys

# Próba importu z obsługą błędów środowiska
try:
    import pandas as pd
    import yfinance as yf
except ImportError:
    print("BŁĄD: Brak bibliotek pandas lub yfinance. Sprawdź requirements.txt!")
    sys.exit(1)

# Wymuszenie UTF-8 dla logów i Telegrama
if sys.stdout.encoding != 'utf-8':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

STATE_FILE = Path("state.json")
OUT_FILE = Path("gem_message.txt")

def fmt_pct(x: float) -> str:
    return f"{x*100:.2f}%"

def extract_ticker_from_label(label: str) -> str:
    if not label: return ""
    m = re.search(r"\(([^)]+)\)", label)
    return m.group(1).strip().upper() if m else ""

def load_state():
    default_state = {"active_label": "DM ex-US (EXUS)", "last_rebalance_month": None, "active_since": None, "entry_price": None}
    if STATE_FILE.exists():
        try:
            content = STATE_FILE.read_text(encoding="utf-8")
            return json.loads(content) if content.strip() else default_state
        except Exception as e:
            print(f"Log: Problem z plikiem stanu ({e}). Resetowanie do domyślnych.")
    return default_state

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

def yf_download_with_retries(symbols, attempts=3, sleep_sec=5):
    print(f"Log: Pobieranie danych dla: {symbols}...")
    last_exc = None
    for i in range(1, attempts + 1):
        try:
            data = yf.download(symbols, period="3y", interval="1d", auto_adjust=False, progress=False, threads=False, timeout=20)
            if data is None or data.empty:
                raise ValueError("Yahoo zwrócił pusty DataFrame.")
            print(f"Log: Pobrano dane pomyślnie (Próba {i}).")
            return data
        except Exception as e:
            print(f"Log: Próba {i} nieudana: {e}")
            last_exc = e
            if i < attempts: time.sleep(sleep_sec)
    raise RuntimeError(f"Błąd Yahoo Finance po {attempts} próbach: {last_exc}")

def month_end_series(series: pd.Series) -> pd.Series:
    s = series.dropna()
    s.index = pd.to_datetime(s.index)
    try:
        return s.resample("ME").last().dropna()
    except:
        return s.resample("M").last().dropna()

def calc_momentum(monthly: pd.Series) -> tuple[float, float]:
    if len(monthly) < 13:
        raise ValueError(f"Zbyt krótka historia (wymagane 13m, jest {len(monthly)}m)")
    r12_1 = (monthly.iloc[-2] / monthly.iloc[-13]) - 1.0
    r6 = (monthly.iloc[-1] / monthly.iloc[-7]) - 1.0 if len(monthly) >= 7 else 0.0
    return float(r12_1), float(r6)

def main():
    try:
        # 1. Konfiguracja
        tickers = json.loads(os.environ.get("GEM_TICKERS_JSON", "{}"))
        risk_assets = json.loads(os.environ.get("GEM_RISK_ASSETS_JSON", "[]"))
        bonds_name = os.environ.get("GEM_BONDS_NAME", "BONDS (AGGH)")
        capital_eur = float(os.environ.get("GEM_CAPITAL_EUR", "583"))

        # 2. Pobieranie
        data = yf_download_with_retries(list(tickers.values()))
        
        # Funkcja pomocnicza do wyciągania cen (odporna na błędy kolumn)
        def get_prices(symbol: str):
            for col in ["Adj Close", "Close"]:
                try:
                    if isinstance(data.columns, pd.MultiIndex):
                        if (col, symbol) in data.columns: return data[(col, symbol)]
                    else:
                        if col in data.columns: return data[col]
                except: continue
            raise KeyError(f"Brak kolumn cenowych dla {symbol}")

        # 3. Obliczenia
        details = {}
        for label, sym in tickers.items():
            try:
                print(f"Log: Obliczanie momentum dla {label} ({sym})...")
                prices = get_prices(sym)
                monthly = month_end_series(prices)
                r12_1, r6 = calc_momentum(monthly)
                score = (r12_1 + r6) / 2.0
                last_p = float(prices.dropna().iloc[-1])
                details[label] = {"score": score, "last_price": last_p}
            except Exception as e:
                print(f"Log: Pominąłem {label} z powodu błędu: {e}")

        # 4. Ranking i Sygnał
        ranked = sorted(
            [(n, details[n]["score"]) for n in risk_assets if n in details],
            key=lambda x: x[1], reverse=True
        )
        
        if not ranked: raise ValueError("Brak danych do stworzenia rankingu!")
        
        top_name, top_score = ranked[0]
        b_score = details.get(bonds_name, {}).get("score", -99.9)
        
        is_risk_on = top_score > b_score
        choice = top_name if is_risk_on else bonds_name
        mode_str = "RISK-ON ✅" if is_risk_on else "RISK-OFF 🛡️"

        # 5. Stan i Wycena (Opcja A)
        state = load_state()
        today = date.today()
        current_m = f"{today.year}-{today.month:02d}"
        
        active_label = state.get("active_label") or choice
        
        # Jeśli nie mamy ceny wejścia (pierwszy start), zapisujemy aktualną
        if state.get("entry_price") is None and active_label in details:
            state["entry_price"] = details[active_label]["last_price"]
            state["active_since"] = current_m

        # Sprawdzenie rebalancingu
        change = (active_label != choice and state.get("last_rebalance_month") != current_m)
        if change:
            print(f"Log: Zmiana pozycji z {active_label} na {choice}")
            state["active_label"] = choice
            state["entry_price"] = details[choice]["last_price"]
            state["active_since"] = current_m
            active_label = choice
        
        state["last_rebalance_month"] = current_m
        save_state(state)

        # 6. Raport
        report_val = ""
        if state.get("entry_price") and active_label in details:
            curr_p = details[active_label]["last_price"]
            ent_p = state["entry_price"]
            val = capital_eur * (curr_p / ent_p)
            gain = (curr_p / ent_p - 1) * 100
            report_val = f"\n📈 WARTOŚĆ: {val:.2f} EUR ({gain:+.2f}%)"

        msg = [
            f"📌 GEM SIGNAL — {today}",
            f"🚦 TRYB: {mode_str}",
            f"🏆 LIDER: {top_name} ({fmt_pct(top_score)})",
            f"🛡️ OBLIGACJE: {fmt_pct(b_score)}",
            "",
            "🎯 AKCJA: " + ("ZMIANA POZYCJI 🔁" if change else "TRZYMAJ 🟦"),
            f"📌 AKTUALNIE W: {choice}",
            report_val,
            f"🕒 W POZYCJI OD: {state.get('active_since')}",
            f"\nStatus: {'Wymagana transakcja!' if change else 'Portfel OK'}"
        ]
        
        OUT_FILE.write_text("\n".join(msg), encoding="utf-8")
        print("Log: Raport wygenerowany pomyślnie.")

    except Exception as e:
        print(f"Log: KRYTYCZNY BŁĄD: {e}")
        OUT_FILE.write_text(f"🔴 BŁĄD BOTA: {e}", encoding="utf-8")
        raise e

if __name__ == "__main__":
    main()
