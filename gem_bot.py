import os
import re
import json
import pandas as pd
import yfinance as yf
from pathlib import Path
from datetime import date, datetime

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
    return {"active_label": "DM ex-US (EXUS)", "last_rebalance_month": None}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

def month_end_series(series: pd.Series) -> pd.Series:
    """Poprawka błędu 'M' vs 'ME' w nowym pandas"""
    s = series.dropna()
    if s.empty: return s
    try:
        return s.resample("ME").last().dropna()
    except ValueError:
        return s.resample("M").last().dropna()

def main():
    tickers_map = json.loads(os.getenv("GEM_TICKERS_JSON", "{}"))
    risk_assets = json.loads(os.getenv("GEM_RISK_ASSETS_JSON", "[]"))
    bonds_name = os.getenv("GEM_BONDS_NAME", "BONDS (VAGF)")
    capital_eur = os.getenv("GEM_CAPITAL_EUR", "583")

    details = {}
    for name, ticker in tickers_map.items():
        df = yf.download(ticker, period="2y", interval="1d", progress=False)["Adj Close"]
        me = month_end_series(df)
        if len(me) < 13: continue
        r12_1 = (me.iloc[-2] / me.iloc[-13]) - 1
        r6 = (me.iloc[-1] / me.iloc[-7]) - 1
        details[name] = {"score": (r12_1 + r6) / 2, "r12_1": r12_1, "r6": r6}

    ranked = sorted([(n, details[n]["score"]) for n in risk_assets if n in details], 
                    key=lambda x: x[1], reverse=True)
    
    top_name, top_score = ranked[0]
    choice = top_name if top_score > details.get(bonds_name, {"score": -1})["score"] else bonds_name

    # ====== SEKCJA STANU (state.json) ======
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

    # ====== BUDOWANIE RAPORTU ======
    lines = [f"GEM SIGNAL - {datetime.now().strftime('%Y-%m-%d')}", "", "RANKING:"]
    for i, (n, _) in enumerate(ranked, 1):
        lines.append(f"{i}. {n}: {fmt_pct(details[n]['score'])}")
    
    lines.append(f"\nAKCJA: {action_title}")
    lines.append(f"{'SPRZEDAJ: ' + current_active_label + ' -> KUP: ' + choice if rebalance_needed else 'Pozostań w: ' + current_active_label}")
    lines.append(f"KWOTA: {capital_eur} EUR")
    lines.append(f"\nStatus bota: {status_note}")
    
    OUT_FILE.write_text("\n".join(lines), encoding="utf-8")

if __name__ == "__main__":
    main()
