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
    return m.group(1).strip().upper() if m else label.strip().upper()

def get_close_series(df):
    if df is None or df.empty: return pd.Series(dtype="float64")
    if isinstance(df.columns, pd.MultiIndex):
        level0 = df.columns.get_level_values(0)
        for col in ["Adj Close", "Close"]:
            if col in level0: return df.xs(col, level=0, axis=1).iloc[:, 0].dropna()
    for col in ["Adj Close", "Close"]:
        if col in df.columns: return df[col].dropna()
    return pd.Series(dtype="float64")

def main():
    # Ładowanie Danych z Secrets
    try:
        tickers_map = json.loads(os.getenv("GEM_TICKERS_JSON", "{}"))
        risk_assets = json.loads(os.getenv("GEM_RISK_ASSETS_JSON", "[]"))
        b_name = os.getenv("GEM_BONDS_NAME", "")
        cap = os.getenv("GEM_CAPITAL_EUR", "0")
    except Exception as e:
        OUT_FILE.write_text(f"🔴 Błąd Secrets: {e}", encoding="utf-8")
        return

    # Pobieranie Cen
    details = {}
    for name, ticker in tickers_map.items():
        try:
            raw = yf.download(ticker, period="2y", progress=False)
            series = get_close_series(raw)
            if series.empty: continue
            try: me = series.resample("ME").last().dropna()
            except: me = series.resample("M").last().dropna()
            
            if len(me) < 13: continue
            # Momentum: średnia z 12-1 mies. i ostatnich 6 mies.
            score = ((me.iloc[-2]/me.iloc[-13]-1) + (me.iloc[-1]/me.iloc[-7]-1))/2
            details[name] = {"score": score}
        except: continue

    # Ranking Akcji
    ranked = sorted([(n, details[n]["score"]) for n in risk_assets if n in details], 
                    key=lambda x: x[1], reverse=True)
    
    if not ranked:
        OUT_FILE.write_text("🔴 Błąd: Brak danych dla aktywów z GEM_RISK_ASSETS_JSON.", encoding="utf-8")
        return

    top_name, top_score = ranked[0]
    b_score = details.get(b_name, {}).get("score")
    
    # Decyzja GEM
    is_risk_on = b_score is not None and top_score > b_score
    choice = top_name if is_risk_on else b_name

    # Obsługa Stanu (state.json)
    if not STATE_FILE.exists():
        STATE_FILE.write_text(json.dumps({"active_label": choice, "last_rebalance_month": ""}))
    
    with open(STATE_FILE, 'r') as f: state = json.load(f)
    today = date.today()
    curr_mo = today.strftime("%Y-%m")
    old_label = state.get("active_label", choice)
    
    is_switch = extract_ticker_from_label(old_label) != extract_ticker_from_label(choice)
    
    # Zapisz nowy stan tylko jeśli to nowy miesiąc lub nastąpiła zmiana
    if is_switch or state.get("last_rebalance_month") != curr_mo:
        state.update({"active_label": choice, "last_rebalance_month": curr_mo})
        with open(STATE_FILE, 'w') as f: json.dump(state, f, indent=2)

    # Budowanie wiadomości
    lines = [f"GEM SIGNAL - {today.isoformat()}", "", f"TOP: ✅ {top_name} ({fmt_pct(top_score)})"]
    if b_score is not None:
        lines.append(f"📈 Przewaga nad BONDS: {fmt_pp(top_score - b_score)}")
    if len(ranked) > 1:
        lines.append(f"🥈 Przewaga nad #2: {fmt_pp(top_score - ranked[1][1])}")
        
    lines.append(f"\nTRYB: {'RISK-ON ✅' if is_risk_on else 'RISK-OFF 🛡️'}")
    lines.append("\nRANKING:")
    for i, (n, s) in enumerate(ranked, 1): lines.append(f"{i}. {n}: {fmt_pct(s)}")
    lines.append(f"\nBONDS ({b_name}): {fmt_pct(b_score)}")
    
    lines.append(f"\nAKCJA: {'ZMIANA POZYCJI' if is_switch else 'TRZYMAJ'}")
    lines.append(f"{'KUP: ' + choice if is_switch else 'Pozostań w: ' + old_label}")
    lines.append(f"KWOTA: {cap} EUR")
    
    OUT_FILE.write_text("\n".join(lines), encoding="utf-8")

if __name__ == "__main__":
    main()
