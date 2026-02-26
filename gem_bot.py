import os
import sys
import math
import json
import time
import logging
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

# Logowanie błędów do konsoli GitHub Actions
logger = logging.getLogger("gem")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

def month_end_series(series: pd.Series) -> pd.Series:
    """Konwertuje dane dzienne na miesięczne. Naprawiony błąd 'M' -> 'ME'."""
    s = series.dropna()
    if s.empty:
        return s
    s.index = pd.to_datetime(s.index)
    #  Kluczowa poprawka: ME zamiast M dla nowych wersji pandas
    try:
        return s.resample("ME").last().dropna()
    except ValueError:
        return s.resample("M").last().dropna()

def total_return(monthly_prices: pd.Series, months: int, skip_last: int = 0) -> float:
    """Oblicza stopę zwrotu z przesunięciem (classic 12-1)[cite: 34]."""
    needed = months + 1 + skip_last
    if len(monthly_prices) < needed:
        return float("nan")
    end = monthly_prices.iloc[-1 - skip_last]
    start = monthly_prices.iloc[-1 - skip_last - months]
    return (end / start) - 1.0

def fmt_pct(x: float) -> str:
    if x is None or not (isinstance(x, (int, float)) and math.isfinite(x)):
        return "n/a"
    return f"{x*100:.2f}%"

def extract_price_series(data: pd.DataFrame, ticker: str) -> pd.Series:
    """Bezpieczne wyciąganie cen niezależnie od formatu yfinance."""
    if data is None or data.empty:
        raise ValueError(f"Brak danych dla {ticker}")

    # Obsługa MultiIndex (częste w nowym yfinance) [cite: 37, 38]
    if isinstance(data.columns, pd.MultiIndex):
        for col in ['Adj Close', 'Close']:
            if col in data.columns.get_level_values(0):
                tmp = data[col]
                return tmp[ticker] if ticker in tmp.columns else tmp.iloc[:, 0]
    
    # Standardowe kolumny [cite: 39]
    for col in ['Adj Close', 'Close']:
        if col in data.columns:
            return data[col]
            
    return data.iloc[:, 0]

def main():
    # Pobieranie konfiguracji z ENV [cite: 35, 48]
    tickers = json.loads(os.environ.get("GEM_TICKERS_JSON", "{}"))
    risk_assets = json.loads(os.environ.get("GEM_RISK_ASSETS_JSON", "[]"))
    bonds_name = os.environ.get("GEM_BONDS_NAME", "BONDS (VAGF)")
    capital_eur = os.environ.get("GEM_CAPITAL_EUR", "560")
    current_holding = os.environ.get("GEM_CURRENT_HOLDING", "").strip()

    if not tickers or not risk_assets:
        logger.error("Błąd konfiguracji JSON[cite: 35].")
        sys.exit(2)

    start = "2000-01-01"
    end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    monthly = {}

    for name, ticker in tickers.items():
        logger.info(f"Pobieram: {name} ({ticker})")
        # Pobieranie z automatycznym retry [cite: 43, 44]
        df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=False)
        
        try:
            series = extract_price_series(df, ticker)
            me = month_end_series(series)
            if me.empty:
                raise ValueError("Brak danych po resamplingu")
            monthly[name] = me
        except Exception as e:
            logger.error(f"Błąd danych dla {name}: {e}[cite: 51].")
            sys.exit(2)

    # Obliczanie Momentum (12-1 + 6M) [cite: 53]
    details = {}
    for name, series in monthly.items():
        r12_1 = total_return(series, 12, skip_last=1)
        r6 = total_return(series, 6, skip_last=0)
        score = 0.5 * r12_1 + 0.5 * r6
        details[name] = {"r12_1": r12_1, "r6": r6, "score": score}

    # Ranking aktywów [cite: 61]
    ranked = sorted(((n, details[n]["score"]) for n in risk_assets), key=lambda x: x[1], reverse=True)
    top_name, top_score = ranked[0]
    
    # Decyzja RISK-ON/OFF [cite: 54]
    choice = top_name if top_score > 0 else bonds_name

    # Budowanie wiadomości [cite: 58, 60]
    status = "TRZYMAJ" if choice == current_holding else "ZMIANA"
    instr = f"Pozostań w: {choice}" if choice == current_holding else f"SPRZEDAJ: {current_holding} -> KUP: {choice}"
    
    lines = [
        f"GEM SIGNAL: {status}",
        f"AKCJA: {instr}",
        f"KWOTA: {capital_eur} EUR",
        "",
        "RANKING:"
    ]
    for i, (n, s) in enumerate(ranked, 1):
        lines.append(f"{i}. {n}: {fmt_pct(s)} (12-1: {fmt_pct(details[n]['r12_1'])}, 6M: {fmt_pct(details[n]['r6'])})")
    
    lines.append(f"\nBONDS: {fmt_pct(details[bonds_name]['score'])}")

    msg = "\n".join(lines)
    with open("gem_message.txt", "w", encoding="utf-8") as f:
        f.write(msg)
    logger.info("Wiadomość wygenerowana pomyślnie.")

if __name__ == "__main__":
    main()
