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

def fmt_pp(x: float) -> str:
    return f"{x * 100:+.2f} pp"

def extract_ticker_from_label(label: str) -> str:
    if not label: return ""
    m = re.search(r"\(([^)]+)\)", label)
    return m.group(1).strip().upper() if m else ""

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError): pass
    return {"active_label": "DM ex-US (EXUS)", "last_rebalance_month": None}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

def month_end_series(series: pd.Series) -> pd.Series:
    s = series.dropna()
    if s.empty: return s
    try: return s.resample("ME").last().dropna()
    except ValueError: return s.resample("M").last().dropna()

def get_close_series(df: pd.DataFrame) -> pd.Series:
    if df is None or df.empty: return pd.Series(dtype="float64")
    if isinstance(df.columns, pd.MultiIndex):
        level0 = df.columns.get_level_values(0)
        for col in ["Adj Close", "Close"]:
            if col in level0: return df.xs(col, level=0, axis=1).iloc[:, 0].dropna()
    for col in ["Adj Close", "Close"]:
        if col in df.columns: return df[col].dropna()
    return pd.Series(dtype="float64")

def main():
    # ---- Ładowanie Konfiguracji ----
    tickers_map = json.loads(os.getenv("GEM_TICKERS_JSON", "{}"))
    risk_assets = json.loads(os.getenv("GEM_RISK_ASSETS_JSON", "[]"))
    bonds_name = os.getenv("GEM_BONDS_NAME", "BONDS (VAGF)")
    capital_eur = os.getenv("GEM_CAPITAL_EUR", "583")

    details = {}
    for name, ticker in tickers_map.items():
        try:
            raw = yf.download(ticker, period="2y", interval="1d", progress=False)
            series = get_close_series(raw)
            me = month_end_series(series)
            if len(me) < 13: continue
            r12_1 = (me.iloc[-2] / me.iloc[-13]) - 1
            r6 = (me.iloc[-1] / me.iloc[-7]) - 1
            details[name] = {"score": (r12_1 + r6) / 2}
        except Exception as e: print(f"Error {ticker}: {e}")

    # ---- Ranking ----
    ranked = sorted([(n, details[n]["score"]) for n in risk_assets if n in details], 
                    key=lambda x: x[1], reverse=True)
    
    if not ranked:
        OUT_FILE.write_text("🔴 BŁĄD DANYCH", encoding="utf-8")
        return

    top_name, top_score = ranked[0]
    bonds_score = details.get(bonds_name, {"score": -1})["score"]
    
    # Decyzja GEM
    is_risk_on = top_score > bonds_score
    choice = top_name if is_risk_on else bonds_name

    # ---- Logika Stanu ----
    state = load_state()
    today = date.today()
    curr_month = f"{today.year}-{today.month:02d}"
    active_label = state.get("active_label", "DM ex-US (EXUS)")
    
    is_new_month = (state.get("last_rebalance_month") != curr_month)
    is_switch = (extract_ticker_from_label(active_label) != extract_ticker_from_label(choice))

    if is_new_month and is_switch:
        action_title, rebal = "ZMIANA POZYCJI", True
        status_note = f"WYKRYTO ZMIANĘ: {active_label} -> {choice}"
        state.update({"active_label": choice, "last_rebalance_month": curr_month})
        save_state(state)
    else:
        action_title, rebal = "TRZYMAJ", False
        status_note = "Utrzymujemy obecną pozycję"
        if is_new_month:
            state["last_rebalance_month"] = curr_month
            save_state(state)

    # ---- Budowanie Raportu PRO ----
    lines = [f"GEM SIGNAL - {today.strftime('%Y-%m-%d')}", ""]
    
    # Sekcja TOP PRO
    lines.append("TOP:")
    lines.append(f"✅ {top_name} — {fmt_pct(top_score)}")
    lines.append(f"📈 Przewaga nad BONDS: {fmt_pp(top_score - bonds_score)}")
    if len(ranked) > 1:
        lines.append(f"🥈 Przewaga nad #2: {fmt_pp(top_score - ranked[1][1])}")
    
    lines.append(f"\nTRYB: {'RISK-ON ✅' if is_risk_on else 'RISK-OFF 🛡️'}")
    
    lines.append("\nRANKING (Momentum Avg):")
    for i, (n, score) in enumerate(ranked, 1):
        lines.append(f"{i}) {n}: {fmt_pct(score)}")
    
    lines.append(f"\nBONDS ({bonds_name}): {fmt_pct(bonds_score)}")
    
    lines.append(f"\nAKCJA: {action_title}")
    lines.append(f"{'SPRZEDAJ: ' + active_label + ' -> KUP: ' + choice if rebal else 'Pozostań w: ' + active_label}")
    lines.append(f"KWOTA: {capital_eur} EUR")
    lines.append(f"Status bota: {status_note}")

    OUT_FILE.write_text("\n".join(lines), encoding="utf-8")

if __name__ == "__main__":
    main()
