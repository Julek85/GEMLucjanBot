import os
import re
import json
import pandas as pd
import yfinance as yf
from pathlib import Path
from datetime import date, datetime

STATE_FILE = Path("state.json")
OUT_FILE = Path("gem_message.txt")

# =========================
# Helpers
# =========================

def fmt_pct(x: float) -> str:
    return f"{x * 100:.2f}%"

def extract_ticker_from_label(label: str) -> str:
    """Wyciąga ticker z etykiety typu: 'USA (VUAA)' -> 'VUAA'"""
    if not label:
        return ""
    # Poprawiony REGEX: szuka tekstu w nawiasach
    m = re.search(r"\(([^)]+)\)", label)
    return m.group(1).strip().upper() if m else ""

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"active_label": "DM ex-US (EXUS)", "last_rebalance_month": None}

def save_state(state: dict):
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )

def month_end_series(series: pd.Series) -> pd.Series:
    """Ujednolicone month-end (ME/M)."""
    s = series.dropna()
    if s.empty:
        return s
    try:
        return s.resample("ME").last().dropna()
    except ValueError:
        return s.resample("M").last().dropna()

def load_json_env(var_name: str, default):
    """Bezpieczne ładowanie JSON z parametrów środowiskowych."""
    raw = os.getenv(var_name)
    if raw is None:
        return default
    raw = raw.strip()
    if raw == "":
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print(f"[ERROR] {var_name} nie jest poprawnym JSON-em. Początek: {raw[:120]}...")
        return default

def get_close_series(df: pd.DataFrame) -> pd.Series:
    """Obsługa Adj Close/Close oraz MultiIndex z yfinance."""
    if df is None or df.empty:
        return pd.Series(dtype="float64")

    if isinstance(df.columns, pd.MultiIndex):
        level0 = df.columns.get_level_values(0)
        if "Adj Close" in level0:
            s = df.xs("Adj Close", level=0, axis=1)
            return s.iloc[:, 0].dropna()
        if "Close" in level0:
            s = df.xs("Close", level=0, axis=1)
            return s.iloc[:, 0].dropna()

    if "Adj Close" in df.columns:
        return df["Adj Close"].dropna()
    if "Close" in df.columns:
        return df["Close"].dropna()
    return pd.Series(dtype="float64")

# =========================
# Main
# =========================

def main():
    # ---- ENV (pancerne) ----
    tickers_map = load_json_env("GEM_TICKERS_JSON", {})
    risk_assets = load_json_env("GEM_RISK_ASSETS_JSON", [])
    bonds_name = os.getenv("GEM_BONDS_NAME", "BONDS (VAGF)")
    capital_eur = os.getenv("GEM_CAPITAL_EUR", "583")

    if not isinstance(tickers_map, dict): tickers_map = {}
    if not isinstance(risk_assets, list): risk_assets = []

    if not tickers_map:
        msg = "🔴 GEM Bot Error: Brak tickerów w GEM_TICKERS_JSON."
        OUT_FILE.write_text(msg, encoding="utf-8")
        print(msg)
        return

    # ---- Pobranie danych + liczenie momentum ----
    details = {}
    for name, ticker in tickers_map.items():
        try:
            raw = yf.download(ticker, period="2y", interval="1d", progress=False)
            series = get_close_series(raw)
            me = month_end_series(series)
            
            if len(me) < 13:
                print(f"[WARN] Za mało danych dla {ticker} (wymagane 13 m-cy).")
                continue

            r12_1 = (me.iloc[-2] / me.iloc[-13]) - 1
            r6 = (me.iloc[-1] / me.iloc[-7]) - 1
            score = (r12_1 + r6) / 2
            details[name] = {"score": score, "r12_1": r12_1, "r6": r6}
        except Exception as e:
            print(f"[WARN] Problem z tickerem {ticker} ({name}): {e}")

    # ---- Ranking aktywów ryzykownych ----
    ranked = sorted(
        [(n, details[n]["score"]) for n in risk_assets if n in details], 
        key=lambda x: x[1], 
        reverse=True
    )

    # ---- Wybór lidera ----
    bonds_score = details.get(bonds_name, {}).get("score", float("-inf"))
    
    if ranked:
        top_name, top_score = ranked[0]
        choice = top_name if top_score > bonds_score else bonds_name
    else:
        choice = bonds_name if bonds_score != float("-inf") else "Brak danych"

    if choice == "Brak danych":
        OUT_FILE.write_text("🔴 GEM Bot Error: Nie udało się pobrać danych dla żadnego aktywa.", encoding="utf-8")
        return

    # ==============================
    # STATE (state.json) + decyzja
    # ==============================
    state = load_state()
    today = date.today()
    current_month = f"{today.year}-{today.month:02d}"
    
    current_active_label = state.get("active_label", "DM ex-US (EXUS)")
    
    new_ticker = extract_ticker_from_label(choice)
    current_ticker = extract_ticker_from_label(current_active_label)

    is_new_month = (state.get("last_rebalance_month") != current_month)
    is_different_asset = (current_ticker != new_ticker)

    if is_new_month and is_different_asset:
        rebalance_needed = True
        action_title = "ZMIANA POZYCJI"
        status_note = f"WYKRYTO ZMIANĘ: {current_active_label} -> {choice}"
        state.update({"active_label": choice, "last_rebalance_month": current_month})
        save_state(state)
    else:
        rebalance_needed = False
        action_title = "TRZYMAJ"
        status_note = "Utrzymujemy obecną pozycję"
        if is_new_month:
            state["last_rebalance_month"] = current_month
            save_state(state)

    # ==============================
    # Budowanie raportu
    # ==============================
    lines = [
        f"GEM SIGNAL - {datetime.now().strftime('%Y-%m-%d')}",
        "",
        "RANKING (risk assets):"
    ]
    
    if ranked:
        for i, (n, _) in enumerate(ranked, 1):
            lines.append(f"{i}. {n}: {fmt_pct(details[n]['score'])}")
    else:
        lines.append("Brak danych do rankingu (risk assets).")

    if bonds_name in details:
        lines.append(f"\nBONDS score: {fmt_pct(details[bonds_name]['score'])}")
    else:
        lines.append("\nBONDS score: brak danych.")

    lines.append(f"\nAKCJA: {action_title}")
    if rebalance_needed:
        lines.append(f"SPRZEDAJ: {current_active_label} -> KUP: {choice}")
    else:
        lines.append(f"Pozostań w: {current_active_label}")

    lines.append(f"KWOTA: {capital_eur} EUR")
    lines.append(f"\nStatus bota: {status_note}")

    OUT_FILE.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    main()
