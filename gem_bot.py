import os
import sys
import math
import json
import time
import logging
from datetime import datetime, timezone
from typing import Dict, Optional

import pandas as pd
import yfinance as yf

# Konfiguracja logowania
logger = logging.getLogger("gem")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

def is_finite(x: float) -> bool:
    return isinstance(x, (int, float)) and not (math.isnan(x) or math.isinf(x))

def month_end_series(series: pd.Series) -> pd.Series:
    """Konwertuje dane dzienne na miesięczne (kompatybilność 'M')."""
    s = series.dropna()
    if s.empty: return s
    s.index = pd.to_datetime(s.index, errors="coerce")
    s = s[~s.index.isna()]
    if s.empty: return s
    return s.resample("M").last().dropna()

def total_return(monthly_prices: pd.Series, months: int, skip_last: int = 0) -> float:
    needed = months + 1 + skip_last
    if monthly_prices is None or len(monthly_prices) < needed:
        return float("nan")
    try:
        end = monthly_prices.iloc[-1 - skip_last]
        start = monthly_prices.iloc[-1 - skip_last - months]
        if pd.isna(start) or pd.isna(end) or start == 0:
            return float("nan")
        return (end / start) - 1.0
    except Exception:
        return float("nan")

def fmt_pct(x: float) -> str:
    return "n/a" if not is_finite(x) else f"{x * 100:.2f}%"

def load_env_json(name: str, default):
    raw = os.environ.get(name)
    if not raw: return default
    try:
        return json.loads(raw)
    except Exception as e:
        logger.error("Błąd JSON w %s: %s", name, e)
        sys.exit(2)

def extract_price_series(data: pd.DataFrame, ticker: str) -> pd.Series:
    if data is None or data.empty: return pd.Series(dtype=float)
    if isinstance(data.columns, pd.MultiIndex):
        lvl0 = data.columns.get_level_values(0)
        key = "Adj Close" if "Adj Close" in lvl0 else "Close"
        tmp = data[key]
        return tmp[ticker] if hasattr(tmp, "columns") and ticker in tmp.columns else tmp.iloc[:, 0]
    if "Adj Close" in data.columns: return data["Adj Close"]
    if "Close" in data.columns: return data["Close"]
    num = data.select_dtypes(include="number")
    return num.iloc[:, 0] if not num.empty else pd.Series(dtype=float)

def download_with_retry(ticker: str, start: str, end: str, tries: int = 3, sleep_s: int = 2) -> pd.DataFrame:
    last_err: Optional[Exception] = None
    for i in range(1, tries + 1):
        try:
            df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=False, actions=False)
            if df is not None and not df.empty: return df
            logger.warning("Puste dane dla %s (próba %d/%d)", ticker, i, tries)
        except Exception as e:
            last_err = e
            logger.warning("Próba %d/%d dla %s nieudana: %s", i, tries, ticker, e)
        time.sleep(sleep_s)
    logger.error("Błąd krytyczny pobierania %s. Ostatni błąd: %s", ticker, last_err)
    sys.exit(2)

def main():
    # 1. Konfiguracja i Walidacja (Gienek Fixes)
    tickers: Dict[str, str] = load_env_json("GEM_TICKERS_JSON", {})
    risk_assets = load_env_json("GEM_RISK_ASSETS_JSON", [])
    bonds_name = os.environ.get("GEM_BONDS_NAME", "BONDS")
    riskoff_threshold = float(os.environ.get("GEM_RISK_OFF_THRESHOLD", "0"))
    switch_threshold = float(os.environ.get("GEM_SWITCH_THRESHOLD", "0.01"))
    capital_eur = os.environ.get("GEM_CAPITAL_EUR", "583")
    current_holding = os.environ.get("GEM_CURRENT_HOLDING", "").strip()

    if not tickers or not risk_assets:
        logger.error("Brak danych w GEM_TICKERS_JSON lub GEM_RISK_ASSETS_JSON.")
        sys.exit(2)
    
    missing = [n for n in risk_assets if n not in tickers]
    if missing:
        logger.error("Risk_assets zawiera nazwy nieobecne w tickers: %s", ", ".join(missing))
        sys.exit(2)
    
    if bonds_name not in tickers:
        logger.error("Brakuje obligacji w tickers. Klucz '%s' musi być w GEM_TICKERS_JSON.", bonds_name)
        sys.exit(2)

    # 2. Pobieranie Danych
    start = "2000-01-01"
    end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    monthly = {}
    for name, ticker in tickers.items():
        logger.info("Pobieram %s (%s)...", name, ticker)
        data = download_with_retry(ticker, start, end)
        series = extract_price_series(data, ticker)
        monthly[name] = month_end_series(series)

    # 3. Obliczenia Momentum
    details = {}
    for name, series in monthly.items():
        r12_1 = total_return(series, 12, skip_last=1)
        r6 = total_return(series, 6, skip_last=0)
        score = (0.5 * r12_1 + 0.5 * r6) if (is_finite(r12_1) and is_finite(r6)) else float("nan")
        details[name] = {"r12_1": r12_1, "r6": r6, "score": score}

    # 4. Logika GEM
    ranked = [(n, details[n]["score"]) for n in risk_assets if is_finite(details[n]["score"])]
    ranked.sort(key=lambda x: x[1], reverse=True)

    if not ranked:
        choice = bonds_name
        top_score = float("nan")
    else:
        top_name, top_score = ranked[0]
        choice = top_name if top_score > riskoff_threshold else bonds_name

    # Anti-noise Switch
    if current_holding and choice != current_holding and current_holding in details:
        curr_score = details[current_holding]["score"]
        if is_finite(top_score) and is_finite(curr_score):
            if (top_score - curr_score) < switch_threshold:
                choice = current_holding

    # 5. Budowanie Raportu (Kosmetyka Gienka)
    if not current_holding:
        status = "USTAW PIERWSZĄ POZYCJĘ"
        instr = f"KUP: {choice} za {capital_eur} EUR"
    elif choice == current_holding:
        status = "TRZYMAJ"
        instr = f"Pozostań w: {choice}"
    else:
        status = "ZMIANA"
        instr = f"SPRZEDAJ: {current_holding} -> KUP: {choice} za {capital_eur} EUR"
    
    msg = f"🚀 GEM SIGNAL: {status}\n\n{instr}\n\nRANKING (risk assets):\n"
    if not ranked:
        msg += "- Brak wystarczających danych (wszystko NaN lub za krótka historia)\n"
    else:
        for n, s in ranked:
            msg += f"- {n}: {fmt_pct(s)} (12-1: {fmt_pct(details[n]['r12_1'])}, 6M: {fmt_pct(details[n]['r6'])})\n"
    
    b = details[bonds_name]
    msg += f"\nBONDS ({bonds_name}): {fmt_pct(b['score'])} (12-1: {fmt_pct(b['r12_1'])}, 6M: {fmt_pct(b['r6'])})\n"
    msg += f"\nRisk-off próg: {fmt_pct(riskoff_threshold)} | Próg zmiany: {fmt_pct(switch_threshold)}\n"

    print(msg)
    with open("gem_message.txt", "w", encoding="utf-8") as f:
        f.write(msg)

if __name__ == "__main__":
    main()
