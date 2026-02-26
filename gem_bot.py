import os
import sys
import math
import json
import time
import logging
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

# ---------------------------
# Konfiguracja Logowania
# ---------------------------
logger = logging.getLogger("gem")
_log_level = os.environ.get("GEM_LOG_LEVEL", "INFO").upper().strip()
logging.basicConfig(
    level=getattr(logging, _log_level, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)

def month_end_series(series: pd.Series) -> pd.Series:
    """Konwertuje dane dzienne na serie z końca miesiąca (pandas >= 2.2)."""
    s = series.dropna()
    if s.empty:
        return s
    s.index = pd.to_datetime(s.index)
    return s.resample("ME").last().dropna()

def total_return(monthly_prices: pd.Series, months: int, skip_last: int = 0) -> float:
    """Oblicza całkowitą stopę zwrotu; skip_last=1 implementuje momentum 12-1."""
    needed = months + 1 + skip_last
    if len(monthly_prices) < needed:
        return float("nan")
    end = monthly_prices.iloc[-1 - skip_last]
    start = monthly_prices.iloc[-1 - skip_last - months]
    return (end / start) - 1.0

def fmt_pct(x: float) -> str:
    """Formatowanie liczb jako procenty."""
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "n/a"
    return f"{x*100:.2f}%"

def load_env_json(name: str, default):
    """Ładuje konfigurację JSON z zmiennych środowiskowych."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"Błąd: {name} nie jest poprawnym formatem JSON: {e}")
        sys.exit(2)

def extract_price_series(data: pd.DataFrame, preferred: str, ticker: str, name: str = "") -> pd.Series:
    """Wyciąga serie cenową, obsługując błędy i MultiIndex z yfinance."""
    if data is None or data.empty:
        raise ValueError("Pusta ramka danych (dataframe)")

    used_key = None
    if isinstance(data.columns, pd.MultiIndex):
        level0 = data.columns.get_level_values(0)
        if preferred in level0:
            used_key = preferred
        elif "Close" in level0:
            used_key = "Close"
        else:
            used_key = level0[0]
        tmp = data[used_key]
        s = tmp[ticker] if ticker in tmp.columns else tmp.iloc[:, 0]
    else:
        if preferred in data.columns:
            used_key = preferred
            s = data[preferred]
        elif "Close" in data.columns:
            used_key = "Close"
            s = data["Close"]
        else:
            used_key = data.columns[0]
            s = data.iloc[:, 0]

    if isinstance(s, pd.DataFrame):
        s = s.iloc[:, 0]
    if not isinstance(s, pd.Series):
        s = pd.Series(s)

    display = f"{name} ({ticker})" if name else ticker
    if used_key != preferred:
        logger.warning(f"[{display}] '{preferred}' niedostępne -> używam '{used_key}' (brak korekty dywidend!).")
    
    return s

def download_with_retry(ticker, start, end, tries=3, base_sleep=2.0, per_ticker_sleep=1.2):
    """Pobiera dane z Yahoo Finance z systemem ponowień w razie błędu."""
    if per_ticker_sleep > 0:
        time.sleep(per_ticker_sleep)
    
    for i in range(tries):
        try:
            df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=False)
            if df is None or df.empty:
                raise ValueError("Brak danych (empty dataframe)")
            return df
        except Exception as e:
            sleep_s = base_sleep * (2 ** i)
            logger.warning(f"[{ticker}] Błąd pobierania: {e} | Próba {i+1}/{tries} za {sleep_s:.1f}s")
            time.sleep(sleep_s)
    raise RuntimeError(f"[{ticker}] Nie udało się pobrać danych po {tries} próbach.")

def main():
    # 1. Ładowanie Konfiguracji
    tickers = load_env_json("GEM_TICKERS_JSON", {})
    risk_assets = load_env_json("GEM_RISK_ASSETS_JSON", [])
    bonds_name = os.environ.get("GEM_BONDS_NAME", "BONDS")
    riskoff_threshold = float(os.environ.get("GEM_RISK_OFF_THRESHOLD", "0"))
    switch_threshold = float(os.environ.get("GEM_SWITCH_THRESHOLD", "0"))
    capital_eur = os.environ.get("GEM_CAPITAL_EUR", "0")
    current_holding = os.environ.get("GEM_CURRENT_HOLDING", "").strip()

    if not tickers or not risk_assets:
        logger.error("Błąd: GEM_TICKERS_JSON lub GEM_RISK_ASSETS_JSON jest pusty.")
        sys.exit(2)

    # 2. Pobieranie Danych
    start = "2000-01-01"
    end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    monthly = {}
    
    for name, ticker in tickers.items():
        data = download_with_retry(ticker, start, end)
        series = extract_price_series(data, "Adj Close", ticker, name)
        me = month_end_series(series)
        if me.empty:
            logger.error(f"Brak danych miesięcznych dla {name}")
            sys.exit(2)
        monthly[name] = me

    # 3. Obliczenia Momentum
    details = {}
    for name, series in monthly.items():
        r12_1 = total_return(series, 12, skip_last=1)
        r6 = total_return(series, 6, skip_last=0)
        score = 0.5 * r12_1 + 0.5 * r6
        details[name] = {"r12_1": r12_1, "r6": r6, "score": score}

    # 4. Ranking i Decyzja
    ranked = sorted([(n, details[n]["score"]) for n in risk_assets], key=lambda x: x[1], reverse=True)
    top_name, top_score = ranked[0]

    if top_score <= riskoff_threshold:
        choice = bonds_name
        reason = f"RISK-OFF: najlepszy {top_name} ({fmt_pct(top_score)}) <= progu {fmt_pct(riskoff_threshold)}"
    else:
        choice = top_name
        reason = f"RISK-ON: wygrywa {top_name} ({fmt_pct(top_score)})"

    # 5. Logika Switch Threshold (Anti-noise)
    if current_holding and choice != current_holding and switch_threshold > 0:
        current_score = details.get(current_holding, {}).get("score", float("-inf"))
        edge = top_score - current_score
        if edge < switch_threshold:
            logger.info(f"Zmiana zablokowana przez próg szumu: przewaga {fmt_pct(edge)} < {fmt_pct(switch_threshold)}")
            choice = current_holding
            reason += f" | BLOKADA ZMIANY (przewaga {fmt_pct(edge)} zbyt mała)"

    # 6. Przygotowanie Komunikatu
    now_local = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    def short_name(x):
        return x.split(" (")[0] # Skraca nazwy dla czytelności

    if current_holding == choice:
        action_title = "TRZYMAJ (BEZ ZMIAN)"
        trade_instr = f"Masz już: {short_name(choice)}. Nic nie robisz w Trading 212."
    else:
        action_title = "ZMIEŃ POZYCJĘ"
        trade_instr = f"SPRZEDAJ: {short_name(current_holding)} -> KUP: {short_name(choice)} (Kwota: {capital_eur} EUR)"

    msg = f"""GEM SIGNAL (Classic 12-1 + 6M)
Time: {now_local}

RANKING (risk assets):
"""
    for i, (n, _) in enumerate(ranked, start=1):
        d = details[n]
        msg += f"{i}. {short_name(n)} | Score: {fmt_pct(d['score'])} (12-1: {fmt_pct(d['r12_1'])}, 6M: {fmt_pct(d['r6'])})\n"
    
    bd = details[bonds_name]
    msg += f"\nBONDS: {short_name(bonds_name)} | Score: {fmt_pct(bd['score'])}\n"
    msg += f"\nAKCJA: {action_title}\n{trade_instr}\n\nReason: {reason}"

    print("=== MESSAGE START ===\n" + msg + "\n=== MESSAGE END ===")
    with open("gem_message.txt", "w", encoding="utf-8") as f:
        f.write(msg)

if __name__ == "__main__":
    main()
