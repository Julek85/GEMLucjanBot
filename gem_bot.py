# GEM Bot – wersja produkcyjna v3.2 (Naprawiony RISK-OFF + Opcja A + %)

import os
import re
import json
import time
from pathlib import Path
from datetime import date, datetime
import sys

# --- KLUCZOWE IMPORTY (Tego brakowało!) ---
import pandas as pd
import yfinance as yf
# ------------------------------------------

# Wymuszenie UTF-8 dla logów
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
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"active_label": "DM ex-US (EXUS)", "last_rebalance_month": None, "active_since": None, "entry_price": None}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

def yf_download_with_retries(symbols, *, period="3y", interval="1d", attempts=3, sleep_sec=5, timeout=20):
    last_exc = None
    for i in range(1, attempts + 1):
        try:
            data = yf.download(
                symbols,
                period=period,
                interval=interval,
                auto_adjust=False,
                progress=False,
                group_by='column',
                timeout=timeout,
                threads=False,
            )
            if data is None or getattr(data, 'empty', True):
                raise ValueError('yfinance zwrócił pusty wynik (empty).')
            if data.isna().all().all():
                raise ValueError('yfinance zwrócił dane, ale wszystkie wartości są NaN.')
            return data
        except Exception as e:
            last_exc = e
            if i < attempts:
                time.sleep(sleep_sec)
    raise RuntimeError(f"yfinance: nie udało się pobrać danych po {attempts} próbach. Ostatni błąd: {last_exc}")

def month_end_series(series: pd.Series) -> pd.Series:
    s = series.dropna()
    if s.empty: return s
    s.index = pd.to_datetime(s.index, errors="coerce")
    s = s[~s.index.isna()]
    if s.empty: return s
    try:
        # Fallback dla różnych wersji pandas (ME vs M)
        return s.resample("ME").last().dropna()
    except ValueError:
        return s.resample("M").last().dropna()

def calc_momentum(monthly: pd.Series) -> tuple[float, float]:
    if len(monthly) < 13:
        raise ValueError("Za mało danych (min. 13 miesięcy).")
    r12_1 = (monthly.iloc[-2] / monthly.iloc[-13]) - 1.0
    # Obliczamy 6M na podstawie ostatnich 7 punktów końcomiesięcznych
    r6 = (monthly.iloc[-1] / monthly.iloc[-7]) - 1.0 if len(monthly) >= 7 else 0.0
    return float(r12_1), float(r6)

def main():
    try:
        # Pobieranie konfiguracji z ENV
        tickers = json.loads(os.environ.get("GEM_TICKERS_JSON", "{}"))
        risk_assets = json.loads(os.environ.get("GEM_RISK_ASSETS_JSON", "[]"))
        bonds_name = os.environ.get("GEM_BONDS_NAME", "BONDS (AGGH)")
        capital_eur = os.environ.get("GEM_CAPITAL_EUR", "0")

        if not tickers or not risk_assets:
            raise ValueError("Brak konfiguracji ENV.")

        yf_symbols = list(set(tickers.values()))
        data = yf_download_with_retries(yf_symbols)

        def get_adj(symbol: str) -> pd.Series:
            if isinstance(data.columns, pd.MultiIndex):
                return data[("Adj Close", symbol)]
            return data["Adj Close"]

        details = {}
        for label, sym in tickers.items():
            try:
                adj = get_adj(sym)
                monthly = month_end_series(adj)
                r12_1, r6 = calc_momentum(monthly)
                score = (r12_1 + r6) / 2.0
                last_price = float(adj.dropna().iloc[-1])
                details[label] = {"sym": sym, "score": score, "last_price": last_price}
            except Exception as e:
                print(f"Pominięto {label}: {e}")

        # Ranking aktywów ryzykowanych
        ranked = sorted(
            [(name, details[name]["score"]) for name in risk_assets if name in details],
            key=lambda x: x[1],
            reverse=True
        )

        top_name, top_score = ranked[0]
        bonds_score = details.get(bonds_name, {}).get("score", -99.0)

        # Logika GEM: Akcje vs Obligacje
        is_risk_on = top_score > bonds_score
        choice = top_name if is_risk_on else bonds_name
        mode_str = "RISK-ON ✅" if is_risk_on else "RISK-OFF 🛡️"

        # Stan i wycena (Opcja A)
        state = load_state()
        today = date.today()
        current_month = f"{today.year}-{today.month:02d}"
        
        current_active_label = state.get("active_label", choice)
        
        # Jeśli to pierwsze uruchomienie, zapisz cenę wejścia
        if state.get("entry_price") is None and current_active_label in details:
            state["entry_price"] = details[current_active_label]["last_price"]
            state["active_since"] = current_month

        # Sprawdzenie zmiany pozycji
        rebalance_needed = False
        if current_active_label != choice and state.get("last_rebalance_month") != current_month:
            rebalance_needed = True
            state["active_label"] = choice
            state["entry_price"] = details[choice]["last_price"]
            state["active_since"] = current_month
        
        state["last_rebalance_month"] = current_month
        save_state(state)

        # Obliczanie zysku/straty
        report_val = ""
        if state.get("entry_price") and current_active_label in details:
            c_price = details[current_active_label]["last_price"]
            e_price = state["entry_price"]
            current_value = float(capital_eur) * (c_price / e_price)
            gain_pct = (c_price / e_price - 1) * 100
            report_val = f"\n📈 WARTOŚĆ: {current_value:.2f} EUR ({gain_pct:+.2f}%)"

        # Budowa wiadomości
        msg = [
            f"📌 GEM SIGNAL — {today}",
            f"🚦 TRYB: {mode_str}",
            f"🏆 LIDER: {top_name} ({fmt_pct(top_score)})",
            f"🛡️ OBLIGACJE: {fmt_pct(bonds_score)}",
            "",
            "🎯 AKCJA: " + ("ZMIANA POZYCJI 🔁" if rebalance_needed else "TRZYMAJ 🟦"),
            f"📌 AKTUALNIE W: {choice}",
            report_val,
            f"🕒 W POZYCJI OD: {state.get('active_since')}",
            f"\nStatus: {'Wymagana transakcja!' if rebalance_needed else 'Portfel OK'}"
        ]

        OUT_FILE.write_text("\n".join(msg), encoding="utf-8")

    except Exception as e:
        OUT_FILE.write_text(f"🔴 BŁĄD BOTA: {e}", encoding="utf-8")
        raise e

if __name__ == "__main__":
    main()
