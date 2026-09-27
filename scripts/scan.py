"""Build the Nifty 500 daily 44 SMA scan for the static GitHub Pages site."""
import csv
import io
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.request import Request, urlopen

import yfinance as yf

CONSTITUENTS = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"
OUTPUT = os.path.join(os.path.dirname(__file__), "..", "scan-results.json")


def symbols():
    request = Request(CONSTITUENTS, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.niftyindices.com/"})
    with urlopen(request, timeout=35) as response:
        rows = list(csv.DictReader(io.StringIO(response.read().decode("utf-8-sig"))))
    names = sorted({row["Symbol"].strip() for row in rows if row.get("Symbol")})
    if len(names) < 450:
        raise RuntimeError(f"Nifty 500 constituent list incomplete: {len(names)} symbols")
    return names


def scan(symbol):
    for attempt in range(2):
        try:
            frame = yf.Ticker(symbol + ".NS").history(period="4mo", interval="1d", auto_adjust=False, timeout=25)
            if len(frame) < 45:
                raise ValueError("fewer than 45 daily candles")
            frame = frame.dropna(subset=["Open", "Close"])
            if len(frame) < 45:
                raise ValueError("incomplete candles")
            closes = frame["Close"]
            today = frame.iloc[-1]
            ma = float(closes.iloc[-44:].mean())
            previous_ma = float(closes.iloc[-45:-1].mean())
            close = float(today["Close"])
            distance = (close / ma - 1) * 100
            result = None
            if ma > previous_ma and close > float(today["Open"]) and 0 < distance <= 1:
                result = {"symbol": symbol, "close": round(close, 2), "ma44": round(ma, 2), "distance_pct": round(distance, 2), "date": frame.index[-1].date().isoformat()}
            return result, frame.index[-1].date().isoformat(), None
        except Exception as exc:
            if attempt == 0:
                time.sleep(2)
            else:
                return None, None, f"{symbol}: {str(exc)[:120]}"


def main():
    universe = symbols()
    matches, dates, failures = [], [], []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for future in as_completed([pool.submit(scan, symbol) for symbol in universe]):
            match, date, error = future.result()
            if match:
                matches.append(match)
            if date:
                dates.append(date)
            if error:
                failures.append(error)
    successful = len(universe) - len(failures)
    if successful < 450:
        raise RuntimeError(f"Only {successful}/{len(universe)} symbols fetched; retaining prior scan. Sample errors: {failures[:3]}")
    latest = max(dates)
    # Exclude stocks with delayed or missing candles from the current snapshot.
    matches = sorted((x for x in matches if x["date"] == latest), key=lambda x: x["distance_pct"])
    payload = {"as_of": latest, "generated_at": datetime.now(timezone.utc).isoformat(), "universe_count": len(universe), "scanned_count": successful, "latest_candle_count": dates.count(latest), "failed_count": len(failures), "matches": matches}
    with open(OUTPUT, "w", encoding="utf-8") as target:
        json.dump(payload, target, indent=2, ensure_ascii=False)
        target.write("\n")
    print(f"Scanned {successful}/{len(universe)}; {len(matches)} matches for {latest}; failures: {failures[:5]}")


if __name__ == "__main__":
    main()
