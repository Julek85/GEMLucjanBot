from datetime import date
import json
import os
from pathlib import Path
import pandas as pd
import yfinance as yf

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


def fmt_pp(x):
  """Format różnicy w punktach procentowych (0.0123 -> +1.23 pp)."""
  return f"{x * 100:+.2f} pp" if x is not None else "n/a"


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
  """Kolorowa etykieta przewagi w pp (konserwatywne progi).

  pp = różnica w ułamku (np. 0.0543 = +5.43 pp)
  """
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
  """Kolorowy badge dla wyniku momentum (%).

  score = ułamek (np. 0.2357 = 23.57%)
  """
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
    if top_pct >= 20 and eb_pp >= 8:
      return "🔥🚀 BARDZO MOCNY (brak #2)"
    if top_pct >= 12 and eb_pp >= 5:
      return "🟢 MOCNY (brak #2)"
    if top_pct >= 5 and eb_pp >= 2:
      return "🟩 UMIARKOWANY (brak #2)"
    return "🟨 SŁABY (brak #2)"

  if top_pct >= 20 and eb_pp >= 8 and e2_pp >= 5:
    return "🔥🚀 BARDZO MOCNY"
  if eb_pp >= 5 and e2_pp >= 3:
    return "🟢 MOCNY"
  if eb_pp >= 2 and e2_pp >= 2:
    return "🟩 UMIARKOWANY"
  return "🟨 SŁABY"


def pick_anchor_index(me: pd.Series, today: date) -> int:
  """Wybiera punkt odniesienia do momentum:

  - jeśli ostatni punkt jest z bieżącego miesiąca -> użyj -2 (ostatni pełny
  miesiąc)
  - w przeciwnym razie -> -1
  """
  if me is None or me.empty:
    return -1
  last_period = me.index[-1].to_period("M")
  curr_period = pd.Timestamp(today).to_period("M")
  return -2 if last_period == curr_period else -1


# ----------------------------
# Main
# ----------------------------
def main():
  # 1) Secrets / config (odporne na pusty string)
  try:
    tickers_map = json.loads(os.getenv("GEM_TICKERS_JSON") or "{}")
    risk_assets = json.loads(os.getenv("GEM_RISK_ASSETS_JSON") or "[]")
    b_name = (os.getenv("GEM_BONDS_NAME") or "").strip()
    cap = os.getenv("GEM_CAPITAL_EUR") or "0"

    # Tryby momentum:
    # "12-1M" = akademickie momentum (12M z pominięciem ostatniego miesiąca -> eliminuje szum i FOMO)
    # "12M"   = klasyczny GEM (czyste 12 miesięcy)
    momentum_mode = (os.getenv("GEM_MOMENTUM_MODE") or "12-1M").upper()

    # Bufor zmiany pozycji (np. 0.015 = 1.5 pp)
    # Nowe aktywo akcyjne musi być lepsze o co najmniej 1.5 pp od obecnego, aby dokonać przełączenia
    buffer_pp = float(os.getenv("GEM_BUFFER_PP") or "0.015")
  except Exception as e:
    OUT_FILE.write_text(
        f"🔴 GEM Bot Error: Problem z Secrets ({e})", encoding="utf-8"
    )
    return

  if not isinstance(tickers_map, dict) or not tickers_map:
    OUT_FILE.write_text(
        "🔴 GEM Bot Error: GEM_TICKERS_JSON jest pusty lub niepoprawny.",
        encoding="utf-8",
    )
    return
  if not isinstance(risk_assets, list) or not risk_assets:
    OUT_FILE.write_text(
        "🔴 GEM Bot Error: GEM_RISK_ASSETS_JSON jest pusty lub niepoprawny.",
        encoding="utf-8",
    )
    return

  today = date.today()
  curr_mo = pd.Timestamp(today).strftime("%Y-%m")

  # Odczyt aktualnej pozycji z pliku stanu
  old_label = None
  if STATE_FILE.exists():
    try:
      state_data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
      old_label = state_data.get("active_label")
    except Exception:
      old_label = None

  # 2) Pobieranie danych i wyliczanie momentum
  details = {}
  anchor_info_txt = ""

  for name, t_raw in tickers_map.items():
    ticker = clean_ticker(t_raw)
    if not ticker:
      continue
    try:
      raw = yf.download(ticker, period="2y", progress=False)
      series = get_close_series(raw)
      if series.empty:
        continue

      # Seria na koniec miesiąca
      try:
        me = series.resample("ME").last().dropna()
      except Exception:
        me = series.resample("M").last().dropna()

      anchor_idx = pick_anchor_index(me, today)

      # Określamy punkty pomiarowe w zależności od wybranego trybu
      if momentum_mode == "12-1M":
        # 12-1M: odrzucamy ostatni miesiąc (anchor_idx - 1) i patrzymy 12M wstecz (anchor_idx - 13)
        end_idx = anchor_idx - 1
        start_idx = anchor_idx - 13
        min_needed = 14 if anchor_idx == -1 else 15
        mode_desc = "12-1M (z pominięciem ostatniego miesiąca)"
      else:
        # Czyste 12M: od anchor_idx do 12M wstecz (anchor_idx - 12)
        end_idx = anchor_idx
        start_idx = anchor_idx - 12
        min_needed = 13 if anchor_idx == -1 else 14
        mode_desc = "12M (czyste 12 miesięcy)"

      if len(me) < min_needed:
        continue

      score = (me.iloc[end_idx] / me.iloc[start_idx]) - 1.0
      details[name] = {"score": float(score), "ticker": ticker}

      if not anchor_info_txt:
        anchor_date_str = me.index[end_idx].strftime("%Y-%m")
        anchor_info_txt = (
            f"{mode_desc} [dane do: {anchor_date_str}, iloc={end_idx}]"
        )

    except Exception:
      continue

  # 3) Ranking aktywów ryzykownych
  ranked = sorted(
      [(n, details[n]["score"]) for n in risk_assets if n in details],
      key=lambda x: x[1],
      reverse=True,
  )

  if not ranked:
    OUT_FILE.write_text(
        "🔴 GEM Bot Error: Brak danych w Yahoo Finance (risk assets)!",
        encoding="utf-8",
    )
    return

  top_name, top_score = ranked[0]

  # 4) Ocena obligacji i decyzja Risk-On / Risk-Off
  b_score = details.get(b_name, {}).get("score") if b_name else None
  safe_b_score = b_score if b_score is not None else 0.0
  is_risk_on = top_score > safe_b_score

  if old_label is None:
    old_label = top_name if is_risk_on else (b_name if b_name else top_name)

  # Wybór docelowej pozycji z uwzględnieniem BUFORA
  buffer_applied = False
  if not is_risk_on:
    choice = b_name if b_name else top_name
  else:
    choice = top_name
    # Jeśli jesteśmy w aktywie akcyjnym, które nadal jest Risk-On, ale pojawił się nowy lider:
    if (
        old_label in risk_assets
        and old_label in details
        and old_label != top_name
    ):
      old_score = details[old_label]["score"]
      if old_score > safe_b_score:
        # Sprawdzamy, czy przewaga nowego lidera przekracza bufor
        if top_score < old_score + buffer_pp:
          choice = old_label
          buffer_applied = True

  # 5) Aktualizacja stanu
  is_switch = old_label != choice

  state_to_save = {
      "active_label": choice,
      "last_rebalance_month": curr_mo
      if (is_switch or not STATE_FILE.exists())
      else json.loads(STATE_FILE.read_text(encoding="utf-8")).get(
          "last_rebalance_month", curr_mo
      ),
  }
  STATE_FILE.write_text(json.dumps(state_to_save, indent=2), encoding="utf-8")

  # 6) Wyliczenie przewag do raportu
  top_vs_2 = (top_score - ranked[1][1]) if len(ranked) > 1 else None
  top_vs_b_or_cash = top_score - safe_b_score
  top_vs_b = (top_score - b_score) if b_score is not None else None
  strength_txt = signal_strength(
      is_risk_on=is_risk_on,
      top_score=top_score,
      edge_vs_bonds_or_cash=top_vs_b_or_cash,
      edge_vs_2=top_vs_2,
  )

  # 7) Sekcja Akcji
  choice_ticker = details.get(choice, {}).get(
      "ticker", clean_ticker(tickers_map.get(choice, ""))
  )

  if is_switch:
    action_txt = "🔁 ZMIANA POZYCJI"
    action_lines = [
        f"💸 SPRZEDAJ: {old_label}",
        f"🛒 KUP: {choice} ({choice_ticker})",
    ]
  else:
    if not is_risk_on:
      action_txt = "🛡️ TRYB OBRONNY (BEZ ZMIAN)"
    else:
      action_txt = (
          "🟦 TRZYMAJ POZYCJĘ (BUFOR)"
          if buffer_applied
          else "🟦 TRZYMAJ POZYCJĘ"
      )
    action_lines = [f"📌 POZOSTAŃ W: {old_label}"]

  # 8) Wiadomość wyjściowa
  vmin = min(s for _, s in ranked)
  vmax = max(s for _, s in ranked)
  lines = []
  lines.append(f"📌 GEM SIGNAL — {today.isoformat()}")
  lines.append("")
  lines.append(f"🏆 TOP: ✅ {top_name} — {score_badge(top_score)}")
  lines.append(f"🥈 Przewaga nad #2: {edge_badge(top_vs_2)}")

  if b_score is not None:
    lines.append(f"🛡️ Vs BONDS: {edge_badge(top_vs_b)}")
  else:
    lines.append("🛡️ Vs BONDS: ⚪ n/a (fallback: cash=0)")

  lines.append("")
  lines.append(f"💥 SIŁA SYGNAŁU: {strength_txt}")
  lines.append(f"🚦 TRYB: {'RISK-ON ✅' if is_risk_on else 'RISK-OFF 🛡️'}")
  lines.append(f"🎯 AKCJA: {action_txt}")
  lines.extend(action_lines)

  if buffer_applied:
    lines.append(
        f"ℹ️ Aktywowano bufor ({buffer_pp*100:.1f} pp) — nowa pozycja nie"
        f" przekroczyła wymaganego progu przewagi nad {old_label}."
    )

  lines.append(f"💶 KWOTA: {cap} EUR")
  lines.append("")
  lines.append("📊 RANKING (momentum):")

  for i, (n, s) in enumerate(ranked, 1):
    bar = emoji_bar(s, vmin, vmax, width=10)
    mark = " (OBECNY)" if n == old_label else ""
    lines.append(f"{i}) {n:<18} {score_badge(s):>10} {bar}{mark}")

  lines.append("")
  if b_name:
    lines.append(f"🧾 BONDS ({b_name}): {fmt_pct(b_score)}")
  else:
    lines.append("🧾 BONDS: (nie ustawiono GEM_BONDS_NAME)")

  lines.append(f"📅 Baza momentum: {anchor_info_txt}")
  lines.append(f"🕒 Rebalance: {curr_mo} (1× / miesiąc)")

  OUT_FILE.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
  main()
