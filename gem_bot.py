# GEM Bot – wersja produkcyjna (v2: ME/M kompatybilność)

import os
import re
import json
from pathlib import Path
from datetime import date, datetime

import pandas as pd
import yfinance as yf

STATE_FILE = Path("state.json")
OUT_FILE = Path("gem_message.txt")


def fmt_pct(x: float) -> str:
    return f"{x*100:.2f}%"


def extract_ticker_from_label(label: str) -> str:
    # Wyciąga ticker z nawiasu, np. 'USA (VUAA)' -> 'VUAA'. Jeśli brak nawiasu -> ''.
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
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def month_end_series(series: pd.Series) -> pd.Series:
    """Resample do końca miesiąca, kompatybilnie z pandas (ME vs M)."""
    s = series.dropna()
    if s.empty:
        return s
    s.index = pd.to_datetime(s.index, errors="coerce")
    s = s[~s.index.isna()]
    if s.empty:
        return s
    try:
        return s.resample("ME").last().dropna()
    except ValueError:
        return s.resample("M").last().dropna()


def calc_momentum(monthly: pd.Series) -> tuple[float, float]:
    if len(monthly) < 13:
        raise ValueError("Za mało danych (min. 13 miesięcy).")

    # 12-1: koniec miesiąca t-1 vs t-13
    r12_1 = (monthly.iloc[-2] / monthly.iloc[-13]) - 1.0

    # 6M: koniec miesiąca t vs t-6
    if len(monthly) < 7:
        raise ValueError("Za mało danych (min. 7 miesięcy) dla 6M.")
    r6 = (monthly.iloc[-1] / monthly.iloc[-7]) - 1.0

    return float(r12_1), float(r6)


def main():
    try:
        tickers = json.loads(os.environ.get("GEM_TICKERS_JSON", "{}"))
        risk_assets = json.loads(os.environ.get("GEM_RISK_ASSETS_JSON", "[]"))
        bonds_name = os.environ.get("GEM_BONDS_NAME", "BONDS (VAGF)")
        capital_eur = os.environ.get("GEM_CAPITAL_EUR", "0")

        if not tickers or not risk_assets:
            raise ValueError("Brak konfiguracji ENV: GEM_TICKERS_JSON i/lub GEM_RISK_ASSETS_JSON.")

        yf_symbols = list(set(tickers.values()))
        data = yf.download(
            yf_symbols,
            period="3y",
            interval="1d",
            auto_adjust=False,
            progress=False,
            group_by='column'
        )

        def get_adj(symbol: str) -> pd.Series:
            if isinstance(data.columns, pd.MultiIndex):
                return data[("Adj Close", symbol)]
            return data["Adj Close"]

        details = {}
        for label, sym in tickers.items():
            adj = get_adj(sym)
            monthly = month_end_series(adj)
            r12_1, r6 = calc_momentum(monthly)
            score = (r12_1 + r6) / 2.0
            details[label] = {"sym": sym, "r12_1": r12_1, "r6": r6, "score": score}

        ranked = sorted(
            [(name, details[name]["score"]) for name in risk_assets],
            key=lambda x: x[1],
            reverse=True
        )

        choice, top_score = ranked[0]

        # ====== SEKCJA STANU (state.json) ======
        state = load_state()
        today = date.today()
        current_month = f"{today.year}-{today.month:02d}"

        current_active_label = state.get("active_label", "DM ex-US (EXUS)")
        current_ticker = extract_ticker_from_label(current_active_label)
        new_ticker = extract_ticker_from_label(choice)

        if not new_ticker:
            raise ValueError(f"Błąd: Etykieta '{choice}' nie zawiera tickera w nawiasach!")

        is_new_month = (state.get("last_rebalance_month") != current_month)
        is_different_asset = (current_ticker != new_ticker)

        if is_new_month and is_different_asset:
            rebalance_needed = True
            action_title = "ZMIANA POZYCJI"
            status_note = f"WYKRYTO ZMIANĘ: {current_active_label} -> {choice}"
            state["active_label"] = choice
            state["last_rebalance_month"] = current_month
            save_state(state)
        else:
            rebalance_needed = False
            action_title = "TRZYMAJ"
            status_note = "Utrzymujemy obecną pozycję"
            if is_new_month:
                state["last_rebalance_month"] = current_month
                save_state(state)
        # ====== /SEKCJA STANU ======

        lines = [
            "GEM SIGNAL (Classic 12-1 + 6M)",
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "",
            "RANKING:",
        ]

        for i, (name, _) in enumerate(ranked, 1):
            d = details[name]
            lines.append(f"{i}. {name}: {fmt_pct(d['score'])} (12-1: {fmt_pct(d['r12_1'])}, 6M: {fmt_pct(d['r6'])})")

        if bonds_name in details:
            lines.append(f"
BONDS: {fmt_pct(details[bonds_name]['score'])}")

        lines.append(f"
AKCJA: {action_title}")
        if rebalance_needed:
            lines.append(f"SPRZEDAJ: {current_active_label} -> KUP: {choice}")
        else:
            lines.append(f"Pozostań w: {current_active_label}")

        lines.append(f"KWOTA: {capital_eur} EUR")
        lines.append(f"
Status bota: {status_note}")
        lines.append(f"Reason: RISK-ON: wygrywa {choice} ({fmt_pct(top_score)})")

        OUT_FILE.write_text("
".join(lines), encoding="utf-8")

    except Exception as e:
        OUT_FILE.write_text(
            "GEM Bot ERROR
" +
            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M')}

" +
            f"{type(e).__name__}: {e}
",
            encoding="utf-8"
        )
        raise


if __name__ == "__main__":
    main()
