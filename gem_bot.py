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
    """Ujednolicone month-end (kompatybilność ME vs M)."""
    s = series.dropna()
    if s.empty:
        return s
    try:
        return s.resample("ME").last().dropna()
    except ValueError:
        return s.resample("M").last().dropna()

def load_json_env(var_name: str, default):
    """Pancerny loader JSON z env."""
    raw = os.getenv(var_name)
    if not raw or not raw.strip():
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print(f"[ERROR] {var_name} nie jest poprawnym JSON-em.")
        return default

def get_close_series(df: pd.DataFrame) -> pd.Series:
    """Obsługuje MultiIndex (yfinance) i fallback Adj Close -> Close."""
    if df is None or df.empty:
        return pd.Series(dtype="float64")
    
    if isinstance(df.columns, pd.MultiIndex):
        level0 = df.columns.get_level_values(0)
        for col in ["Adj Close", "Close"]:
            if col in level0:
                return df.xs(col, level=0, axis=1).iloc[:, 0].dropna()
    
    for col in ["Adj Close", "Close"]:
        if col in df.columns:
            return df[col].dropna()
    return pd.Series(dtype="float64")

# =========================
# Main Logic
# =========================

def main():
    tickers_map = load_json_env("GEM_TICKERS_JSON", {})
    risk_assets = load_json_env("GEM_RISK_ASSETS_JSON", [])
    bonds_name = os.getenv("GEM_BONDS_NAME", "BONDS (VAGF)")
    capital_eur = os.getenv("GEM_CAPITAL_EUR", "583")

    if not tickers_map:
        msg = "🔴 GEM Bot Error: Brak tickerów w GEM_TICKERS_JSON."
        OUT_FILE.write_text(msg, encoding="utf-8")
        return

    details = {}
    for name, ticker in tickers_map.items():
        try:
            raw = yf.download(ticker, period="2y", interval="1d", progress=False)
            series = get_close_series(raw)
            me = month_end_series(series)
            
            if len(me) < 13:
                continue

            r12_1 = (me.iloc[-2] / me.iloc[-13]) - 1
            r6 = (me.iloc[-1] / me.iloc[-7]) - 1
            details[name] = {"score": (r12_1 + r6) / 2, "r12_1": r12_1, "r6": r6}
        except Exception as e:
            print(f"[WARN] Problem z {ticker}: {e}")

    ranked = sorted(
        [(n, details[n]["score"]) for n in risk_assets if n in details], 
        key=lambda x: x[1], reverse=True
    )

    bonds_score = details.get(bonds_name, {}).get("score", float("-inf"))
    
    if ranked:
        top_name, top_score = ranked[0]
        choice = top_name if top_score > bonds_score else bonds_name
    else:
        choice = bonds_name if bonds_score != float("-inf") else "Brak danych"

    # --- SEKCJA STANU ---
    state = load_state()
    today = date.today()
    current_month = f"{today.year}-{today.month:02d}"
    current_active_label = state.get("active_label", "DM ex-US (EXUS)")
    
    is_new_month = (state.get("last_rebalance_month") != current_month)
    is_different = (extract_ticker_from_label(current_active_label) != extract_ticker_from_label(choice))

    if is_new_month and is_different:
        rebalance_needed, action_title = True, "ZMIANA POZYCJI"
        status_note = f"ZMIANA: {current_active_label} -> {choice}"
        state.update({"active_label": choice, "last_rebalance_month": current_month})
        save_state(state)
    else:
        rebalance_needed, action_title = False, "TRZYMAJ"
        status_note = "Utrzymujemy pozycję"
        if is_new_month:
            state["last_rebalance_month"] = current_month
            save_state(state)

    # --- RAPORT ---
    lines = [f"GEM SIGNAL - {datetime.now().strftime('%Y-%m-%d')}", "", "RANKING:"]
    for i, (n, _) in enumerate(ranked, 1):
        lines.append(f"{i}. {n}: {fmt_pct(details[n]['score'])}")
    
    lines.append(f"\nBONDS score: {fmt_pct(bonds_score)}")
    lines.append(f"\nAKCJA: {action_title}")
    lines.append(f"{'SPRZEDAJ: ' + current_active_label + ' -> KUP: ' + choice if rebalance_needed else 'Pozostań w: ' + current_active_label}")
    lines.append(f"KWOTA: {capital_eur} EUR\nStatus: {status_note}")

    OUT_FILE.write_text("\n".join(lines), encoding="utf-8")

if __name__ == "__main__":
    main()
