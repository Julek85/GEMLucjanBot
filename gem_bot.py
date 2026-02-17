import os
          import sys
          import math
          import json
          import pandas as pd
          from datetime import datetime, timezone
          import yfinance as yf

          def month_end_series(price_series: pd.Series) -> pd.Series:
              s = price_series.dropna()
              if s.empty:
                  return s
              s.index = pd.to_datetime(s.index)
              return s.resample("M").last().dropna()

          def total_return(monthly_prices: pd.Series, months: int, skip_last: int = 0) -> float:
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

          def main():
              tickers = json.loads(os.environ.get("GEM_TICKERS_JSON", "{}"))
              risk_assets = json.loads(os.environ.get("GEM_RISK_ASSETS_JSON", "[]"))
              bonds_name = os.environ.get("GEM_BONDS_NAME", "BONDS")
              threshold = float(os.environ.get("GEM_RISK_OFF_THRESHOLD", "0.0"))

              if not tickers:
                  print("ERROR: GEM_TICKERS_JSON is empty.")
                  sys.exit(2)
              if not risk_assets:
                  print("ERROR: GEM_RISK_ASSETS_JSON is empty.")
                  sys.exit(2)

              start = "2000-01-01"
              end = datetime.now(timezone.utc).strftime("%Y-%m-%d")

              monthly = {}
              last_me_date = {}

              for name, ticker in tickers.items():
                  data = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=False)
                  if data is None or data.empty:
                      print(f"ERROR: No data for {name} ({ticker}).")
                      sys.exit(2)

                  series = data["Adj Close"] if "Adj Close" in data.columns else data["Close"]
                  me = month_end_series(series)
                  if me.empty:
                      print(f"ERROR: No month-end data for {name} ({ticker}).")
                      sys.exit(2)

                  monthly[name] = me
                  last_me_date[name] = str(me.index[-1].date())

              details = {}
              for name, series in monthly.items():
                  r12_1 = total_return(series, 12, skip_last=1)   # 12-1
                  r6 = total_return(series, 6, skip_last=0)
                  score = 0.5 * r12_1 + 0.5 * r6
                  details[name] = {"r12_1": r12_1, "r6": r6, "score": score}

              ranked = sorted(((n, details[n]["score"]) for n in risk_assets), key=lambda x: x[1], reverse=True)
              top_name, top_score = ranked[0]

              if (top_score is None) or (isinstance(top_score, float) and math.isnan(top_score)) or top_score <= threshold:
                  choice = bonds_name
                  reason = f"RISK-OFF: najlepszy wynik {top_name} = {fmt_pct(top_score)} ≤ {fmt_pct(threshold)}"
              else:
                  choice = top_name
                  reason = f"RISK-ON: wygrywa {top_name} = {fmt_pct(top_score)}"

              now_local = datetime.now().strftime("%Y-%m-%d %H:%M")
              lines = []
              lines.append("📈 GEM – sygnał (klasyczny 12-1 + 6M)")
              lines.append(f"🕒 {now_local}")
              lines.append("")
              lines.append("Ranking (risk assets):")
              for n, _ in ranked:
                  d = details[n]
                  lines.append(
                      f"• {n}: score {fmt_pct(d['score'])} | 12-1 {fmt_pct(d['r12_1'])} | 6M {fmt_pct(d['r6'])} | ME: {last_me_date[n]}"
                  )
              lines.append("")
              if bonds_name in details:
                  d = details[bonds_name]
                  lines.append(
                      f"🛡️ {bonds_name}: score {fmt_pct(d['score'])} | 12-1 {fmt_pct(d['r12_1'])} | 6M {fmt_pct(d['r6'])} | ME: {last_me_date[bonds_name]}"
                  )
                  lines.append("")
              lines.append(f"✅ DECYZJA: {choice}")
              lines.append(f"ℹ️ {reason}")
              lines.append("💰 Kapitał GEM: 560€ (rotacyjny, 1 ETF naraz)")

              msg = "\n".join(lines)
              print(msg)

              with open("gem_message.txt", "w", encoding="utf-8") as f:
                  f.write(msg)

          if __name__ == "__main__":
              main()
          PY

      - name: Run GEM (classic 12-1 + 6M)
        env:
          # Tickery Yahoo Finance (jeśli któryś nie działa, dopasujemy)
          GEM_TICKERS_JSON: >-
            {
              "USA (VUAA)": "VUAA.L",
              "DM ex-US (EXUS)": "EXUS.L",
              "EM (Vanguard FTSE EM Acc)": "VFEG.L",
              "BONDS (VAGF)": "VAGF.DE"
            }
          GEM_RISK_ASSETS_JSON: >-
            ["USA (VUAA)", "DM ex-US (EXUS)", "EM (Vanguard FTSE EM Acc)"]
          GEM_BONDS_NAME: "BONDS (VAGF)"
          GEM_RISK_OFF_THRESHOLD: "0"
        run: |
          python gem_bot.py

      - name: Send Telegram alert
        env:
          BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: |
          set -e
          TEXT="$(cat gem_message.txt)"
          curl -sS -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
            -d "chat_id=${CHAT_ID}" \
            --data-urlencode "text=${TEXT}" \
            -d "disable_web_page_preview=true"
