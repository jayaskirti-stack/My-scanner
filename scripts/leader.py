"""Daily Nifty 500 breakout and Screener.in fundamental shortlist.

Macro support and sector leadership require dated source evidence in
leader-reviews.json; they are never inferred from a price breakout alone.
"""
import csv
import io
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import quote
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import yfinance as yf
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.dirname(__file__))
UNIVERSE_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MarketLeaderResearch/1.0)", "Accept-Language": "en-IN,en;q=0.9"}


def constituents():
    request = Request(UNIVERSE_URL, headers={**HEADERS, "Referer": "https://www.niftyindices.com/"})
    with urlopen(request, timeout=40) as response:
        rows = csv.DictReader(io.StringIO(response.read().decode("utf-8-sig")))
        companies = {row["Symbol"].strip(): row.get("Industry", "").strip() for row in rows if row.get("Symbol") and not row["Symbol"].startswith("DUMMY")}
    if not 490 <= len(companies) <= 510:
        raise RuntimeError(f"Constituent list incomplete: {len(companies)}")
    return companies


def breakout(symbol):
    for attempt in range(2):
        try:
            bars = yf.Ticker(symbol + ".NS").history(period="18mo", interval="1d", auto_adjust=True, timeout=30).dropna(subset=["Close"])
            # Each of the last 20 sessions is compared with the preceding 252 closes.
            if len(bars) < 273:
                raise ValueError("fewer than 273 daily closes")
            closes = bars["Close"].astype(float).tolist()
            if any(not abs(x) < 1e9 or x <= 0 for x in closes):
                raise ValueError("invalid price history")
            for index in range(len(closes) - 20, len(closes)):
                high = max(closes[index - 252:index])
                if closes[index] > high and closes[index - 1] <= high and closes[-1] >= high:
                    return {"symbol": symbol, "close": round(closes[-1], 2), "date": bars.index[-1].date().isoformat(), "breakout_date": bars.index[index].date().isoformat(), "prior_high": round(high, 2)}, None
            return {"symbol": symbol, "date": bars.index[-1].date().isoformat()}, None
        except Exception as exc:
            if attempt == 0:
                time.sleep(2)
            else:
                return None, f"{symbol}: {str(exc)[:120]}"


def number(value):
    cleaned = re.sub(r"[^\d.\-]", "", value.replace(",", ""))
    return float(cleaned) if cleaned and cleaned not in ("-", ".") else None


def annual_table(soup, section_id):
    section = soup.select_one("#" + section_id)
    if section is None:
        raise ValueError(f"missing {section_id} table")
    table = section.select_one("table")
    if table is None:
        raise ValueError(f"missing {section_id} table")
    headers = [x.get_text(" ", strip=True) for x in table.select("thead th")]
    if not headers:
        headers = [x.get_text(" ", strip=True) for x in table.select("tr:first-child th")]
    result = {}
    for row in table.select("tbody tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if cells:
            result[cells[0].get_text(" ", strip=True).split("+")[0].strip()] = [number(x.get_text(" ", strip=True)) for x in cells[1:]]
    return headers, result


def parse_screener(html):
    soup = BeautifulSoup(html, "html.parser")
    ratio = soup.select_one("#top-ratios")
    if ratio is None:
        raise ValueError("Screener market cap missing")
    caps = [x.get_text(" ", strip=True) for x in ratio.select("li") if "Market Cap" in x.get_text(" ", strip=True)]
    cap = number(caps[0].split("Market Cap", 1)[1]) if caps else None
    pheaders, profit_rows = annual_table(soup, "profit-loss")
    bheaders, balance_rows = annual_table(soup, "balance-sheet")
    def annual(headers, rows, label, year):
        matches = [i for i, x in enumerate(headers[1:]) if year in x]
        if len(matches) != 1 or label not in rows or matches[0] >= len(rows[label]):
            raise ValueError(f"missing {label} for {year}")
        value = rows[label][matches[0]]
        if value is None:
            raise ValueError(f"blank {label} for {year}")
        return value
    p25 = annual(pheaders, profit_rows, "Net Profit", "Mar 2025")
    p26 = annual(pheaders, profit_rows, "Net Profit", "Mar 2026")
    equity = annual(bheaders, balance_rows, "Equity Capital", "Mar 2026") + annual(bheaders, balance_rows, "Reserves", "Mar 2026")
    debt = annual(bheaders, balance_rows, "Borrowings", "Mar 2026")
    if cap is None or equity <= 0 or p25 <= 0:
        raise ValueError("invalid market cap, equity or FY2025 profit")
    return {"market_cap_cr": round(cap, 2), "debt_equity": round(debt / equity, 3), "profit_2025_cr": p25, "profit_2026_cr": p26, "profit_growth_pct": round((p26 / p25 - 1) * 100, 1)}


def fundamentals(symbol):
    root = f"https://www.screener.in/company/{quote(symbol, safe='')}/"
    url = root + "consolidated/"
    for attempt in range(2):
        try:
            try:
                with urlopen(Request(url, headers=HEADERS), timeout=30) as response:
                    data = parse_screener(response.read().decode("utf-8"))
            except (ValueError, HTTPError):
                url = root
                with urlopen(Request(url, headers=HEADERS), timeout=30) as response:
                    data = parse_screener(response.read().decode("utf-8"))
            return data, url, None
        except Exception as exc:
            if attempt == 0:
                time.sleep(3)
            else:
                return None, url, f"{symbol}: {str(exc)[:100]}"


def main():
    universe = constituents()
    prices, errors = [], []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for future in as_completed([pool.submit(breakout, symbol) for symbol in universe]):
            item, error = future.result()
            if item:
                prices.append(item)
            if error:
                errors.append(error)
    if len(prices) < 450:
        raise RuntimeError(f"Price scan incomplete: {len(prices)}/{len(universe)}; {errors[:3]}")
    as_of = max(item["date"] for item in prices)
    candidates = [x for x in prices if x["date"] == as_of and "breakout_date" in x]
    with open(os.path.join(os.path.dirname(__file__), "leader-reviews.json"), encoding="utf-8") as source:
        reviews = json.load(source)
    screened, fundamentals_errors = [], []
    for item in candidates:
        data, url, error = fundamentals(item["symbol"])
        time.sleep(1)
        if error:
            fundamentals_errors.append(error)
            continue
        if 5000 <= data["market_cap_cr"] <= 25000 and data["debt_equity"] < 0.5 and data["profit_growth_pct"] > 25:
            review = reviews.get(item["symbol"], {})
            macro = review.get("macro", {})
            leader = review.get("sector_leader", {})
            for evidence in (macro, leader):
                if evidence.get("url") and not evidence["url"].startswith("https://"):
                    raise ValueError("Review evidence must use HTTPS")
            screened.append({**item, **data, "industry": universe[item["symbol"]], "screener_url": url,
                             "macro": macro, "sector_leader": leader,
                             "fully_reviewed": bool(macro.get("url") and macro.get("summary") and leader.get("url") and leader.get("summary"))})
    if candidates and len(fundamentals_errors) > max(3, len(candidates) // 4):
        raise RuntimeError(f"Screener fetch incomplete: {len(fundamentals_errors)}/{len(candidates)}; {fundamentals_errors[:3]}")
    payload = {"as_of": as_of, "generated_at": datetime.now(timezone.utc).isoformat(), "universe_count": len(universe),
               "price_scanned_count": len(prices), "breakout_count": len(candidates), "fundamental_checked_count": len(candidates) - len(fundamentals_errors),
               "fundamental_errors": fundamentals_errors[:20], "matches": sorted(screened, key=lambda x: (not x["fully_reviewed"], -x["profit_growth_pct"]))}
    with open(os.path.join(ROOT, "leader-results.json"), "w", encoding="utf-8") as output:
        json.dump(payload, output, indent=2, ensure_ascii=False)
        output.write("\n")
    print(f"Market Leader: {len(prices)}/{len(universe)} prices; {len(candidates)} recent breakouts; {len(screened)} fundamental matches; {sum(x['fully_reviewed'] for x in screened)} macro/leader reviews; {fundamentals_errors[:3]}")


if __name__ == "__main__":
    main()
