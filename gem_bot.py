import os
import json
import time
import pandas as pd
import yfinance as yf
from pathlib import Path
from datetime import date
import sys

# Wymuszenie UTF-8 dla poprawnych znaków w logach i wiadomościach
if sys.stdout.encoding != 'utf-8':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

STATE_FILE = Path("state.json")
OUT_FILE = Path("gem_message.txt")

def clean_ticker(t: str) -> str:
    if not isinstance(t, str): return ""
    return t.strip().rstrip(".")

def normalize_yf_ticker(t: str) -> str:
    t = clean_ticker(t)
    if not t: return ""
    if "." in t or ":" in t: return t
    if t.upper() == "AGGH": return "AGGH.L"
    return t

def get_close_series(df: pd.DataFrame) -> pd.Series:
    if df is None or df.empty: return pd.Series(dtype="float64")
    if isinstance(df.columns, pd.MultiIndex):
        for col in ["Adj Close", "Close"]:
            if col in df.columns.get_level_values(0):
                return df.xs(col, level=0, axis=1).iloc[:, 0].dropna()
    for col in ["Adj Close", "Close"]:
        if col in df.columns: return df[col].dropna()
    return pd.Series(dtype="float64")

def yf_download_safe(ticker: str):
    t = normalize_yf_ticker(ticker)
    last_err = None
    for attempt in range(1, 4):
        try:
            # Pobieranie z timeoutem i bez wątków dla stabilności w Actions
            df = yf.download(t, period="2y", progress=False, threads=False, timeout=20)
            s = get_close_series(df)
            if not s.empty: return s
        except Exception as e:
            last_err = e
            time.sleep(5 * attempt)
    raise RuntimeError(f"Błąd pobierania {t}: {last_err}")

def pick_anchor_index(me, today_):
    if me is None or me.empty: return -1
    last_p = me.index[-1].to_period("M")
    curr_p = pd.Timestamp(today_).to_period("M")
    if last_p == curr_p and me.index[-1].date() == today_: return -1
    return -2 if last_p == curr_p else -1

def score_badge(score):
    if score is None: return "⚪ n/a"
    pct = score * 100.0
    emo = "🔴" if pct < 0 else ("🟨" if pct < 5 else ("🟩" if pct < 12 else "🟢"))
    if pct >= 20: emo = "🔥🚀"
    return f"{emo} {pct:.2f}%"

def edge_badge(pp):
    if pp is None: return "⚪ n/a"
    ppv = pp * 100.0
    emo = "🔴" if ppv < 0 else ("🟨" if ppv < 2 else ("🟩" if ppv < 5 else "🟢"))
    return f"{emo} {ppv:+.2f} pp"

def main():
    try:
        tickers_map = json.loads(os.getenv("GEM_TICKERS_JSON") or "{}")
        risk_assets = json.loads(os.getenv("GEM_RISK_ASSETS_JSON") or "[]")
        b_name_env = (os.getenv("GEM_BONDS_NAME") or "").strip()
        cap = os.getenv("GEM_CAPITAL_EUR") or "0"
    except Exception as e:
        OUT_FILE.write_text(f"🔴 Błąd Secrets: {e}", encoding="utf-8")
        return

    b_name = b_name_env if b_name_env in tickers_map else next((k for k in tickers_map if "BONDS" in k.upper()), b_name_env)
    details, errors, today = {}, [], date.today()

    for name, t_raw in tickers_map.items():
        try:
            series = yf_download_safe(t_raw)
            try: me = series.resample("ME").last().dropna()
            except: me = series.resample("M").last().dropna()
            
            idx = pick_anchor_index(me, today)
            if len(me) < (13 if idx == -1 else 14): continue
            
            val = me.iloc[idx]
            score = ((val / me.iloc[idx-12]) + (val / me.iloc[idx-6])) / 2.0 - 1.0
            details[name] = {"score": float(score), "ticker": normalize_yf_ticker(t_raw), "idx": idx}
        except Exception as e:
            errors.append(f"{name}: {e}")

    ranked = sorted([(n, details[n]["score"]) for n in risk_assets if n in details], key=lambda x: x[1], reverse=True)
    
    if not ranked:
        OUT_FILE.write_text(f"🔴 Brak danych rynkowych!\nDebug: {errors[:3]}", encoding="utf-8")
        return

    top_n, top_s = ranked[0]
    b_s = details.get(b_name, {}).get("score", 0.0)
    is_risk_on = top_s > b_s
    choice = top_n if is_risk_on else (b_name if b_name in details else top_n)
    
    try: state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except: state = {"active_label": choice, "last_rebalance_month": ""}
    
    old_label = state.get("active_label", choice)
    curr_mo = pd.Timestamp(today).strftime("%Y-%m")
    is_switch = (old_label != choice)
    
    if is_switch or state.get("last_rebalance_month") != curr_mo:
        state.update({"active_label": choice, "last_rebalance_month": curr_mo})
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")

    lines = [
        f"📌 GEM SIGNAL — {today.isoformat()}", "",
        f"🏆 TOP: ✅ {top_n} — {score_badge(top_s)}",
        f"🥈 Przewaga nad #2: {edge_badge(top_s - ranked[1][1] if len(ranked)>1 else None)}",
        f"🛡️ Vs BONDS: {edge_badge(top_s - b_s)}", "",
        f"🚦 TRYB: {'RISK-ON ✅' if is_risk_on else 'RISK-OFF 🛡️'}",
        f"🎯 AKCJA: {'🔁 ZMIANA' if is_switch else '🟦 TRZYMAJ'}",
        f"💸 SPRZEDAJ: {old_label}" if is_switch else f"📌 POZOSTAŃ W: {old_label}",
        f"🛒 KUP: {choice} ({details[choice]['ticker']})" if is_switch else "",
        f"💶 KWOTA: {cap} EUR", "", "📊 RANKING:"
    ]
    for i, (n, s) in enumerate(ranked, 1):
        lines.append(f"{i}) {n:<12} {score_badge(s)}")

    lines.append(f"\n🕒 Rebalance: {curr_mo}")
    OUT_FILE.write_text("\n".join(filter(None, lines)), encoding="utf-8")

if __name__ == "__main__":
    main()
