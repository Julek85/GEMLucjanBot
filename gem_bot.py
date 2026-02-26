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
# Logging
# ---------------------------
logger = logging.getLogger("gem")
_log_level = os.environ.get("GEM_LOG_LEVEL", "INFO").upper().strip()
logging.basicConfig(
    level=getattr(logging, _log_level, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)


def month_end_series(series: pd.Series) -> pd.Series:
    """Daily -> month-end series (pandas >= 2.2 uses 'ME')."""
    s = series.dropna()
    if s.empty:
        return s
    s.index = pd.to_datetime(s.index)
    return s.resample("ME").last().dropna()


def total_return(monthly_prices: pd.Series, months: int, skip_last: int = 0) -> float:
    """Total return over `months` month-ends; skip_last=1 implements classic 12-1 momentum."""
    needed = months + 1 + skip_last
    if len(monthly_prices) < needed:
        return float("nan")
    end = monthly_prices.iloc[-1 - skip_last]
    start = monthly_prices.iloc[-1 - skip_last - months]
    return (end / start) - 1.0


def fmt_pct(x: float) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "n/a"
    return f"{x*100:.2f}%"


def load_env_json(name: str, default):
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"ERROR: {name} is not valid JSON: {e}")
        sys.exit(2)


def extract_price_series(data: pd.DataFrame, preferred: str, ticker: str, name: str = "") -> pd.Series:
    """
    Always return a 1D Series from yfinance output (handles MultiIndex/multi-ticker).
    Adds explicit logging when Adj Close is missing and bot falls back to Close.
    """
    if data is None or data.empty:
        raise ValueError("empty dataframe")

    used_key = None

    # MultiIndex case (sometimes yfinance returns MultiIndex columns)
    if isinstance(data.columns, pd.MultiIndex):
        level0 = data.columns.get_level_values(0)
        if preferred in level0:
            used_key = preferred
        elif "Close" in level0:
            used_key = "Close"
        else:
            used_key = level0[0]

        tmp = data[used_key]
        if isinstance(tmp, pd.DataFrame):
            s = tmp[ticker] if ticker in tmp.columns else tmp.iloc[:, 0]
        else:
            s = tmp

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

    # ---- Logging re: Adj Close fallback / quality ----
    display = f"{name} ({ticker})" if name else ticker

    if used_key != preferred:
        logger.warning(f"[{display}] '{preferred}' not available -> using '{used_key}' (NOT dividend/split adjusted).")
    else:
        total = len(s)
        non_na = int(s.notna().sum())
        if total > 0:
            missing_ratio = 1.0 - (non_na / total)
            if missing_ratio > 0.10:
                logger.warning(f"[{display}] '{preferred}' has {missing_ratio:.0%} missing values; results may be noisy.")

    return s


def download_with_retry(
    ticker: str,
    start: str,
    end: str,
    tries: int = 3,
    base_sleep: float = 2.0,
    per_ticker_sleep: float = 1.2,
):
    """
    yfinance download with retry + exponential backoff.
    Helpful when Yahoo blocks/rate-limits GitHub Actions IPs.
    """
    last_err = None

    # Small sleep between tickers to reduce rate-limit probability
    if per_ticker_sleep and per_ticker_sleep > 0:
        time.sleep(per_ticker_sleep)

    for i in range(tries):
        try:
            df = yf.download(
                ticker,
                start=start,
                end=end,
                progress=False,
                auto_adjust=False,
                group_by="column",
                threads=False,
            )
            if df is None or df.empty:
                raise ValueError("No data returned (empty dataframe)")
            return df
        except Exception as e:
            last_err = e
            sleep_s = base_sleep * (2 ** i)
            logger.warning(f"[{ticker}] download failed: {e} | retry in {sleep_s:.1f}s ({i+1}/{tries})")
            time.sleep(sleep_s)

    raise RuntimeError(f"[{ticker}] download failed after {tries} tries: {last_err}")


def main():
    tickers = load_env_json("GEM_TICKERS_JSON", {})
    risk_assets = load_env_json("GEM_RISK_ASSETS_JSON", [])
    bonds_name = os.environ.get("GEM_BONDS_NAME", "BONDS")

    # Risk-off threshold (classic = 0), and anti-noise switch threshold (optional)
    riskoff_threshold = float(os.environ.get("GEM_RISK_OFF_THRESHOLD", "0"))
    switch_threshold = float(os.environ.get("GEM_SWITCH_THRESHOLD", "0"))  # e.g. 0.02 => 2%

    capital_eur = os.environ.get("GEM_CAPITAL_EUR", "560")
    current_holding = os.environ.get("GEM_CURRENT_HOLDING", "").strip()

    # Retry/sleep tuning from env
    dl_tries = int(os.environ.get("GEM_YF_TRIES", "3"))
    dl_base_sleep = float(os.environ.get("GEM_YF_BASE_SLEEP", "2"))
    dl_per_ticker_sleep = float(os.environ.get("GEM_YF_TICKER_SLEEP", "1.2"))

    if not tickers:
        print("ERROR: GEM_TICKERS_JSON is empty")
        sys.exit(2)
    if not risk_assets:
        print("ERROR: GEM_RISK_ASSETS_JSON is empty")
        sys.exit(2)
    if bonds_name not in tickers:
        print(f"ERROR: GEM_BONDS_NAME '{bonds_name}' is not a key in GEM_TICKERS_JSON")
        sys.exit(2)

    logger.info(f"Loaded tickers: {list(tickers.keys())}")
    logger.info(f"Risk assets: {risk_assets} | Bonds: {bonds_name}")
    logger.info(f"Current holding (from ENV/Secrets): '{current_holding or '(empty)'}'")

    start = os.environ.get("GEM_START_DATE", "2000-01-01")
    end = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    monthly = {}
    last_me_date = {}

    for name, ticker in tickers.items():
        try:
            data = download_with_retry(
                ticker=ticker,
                start=start,
                end=end,
                tries=dl_tries,
                base_sleep=dl_base_sleep,
                per_ticker_sleep=dl_per_ticker_sleep,
            )
        except Exception as e:
            print(f"ERROR: No data for {name} ({ticker}). {e}")
            sys.exit(2)

        try:
            series = extract_price_series(data, preferred="Adj Close", ticker=ticker, name=name)
        except Exception as e:
            print(f"ERROR: Could not extract prices for {name} ({ticker}): {e}")
            sys.exit(2)

        me = month_end_series(series)
        if me.empty:
            print(f"ERROR: No month-end data for {name} ({ticker}).")
            sys.exit(2)

        monthly[name] = me
        last_me_date[name] = str(me.index[-1].date())

    details = {}
    for name, series in monthly.items():
        r12_1 = total_return(series, 12, skip_last=1)
        r6 = total_return(series, 6, skip_last=0)

        # Momentum blend (12-1 + 6M)
        score = 0.5 * r12_1 + 0.5 * r6
        details[name] = {"r12_1": float(r12_1), "r6": float(r6), "score": float(score)}

    ranked = sorted(
        ((n, details[n]["score"]) for n in risk_assets),
        key=lambda x: x[1],
        reverse=True,
    )

    top_name, top_score = ranked[0]

    # Risk-off decision
    if (top_score is None) or (isinstance(top_score, float) and math.isnan(top_score)) or top_score <= riskoff_threshold:
        choice = bonds_name
        reason = f"RISK-OFF: best {top_name} = {fmt_pct(top_score)} <= {fmt_pct(riskoff_threshold)}"
    else:
        choice = top_name
        reason = f"RISK-ON: winner {top_name} = {fmt_pct(top_score)}"

    # For reporting: how close were we to switching?
    edge_info_line = ""

    # Optional: anti-noise switch threshold
    if current_holding and (choice != current_holding) and switch_threshold > 0:
        current_score = details.get(current_holding, {}).get("score", float("nan"))
        if isinstance(current_score, float) and not math.isnan(current_score):
            edge = top_score - current_score
            edge_info_line = (
                f"Edge vs holding: leader={fmt_pct(top_score)} | holding={fmt_pct(current_score)} | "
                f"edge={fmt_pct(edge)} | threshold={fmt_pct(switch_threshold)}"
            )
            if edge < switch_threshold:
                logger.info(
                    f"Switch blocked by GEM_SWITCH_THRESHOLD: edge={fmt_pct(edge)} < {fmt_pct(switch_threshold)}"
                )
                choice = current_holding
                reason += f" | SWITCH_BLOCKED edge {fmt_pct(edge)} < {fmt_pct(switch_threshold)}"

    now_local = datetime.now().strftime("%Y-%m-%d %H:%M")

    def short_name(x: str) -> str:
        return (
            x.replace("EM (Vanguard FTSE EM Acc)", "EM")
            .replace("USA (VUAA)", "USA (VUAA)")
            .replace("DM ex-US (EXUS)", "DM ex-US (EXUS)")
            .replace("BONDS (VAGF)", "BONDS (VAGF)")
        )

    if current_holding and current_holding == choice:
        action_title = "TRZYMAJ (BEZ ZMIAN)"
        trade_lines = [
            f"Masz juz: {short_name(choice)}",
            "Nic nie robisz w Trading 212.",
        ]
    elif current_holding and current_holding != choice:
        action_title = "ZMIEN POZYCJE"
        trade_lines = [
            f"SPRZEDAJ: {short_name(current_holding)}",
            f"KUP: {short_name(choice)}",
            f"KWOTA: {capital_eur} EUR (100% kapitalu rotacyjnego)",
        ]
    else:
        action_title = "DECYZJA"
        trade_lines = [
            f"KUP/TRZYMAJ: {short_name(choice)}",
            f"KWOTA: {capital_eur} EUR (100% kapitalu rotacyjnego)",
        ]

    lines = []
    lines.append("GEM SIGNAL (Classic 12-1 + 6M)")
    lines.append(f"Time: {now_local}")
    lines.append("")

    lines.append("RANKING (risk assets):")
    for i, (n, _) in enumerate(ranked, start=1):
        d = details[n]
        lines.append(
            f"{i}. {short_name(n)} "
            f"score {fmt_pct(d['score'])} "
            f"12-1 {fmt_pct(d['r12_1'])} "
            f"6M {fmt_pct(d['r6'])}"
        )

    lines.append("")
    bd = details[bonds_name]
    lines.append(
        f"BONDS: {short_name(bonds_name)} "
        f"score {fmt_pct(bd['score'])} "
        f"12-1 {fmt_pct(bd['r12_1'])} "
        f"6M {fmt_pct(bd['r6'])}"
    )

    lines.append("")
    lines.append(f"ACTION: {action_title}")
    for t in trade_lines:
        lines.append(t)
    lines.append("")
    lines.append(f"Reason: {reason}")
    lines.append("Rule: Top1 score > threshold => RISK-ON, else => BONDS (RISK-OFF)")

    if switch_threshold > 0:
        lines.append(f"Anti-noise: GEM_SWITCH_THRESHOLD = {fmt_pct(switch_threshold)}")
    if edge_info_line:
        lines.append(edge_info_line)

    msg = "\n".join(lines)

    print("=== GEM MESSAGE START ===")
    print(msg)
    print("=== GEM MESSAGE END ===")

    with open("gem_message.txt", "w", encoding="utf-8") as f:
        f.write(msg)


if __name__ == "__main__":
    main()
