import os
import json
import pandas as pd
import yfinance as yf
from pathlib import Path
from datetime import date

STATE_FILE = Path("state.json")
OUT_FILE = Path("gem_message.txt")

# ----------------------------
# Helpers (formatting / safety)
# ----------------------------

def clean_ticker(t: str) -> str:
    """Usuwa zbędne spacje i kropki z końca tickera."""
    if not isinstance(t, str):
        return ""
    return t.strip().rstrip(".")

def fmt_pct(x):
    """Format procentowy z wartości ułamkowej (0.12 -> 12.00%)."""
    return f"{x * 100:.2f}%" if x is not None else "Brak danych"

def get_close_series(df: pd.DataFrame) -> pd.Series:
    """Wyciąga serię cen (Adj Close/Close) z DataFrame, także gdy MultiIndex."""
    if df is None or df.empty:
        return pd.Series(dtype="float64")

    if isinstance(df.columns, pd.MultiIndex):
        level0 = df.columns.get_level_values(0)
        for col in ["Adj Close", "Close"]:
            if col in level0:
                s = df.xs(col, level=0, axis=1).iloc[:, 0]
                return s.dropna()

    for col in ["Adj Close", "Close"]:
        if col in df.columns:
            return df[col].dropna()
    return pd.Series(dtype="float64")

def emoji_bar(value, vmin, vmax, width=10, full="🟩", empty="⬜"):
    """Prosty pasek 'wykres' z emoji."""
    if value is None:
        return "❔" * width
    if vmax <= vmin:
        return full * width
    x = (value - vmin) / (vmax - vmin)
    x = max(0.0, min(1.0, x))
    filled = int(round(x * width))
    return full * filled + empty * (width - filled)

def edge_badge(pp):
    """Kolorowa etykieta przewagi w pp (konserwatywne progi)."""
    if pp is None:
        return "⚪ n/a"
    ppv = pp * 100.0
    if ppv < 0:
        emo = "🔴"
    elif ppv < 2:
        emo = "🟨"
    elif ppv < 5:
        emo = "🟩"
    elif ppv < 8:
        emo = "🟢"
    else:
        emo = "🔥🚀"
    return f"{emo} {ppv:+.2f} pp"

def score_badge(score):
    """Kolorowy badge dla wyniku momentum (%)."""
    if score is None:
        return "⚪ n/a"
    pct = score * 100.0
    if pct < 0:
        emo = "🔴"
    elif pct < 5:
        emo = "🟨"
    elif pct < 12:
        emo = "🟩"
    elif pct < 20:
        emo = "🟢"
    else:
        emo = "🔥🚀"
    return f"{emo} {pct:.2f}%"

def signal_strength(is_risk_on: bool, top_score, edge_vs_bonds_or_cash, edge_vs_2):
    """💥 SIŁA SYGNAŁU (konserwatywnie)."""
    if not is_risk_on:
        return "🛡️ OBRONNY (RISK-OFF)"
    
    top_pct = (top_score or 0.0) * 100.0
    eb_pp = (edge_vs_bonds_or_cash or 0.0) * 100.0
    e2_pp = (edge_vs_2 * 100.0) if edge_vs_2 is not None else None

    if e2_pp is None:
        if top_pct >= 20 and eb_pp >= 8: return "🔥🚀 BARDZO MOCNY (brak #2)"
        if top_pct >= 12 and eb_pp >= 5: return "🟢 MOCNY (brak #2)"
        if top_pct >= 5 and eb_pp >= 2: return "🟩 UMIARKOWANY (brak #2)"
        return "🟨 SŁABY (brak #2)"

    if top_pct >= 20 and eb_pp >= 8 and e2_pp >= 5: return "🔥🚀 BARDZO MOCNY"
    if eb_pp >= 5 and e2_pp >= 3: return "🟢 MOCNY"
    if eb_pp >= 2 and e2_pp >= 2: return "🟩 UMIARKOWANY"
    return "🟨 SŁABY"

def pick_anchor_index(me: pd.Series, today: date) -> int:
    """Wybiera punkt odniesienia: -2 jeśli bieżący miesiąc jest niepełny."""
    if me is None or me.empty:
        return -1
    last_period = me.index[-1].to_period("M")
    curr_period = pd.Timestamp(today).to_period("M")
    
    if last_period == curr_period and me.index[-1].date() == today:
        return -1
        
    return -2 if last_period == curr_period else -1

# ----------------------------
# Main logic
# ----------------------------

def main():
    # 1) Secrets / config
    try:
        tickers_map = json.loads(os.getenv("GEM_TICKERS_JSON") or "{}")
        risk_assets = json.loads(os.getenv("GEM_RISK_ASSETS_JSON") or "[]")
        b_name = (os.getenv("GEM_BONDS_NAME") or "").strip()
        cap = os.getenv("GEM_CAPITAL_EUR") or "0"
    except Exception as e:
        OUT_FILE.write_text(f"🔴 GEM Bot Error: Problem z Secrets ({e})", encoding="utf-8")
        return

    # Walidacja typów danych
    if not isinstance(tickers_map, dict) or not isinstance(risk_assets, list) or not tickers_map or not risk_assets:
        OUT_FILE.write_text("🔴 GEM Bot Error: Brak konfiguracji lub zły format danych w Secrets.", encoding="utf-8")
        return

    # 2) Download + compute momentum
    details = {}
    today = date.today()

    for name, t_raw in tickers_map.items():
        ticker = clean_ticker(t_raw)
        if not ticker: continue
        try:
            raw = yf.download(ticker, period="2y", progress=False)
            series = get_close_series(raw)
            if series.empty: continue
            
            try:
                me = series.resample("ME").last().dropna()
            except Exception:
                me = series.resample("M").last().dropna()

            anchor_idx = pick_anchor_index(me, today)
            min_needed = 13 if anchor_idx == -1 else 14
            
            if len(me) < min_needed: continue

            anchor = me.iloc[anchor_idx]
            score_12m = (anchor / me.iloc[anchor_idx - 12]) - 1.0
            score_6m = (anchor / me.iloc[anchor_idx - 6]) - 1.0
            score = (score_12m + score_6m) / 2.0
            
            details[name] = {"score": float(score), "ticker": ticker, "anchor_idx": anchor_idx}
        except Exception:
            continue

    # 3) Ranking
    ranked = sorted(
        [(n, details[n]["score"]) for n in risk_assets if n in details],
        key=lambda x: x[1], reverse=True
    )
    if not ranked:
        OUT_FILE.write_text("🔴 GEM Bot Error: Brak danych w Yahoo Finance!", encoding="utf-8")
        return

    top_name, top_score = ranked[0]
    anchor_idx = details[top_name]["anchor_idx"]

    # 4) Bonds / Risk-on check
    b_score = details.get(b_name, {}).get("score") if b_name else None
    safe_b_score = b_score if b_score is not None else 0.0
    is_risk_on = top_score > safe_b_score
    choice = top_name if is_risk_on else (b_name if b_name else top_name)

    # 5) State handling
    curr_mo = pd.Timestamp(today).strftime("%Y-%m")
    if not STATE_FILE.exists():
        initial_state = {"active_label": choice, "last_rebalance_month": ""}
        STATE_FILE.write_text(json.dumps(initial_state, indent=2), encoding="utf-8")

    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        state = {"active_label": choice, "last_rebalance_month": ""}
        
    old_label = state.get("active_label", choice)
    is_switch = (old_label != choice)

    if is_switch or state.get("last_rebalance_month") != curr_mo:
        state.update({"active_label": choice, "last_rebalance_month": curr_mo})
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")

    # 6) Calculations for report
    top_vs_2 = (top_score - ranked[1][1]) if len(ranked) > 1 else None
    top_vs_b_or_cash = top_score - safe_b_score
    top_vs_b = (top_score - b_score) if b_score is not None else None
    strength_txt = signal_strength(is_risk_on, top_score, top_vs_b_or_cash, top_vs_2)

    # 7) Action text
    choice_ticker = details.get(choice, {}).get("ticker") or clean_ticker(tickers_map.get(choice, ""))
    if is_switch:
        action_txt = "🔁 ZMIANA POZYCJI"
        action_lines = [f"💸 SPRZEDAJ: {old_label}", f"🛒 KUP: {choice} ({choice_ticker})"]
    else:
        action_txt = "🟦 TRZYMAJ POZYCJĘ" if is_risk_on else "🛡️ TRYB OBRONNY (BEZ ZMIAN)"
        action_lines = [f"📌 POZOSTAŃ W: {old_label}"]

    # 8) Build Message
    vmin = min(s for _, s in ranked)
    vmax = max(s for _, s in ranked)
    
    lines = [
        f"📌 GEM SIGNAL — {today.isoformat()}",
        "",
        f"🏆 TOP: ✅ {top_name} — {score_badge(top_score)}",
        f"🥈 Przewaga nad #2: {edge_badge(top_vs_2)}",
        f"🛡️ Vs BONDS: {edge_badge(top_vs_b) if b_score is not None else '⚪ n/a (fallback: cash=0)'}",
        "",
        f"💥 SIŁA SYGNAŁU: {strength_txt}",
        f"🚦 TRYB: {'RISK-ON ✅' if is_risk_on else 'RISK-OFF 🛡️'}",
        f"🎯 AKCJA: {action_txt}"
    ]
    lines.extend(action_lines)
    lines.append(f"💶 KWOTA: {cap} EUR")
    lines.append("")
    lines.append("📊 RANKING (momentum):")
    for i, (n, s) in enumerate(ranked, 1):
        bar = emoji_bar(s, vmin, vmax, width=10)
        lines.append(f"{i}) {n:<20} {score_badge(s):>10} {bar}")
    
    lines.append("")
    if b_name:
        lines.append(f"🧾 BONDS ({b_name}): {fmt_pct(b_score)}")
    else:
        lines.append("🧾 BONDS: (nie ustawiono GEM_BONDS_NAME)")
        
    lines.append(f"🕒 Rebalance: {curr_mo} (1× / miesiąc)")
    
    anchor_desc = "Poprzedni miesiąc (iloc=-2)" if anchor_idx == -2 else "Bieżący miesiąc (iloc=-1)"
    lines.append(f"📅 Anchor: {anchor_desc}")

    OUT_FILE.write_text("\n".join(lines), encoding="utf-8")

if __name__ == "__main__":
    main()
