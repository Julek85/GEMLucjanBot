# GEM Bot – wersja produkcyjna v3.2 (Naprawiony RISK-OFF + Opcja A + %)

import os
import re
import json
import time
from pathlib import Path
from datetime import date, datetime
import sys

# Wymuszenie UTF-8, żeby logi i Telegram przyjmowały emoji bez błędu
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
        return s.resample("ME").last().dropna()
    except ValueError:
        return s.resample("M").last().dropna()

def calc_momentum(monthly: pd.Series) -> tuple[float, float]:
    if len(monthly) < 13:
        raise ValueError("Za mało danych (min. 13 miesięcy).")
    r12_1 = (monthly.iloc[-2] / monthly.iloc[-13]) - 1.0
    if len(monthly) < 7:
        raise ValueError("Za mało danych (min. 7 miesięcy) dla 6M.")
    r6 = (monthly.iloc[-1] / monthly.iloc[-7]) - 1.0
    return float(r12_1), float(r6)

def main():
    try:
        tickers = json.loads(os.environ.get("GEM_TICKERS_JSON", "{}"))
        risk_assets = json.loads(os.environ.get("GEM_RISK_ASSETS_JSON", "[]"))
        bonds_name = os.environ.get("GEM_BONDS_NAME", "BONDS (AGGH)")
        capital_eur = os.environ.get("GEM_CAPITAL_EUR", "0")

        if not tickers or not risk_assets:
            raise ValueError("Brak konfiguracji ENV: GEM_TICKERS_JSON i/lub GEM_RISK_ASSETS_JSON.")

        yf_symbols = list(set(tickers.values()))
        data = yf_download_with_retries(yf_symbols, period="3y", interval="1d", attempts=3, sleep_sec=5, timeout=20)

        def get_adj(symbol: str) -> pd.Series:
            if isinstance(data.columns, pd.MultiIndex):
                if ("Adj Close", symbol) in data.columns:
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
                details[label] = {"sym": sym, "r12_1": r12_1, "r6": r6, "score": score, "last_price": last_price}
            except Exception as e:
                print(f"Pominięto {label} ({sym}): {e}")

        ranked = sorted(
            [(name, details[name]["score"]) for name in risk_assets if name in details],
            key=lambda x: x[1],
            reverse=True
        )

        if not ranked:
            raise ValueError("Brak wystarczających danych do wyliczenia rankingu!")

        # ---------------------------------------------------------
        # NAPRAWIONA LOGIKA GEM (Porównanie z Obligacjami)
        # ---------------------------------------------------------
        top_name, top_score = ranked[0]
        bonds_score = details.get(bonds_name, {}).get("score", 0.0)

        is_risk_on = top_score > bonds_score
        if is_risk_on:
            choice = top_name
            mode_str = "RISK-ON ✅"
        else:
            choice = bonds_name if bonds_name in details else top_name
            mode_str = "RISK-OFF 🛡️"

        # ====== STAN + OPCJA A ======
        state = load_state()
        today = date.today()
        current_month = f"{today.year}-{today.month:02d}"

        # Używamy domyślnego choice, jeśli to pierwszy start bota
        current_active_label = state.get("active_label", choice)
        previous_active_label = current_active_label

        current_ticker = extract_ticker_from_label(current_active_label)
        new_ticker = extract_ticker_from_label(choice)

        is_new_month = (state.get("last_rebalance_month") != current_month)
        is_different_asset = (current_ticker != new_ticker)

        # Inicjalizacja ceny wejściowej, jeśli brakuje
        if state.get("entry_price") is None and current_active_label in details:
            state["entry_price"] = float(details[current_active_label]["last_price"])
            state["active_since"] = state.get("active_since") or current_month

        if is_new_month and is_different_asset:
            rebalance_needed = True
            action_title = "🔁 ZMIANA POZYCJI"
            status_note = f"WYKRYTO ZMIANĘ: {current_active_label} -> {choice}"

            state["active_label"] = choice
            state["last_rebalance_month"] = current_month
            state["active_since"] = current_month
            state["entry_price"] = float(details[choice]["last_price"])
            save_state(state)
            current_active_label = choice
        else:
            rebalance_needed = False
            action_title = "🟦 TRZYMAJ"
            status_note = "Utrzymujemy obecną pozycję"
            if is_new_month:
                state["last_rebalance_month"] = current_month
                save_state(state)

        # Opcja A – wartość i % od wejścia
        est_value = None
        pct_gain = None
        if state.get("entry_price") and current_active_label in details:
            current_price = float(details[current_active_label]["last_price"])
            est_value = float(capital_eur) * (current_price / float(state["entry_price"]))
            pct_gain = (current_price / float(state["entry_price"]) - 1.0) * 100.0

        # ====== RAPORT ======
        lines = [
            f"📌 GEM SIGNAL — {datetime.now().strftime('%Y-%m-%d')}",
            "",
            f"🏆 TOP: ✅ {top_name} — {fmt_pct(top_score)}",
            f"🛡️ Vs BONDS: {fmt_pct(bonds_score) if bonds_name in details else 'Brak danych'}",
            f"🚦 TRYB: {mode_str}",
            "",
            "📊 RANKING (momentum):",
        ]

        for i, (name, score) in enumerate(ranked, 1):
            lines.append(f"{i}) {name:<18} {fmt_pct(score)}")

        if bonds_name in details:
            lines.append(f"\n🧾 BONDS ({bonds_name}): {fmt_pct(details[bonds_name]['score'])}")

        lines.append(f"\n🎯 AKCJA: {action_title}")
        if rebalance_needed:
            lines.append(f"💸 SPRZEDAJ: {previous_active_label}")
            lines.append(f"🛒 KUP: {choice}")
        else:
            lines.append(f"📌 POZOSTAŃ W: {current_active_label}")

        lines.append(f"\n💶 KWOTA STARTOWA: {capital_eur} EUR")
        if est_value is not None:
            if pct_gain is not None:
                lines.append(f"📈 WARTOŚĆ: {est_value:.2f} EUR ({pct_gain:+.2f}%)")
            else:
                lines.append(f"📈 WARTOŚĆ: {est_value:.2f} EUR")
            lines.append(f"🕒 W POZYCJI OD: {state.get('active_since')}")

        lines.append(f"\nStatus bota: {status_note}")
        lines.append(f"Rebalance: {state.get('last_rebalance_month')}")

        OUT_FILE.write_text("\n".join(lines), encoding="utf-8")

    except Exception as e:
        err_msg = f"🔴 GEM Bot ERROR\nTime: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n{type(e).__name__}: {e}"
        OUT_FILE.write_text(err_msg, encoding="utf-8")
        raise

if __name__ == "__main__":
    main()
