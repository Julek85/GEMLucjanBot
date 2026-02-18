import os
import sys
import math
import json
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf


def month_end_series(series: pd.Series) -> pd.Series:
    """Convert daily prices to month-end prices (last available trading day each month)."""
    s = series.dropna()
    if s.empty:
        return s
    s.index = pd.to_datetime(s.index)
    return s.resample("M").last().dropna()


def total_return(monthly_prices: pd.Series, months: int, skip_last: int = 0) -> float:
    """Total return over `months` months using month-end prices.

    skip_last=1 implements classic '12-1' momentum (skip the most recent month).
    """
    needed = months + 1 + skip_last
    if len(monthly_prices) < needed:
        return float("nan")

    end = monthly_prices.iloc[-1 - skip_last]
    start = monthly_prices.iloc[-1 - skip_last - months]
    return (end / start) - 1.0


def fmt_pct(x: float) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "n/a"
    return f"{x * 100:.2f}%"


def load_env_json(name: str, default):
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: {name} is not valid JSON: {e}")
        sys.exit(2)


def main():
    tickers = load_env_json("GEM_TICKERS_JSON", {})
    risk_assets = load_env_json("GEM_RISK_ASSETS_JSON", [])
    bonds_name = os.environ.get("GEM_BONDS_NAME", "BONDS")
    threshold = float(os.environ.get("GEM_RISK_OFF_THRESHOLD", "0"))
    capital_eur = os.environ.get("GEM_CAPITAL_EUR", "560")

    if not tickers:
        print("ERROR: GEM_TICKERS_JSON is empty")
        sys.exit(2)
    if not risk_assets:
        print("ERROR: GEM_RISK_ASSETS_JSON is empty")
        sys.exit(2)
    if bonds_name not in tickers:
        print(f"ERROR: GEM_BONDS_NAME '{bonds_name}' is not a key in GEM_TICKERS_JSON")
        sys.exit(2)

    start = "2000-01-01"
    end = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    monthly = {}
    last_me_date = {}

    for name, ticker in tickers.items():
        data = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=False)
        if data is None or data.empty:
            print(f"ERROR: No data for {name} ({ticker}). Check the Yahoo ticker.")
            sys.exit(2)

        series = data["Adj Close"] if "Adj Close" in data.columns else data["Close"]
        me = month_end_series(series)
        if me.empty:
            print(f"ERROR: No month-end data for {name} ({ticker}).")
            sys.exit(2)

        monthly[name] = me
        last_me_date[name] = str(me.index[-1].date())

    # Classic GEM score: 0.5*(12-1) + 0.5*(6M)
    details = {}
    for name, series in monthly.items():
        r12_1 = total_return(series, 12, skip_last=1)
        r6 = total_return(series, 6, skip_last=0)
        score = 0.5 * r12_1 + 0.5 * r6
        details[name] = {"r12_1": r12_1, "r6": r6, "score": score}

    ranked = sorted(((n, details[n]["score"]) for n in risk_assets), key=lambda x: x[1], reverse=True)
    top_name, top_score = ranked[0]

    if (top_score is None) or (isinstance(top_score, float) and math.isnan(top_score)) or top_score <= threshold:
        choice = bonds_name
        reason = f"RISK-OFF: najlepszy wynik {top_name} = {fmt_pct(top_score)} â¤ {fmt_pct(threshold)}"
    else:
        choice = top_name
        reason = f"RISK-ON: wygrywa {top_name} = {fmt_pct(top_score)}"

    now_local = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = []
    lines.append("ð GEM â sygnaÅ (klasyczny 12-1 + 6M)")
    lines.append(f"ð {now_local}")
    lines.append("")
    lines.append("Ranking (risk assets):")

    for n, _ in ranked:
        d = details[n]
        lines.append(
            f"â¢ {n}: score {fmt_pct(d['score'])} | 12-1 {fmt_pct(d['r12_1'])} | 6M {fmt_pct(d['r6'])} | ME: {last_me_date[n]}"
        )

    lines.append("")

    bd = details[bonds_name]
    lines.append(
        f"ð¡ï¸ {bonds_name}: score {fmt_pct(bd['score'])} | 12-1 {fmt_pct(bd['r12_1'])} | 6M {fmt_pct(bd['r6'])} | ME: {last_me_date[bonds_name]}"
    )

    lines.append("")
    lines.append(f"â DECYZJA: {choice}")
    lines.append(f"â¹ï¸ {reason}")
    lines.append(f"ð° KapitaÅ GEM: {capital_eur}â¬ (rotacyjny, 1 ETF naraz)")

    msg = "
".join(lines)
    print("=== GEM MESSAGE START ===")
    print(msg)
    print("=== GEM MESSAGE END ===")

    with open("gem_message.txt", "w", encoding="utf-8") as f:
        f.write(msg)


if __name__ == "__main__":
    main()
