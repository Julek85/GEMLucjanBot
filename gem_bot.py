import os
import re
import json
import pandas as pd
import yfinance as yf
from pathlib import Path
from datetime import date, datetime

STATE_FILE = Path("state.json")
OUT_FILE = Path("gem_message.txt")

def fmt_pct(x):
    return f"{x * 100:.2f}%" if x is not None else "Brak danych"

def fmt_pp(x):
    return f"{x * 100:+.2f} pp" if x is not None else "n/a"

def extract_ticker_from_label(label):
    if not label: return ""
    m = re.search(r"\(([^)]+)\)", label)
    return m.group(1).strip().upper() if m else ""

def get_close_series(df):
    """Pancerne pobieranie cen - rozwiązuje błąd KeyError: 'Adj Close'"""
    if df is None or df.empty: return pd.Series(dtype="float64")
    if isinstance(df.columns, pd.MultiIndex):
        level0 = df.columns.get_level_values(0)
        for col in ["Adj Close", "Close"]:
            if col in level0: return df.xs(col, level=0, axis=1).iloc[:, 0].dropna()
    for col in ["Adj Close", "Close"]:
        if col in df.columns: return df[col].dropna()
    return pd.Series(dtype="float64")

def main():
    # Pobieranie danych WYŁĄCZNIE z Secrets
    try:
        tickers_map = json.loads(os.getenv("GEM_TICKERS_JSON", "{}"))
        risk_assets = json.loads(os.getenv("GEM_RISK_ASSETS_JSON", "[]"))
        bonds_name = os.getenv("GEM_BONDS_NAME", "BONDS (VAGF)")
        capital_eur = os.getenv("GEM_CAPITAL_EUR", "583")
    except:
        OUT_FILE.write_text("🔴 Błąd: Sprawdź formatowanie JSON w Secrets!", encoding="utf-8")
        return

    details = {}
    for name, ticker in tickers_map.items():
        try:
            raw = yf.download(ticker, period="2y", progress=False)
            series = get_close_series(raw)
            try: me = series.resample("ME").last().dropna()
            except: me = series.resample("M").last().dropna()
            
            if len(me) < 13: continue
            details[name] = {"score": ((me.iloc[-2]/me.iloc[-13]-1) + (me.iloc[-1]/me.iloc[-7]-1))/2}
        except: continue

    ranked = sorted([(n, details[n]["score"]) for n in risk_assets if n in details], 
                    key=lambda x: x[1], reverse=True)
    
    if not ranked:
        OUT_FILE.write_text("🔴 Błąd: Nie udało się pobrać danych dla ETF-ów akcji.", encoding="utf-8")
        return

    top_name, top_score = ranked[0]
    b_score = details.get(bonds_name, {}).get("score")
    
    # Decyzja
    is_risk_on = b_score is not None and top_score > b_score
    choice = top_name if is_risk_on else bonds_name

    # Logika stanu
    with open(STATE_FILE, 'r') as f: state = json.load(f)
    today = date.today()
    curr_month = today.strftime("%Y-%m")
    active_label = state.get("active_label", "n/a")
    
    is_switch = extract_ticker_from_label(active_label) != extract_ticker_from_label(choice)
    action = "ZMIANA POZYCJI" if is_switch else "TRZYMAJ"
    
    if is_switch or state.get("last_rebalance_month") != curr_month:
        state.update({"active_label": choice, "last_rebalance_month": curr_month})
        with open(STATE_FILE, 'w') as f: json.dump(state, f, indent=2)

    # Budowanie alertu (Czytelna wersja)
    lines = [f"GEM SIGNAL - {today.isoformat()}", "", f"TOP: ✅ {top_name} ({fmt_pct(top_score)})"]
    if b_score is not None:
        lines.append(f"📈 Przewaga nad BONDS: {fmt_pp(top_score - b_score)}")
    lines.append(f"\nTRYB: {'RISK-ON ✅' if is_risk_on else 'RISK-OFF 🛡️'}")
    lines.append("\nRANKING:")
    for i, (n, s) in enumerate(ranked, 1): lines.append(f"{i}. {n}: {fmt_pct(s)}")
    lines.append(f"\nBONDS: {fmt_pct(b_score)}")
    lines.append(f"\nAKCJA: {action}\n{'KUP: ' + choice if is_switch else 'Pozostań w: ' + active_label}")
    lines.append(f"KWOTA: {capital_eur} EUR")
    
    OUT_FILE.write_text("\n".join(lines), encoding="utf-8")

if __name__ == "__main__":
    main()
