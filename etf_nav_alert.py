"""
ETF Price-Below-NAV / Day-Drop Alert Tool
------------------------------------------
Monitors all NSE-listed ETFs. For each ETF it checks TWO independent conditions:

    1. NAV discount:  LTP (Last Traded Price)  <  NAV
    2. Day drop:       today's % change  <=  -(your chosen drop %)

Either condition firing sends an alert via:
    - Desktop notification (Windows toast, if run on your PC)
    - Telegram message to your phone (works anywhere, no separate app needed)
    - Console table (visual list)
    - CSV log (etf_nav_log.csv) for history

WHY TELEGRAM FOR MOBILE:
There's no need to build a separate mobile app. A free Telegram bot can push
a message straight to your phone the instant this script detects a match.
Setup takes ~3 minutes (see TELEGRAM SETUP below).

WHY THIS IS A SCRIPT AND NOT A BROWSER APP:
NSE's data API (nseindia.com/api/etf) requires session cookies obtained by
first visiting nseindia.com, and blocks direct cross-origin browser requests
(CORS). A script running with the `requests` library has no such restriction.

SETUP (one-time):
    pip install requests plyer

TELEGRAM SETUP (one-time, ~3 min, gives you the mobile alerts):
    1. In Telegram, message @BotFather -> /newbot -> follow prompts.
       BotFather gives you a token like: 123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxx
    2. Send any message (e.g. "hi") to your new bot from your phone.
    3. Visit this URL in a browser (with YOUR token pasted in):
       https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
       Find "chat":{"id": 123456789 ...} in the response - that number is your chat ID.
    4. Fill TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID below, or pass them as
       --telegram-token and --telegram-chat-id on the command line.

RUNNING:
    python etf_nav_alert.py                                  # single check, then exit
    python etf_nav_alert.py --loop                            # every 5 min during market hours
    python etf_nav_alert.py --loop --interval 300 --threshold 0.5 --day-drop 2
    python etf_nav_alert.py --telegram-token XXXX --telegram-chat-id 123456789

RUNNING IT SO ALERTS REACH YOUR PHONE EVEN WHEN YOUR PC IS OFF:
Telegram alerts only fire while this script is actually running somewhere.
If you want alerts even when your PC is off, this same script (minus the
desktop-notification bit) can run on a free always-on host (PythonAnywhere
scheduled task, a Raspberry Pi, or a small cloud VM) on a cron/interval.
Happy to set that up if you want it - just say the word.
"""

import argparse
import csv
import datetime
import os
import sys
import time

import requests

try:
    from plyer import notification as desktop_notification
    HAS_PLYER = True
except ImportError:
    HAS_PLYER = False

# Fill these in once, OR leave blank and set as environment variables / GitHub
# Actions secrets named TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (recommended
# when running on GitHub Actions rather than your own PC).
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
# Multiple people: put a comma-separated list of chat IDs, e.g. "111111,222222"
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Only these ETF symbols will be checked. Leave empty to check ALL NSE ETFs.
# Use the exact NSE trading symbol, e.g. NIFTYBEES, GOLDBEES, BANKBEES.
# Can also be set via WATCHLIST env var (comma-separated) or --symbols on the CLI.
WATCHLIST = [s.strip().upper() for s in os.environ.get("WATCHLIST", "").split(",") if s.strip()]
print(f"[DEBUG] Raw WATCHLIST env value: '{os.environ.get('WATCHLIST', '')}'  ->  parsed: {WATCHLIST}")

# NOTE ON FIELD NAMES: NSE's JSON key names for LTP/NAV/day-change have shifted
# over the years. This script tries a list of known candidate names (below).
# If NSE changes their schema again, run with --debug once: it prints the raw
# keys of the first record so you can add the new key name in 30 seconds.

NSE_HOME_URL = "https://www.nseindia.com"
NSE_ETF_API_URL = "https://www.nseindia.com/api/etf"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/market-data/exchange-traded-funds-etf",
}

# Candidate JSON key names NSE has used at different times for each field.
SYMBOL_KEYS = ["symbol", "symbolCode", "meta_symbol"]
LTP_KEYS = ["ltP", "lastPrice", "ltp", "lastTradedPrice"]
NAV_KEYS = ["nav", "indicativeNav", "iNav", "navValue"]
NAME_KEYS = ["companyName", "meta_companyName", "name"]
CHANGE_PCT_KEYS = ["pChange", "perChange", "netPricePercChange", "changePercent", "percentChange"]
PREV_CLOSE_KEYS = ["prevClose", "previousClose", "pClose"]

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "etf_nav_log.csv")


def get_session() -> requests.Session:
    """Establish a session with NSE by visiting the homepage first (to get cookies)."""
    session = requests.Session()
    session.headers.update(HEADERS)
    # Hitting the homepage sets the cookies NSE's API expects.
    session.get(NSE_HOME_URL, timeout=10)
    return session


def fetch_etf_json(session: requests.Session) -> dict:
    resp = session.get(NSE_ETF_API_URL, timeout=10)
    resp.raise_for_status()
    return resp.json()


def first_present(record: dict, keys: list):
    for k in keys:
        if k in record and record[k] not in (None, "", "-"):
            return record[k]
    return None


def parse_rows(raw_json: dict, debug: bool = False) -> list:
    data = raw_json.get("data", [])
    if debug and data:
        print("DEBUG - raw keys in first record:")
        print(sorted(data[0].keys()))
        print()

    rows = []
    for rec in data:
        symbol = first_present(rec, SYMBOL_KEYS)
        ltp = first_present(rec, LTP_KEYS)
        nav = first_present(rec, NAV_KEYS)
        name = first_present(rec, NAME_KEYS) or ""
        prev_close_raw = first_present(rec, PREV_CLOSE_KEYS)
        day_change_raw = first_present(rec, CHANGE_PCT_KEYS)

        if symbol is None or ltp is None or nav is None:
            continue  # skip incomplete records rather than crash

        try:
            ltp = float(ltp)
            nav = float(nav)
        except (TypeError, ValueError):
            continue

        if nav == 0:
            continue

        # Prefer computing day-change % directly from prevClose (exact),
        # since NSE's own percent-change field names have shifted/been ambiguous.
        day_change_pct = None
        try:
            if prev_close_raw is not None:
                prev_close = float(prev_close_raw)
                if prev_close != 0:
                    day_change_pct = round((ltp - prev_close) / prev_close * 100, 3)
        except (TypeError, ValueError):
            day_change_pct = None

        if day_change_pct is None and day_change_raw is not None:
            try:
                day_change_pct = float(day_change_raw)
            except (TypeError, ValueError):
                day_change_pct = None

        discount_pct = round((ltp - nav) / nav * 100, 3)
        rows.append(
            {
                "symbol": symbol,
                "name": name,
                "ltp": ltp,
                "nav": nav,
                "discount_pct": discount_pct,
                "day_change_pct": day_change_pct,
            }
        )
    return rows


def notify_desktop(title: str, message: str):
    if HAS_PLYER:
        try:
            desktop_notification.notify(title=title, message=message, timeout=8)
            return
        except Exception:
            pass
    # Fallback: just print, so nothing is silently lost if plyer isn't installed.
    print(f"[NOTIFY] {title}: {message}")


def notify_telegram(token: str, chat_ids: str, message: str):
    """chat_ids can be a single ID or a comma-separated list, e.g. '111111,222222'."""
    if not token or not chat_ids:
        return
    ids = [c.strip() for c in str(chat_ids).split(",") if c.strip()]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chat_id in ids:
        try:
            requests.post(url, data={"chat_id": chat_id, "text": message}, timeout=10)
        except requests.RequestException as e:
            print(f"[Telegram send failed for {chat_id}] {e}")


def log_to_csv(rows: list, reason_key: str = "discount_pct"):
    file_exists = os.path.isfile(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["timestamp", "symbol", "name", "ltp", "nav", "discount_pct", "day_change_pct", "reason"])
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        for r in rows:
            writer.writerow(
                [ts, r["symbol"], r["name"], r["ltp"], r["nav"], r["discount_pct"], r.get("day_change_pct"), r.get("reason", "")]
            )


def run_once(
    threshold_pct: float,
    day_drop_pct: float,
    telegram_token: str = "",
    telegram_chat_id: str = "",
    watchlist: list = None,
    debug: bool = False,
):
    session = get_session()
    raw_json = fetch_etf_json(session)
    all_rows = parse_rows(raw_json, debug=debug)

    if watchlist:
        all_rows = [r for r in all_rows if r["symbol"].upper() in watchlist]

    below_nav = [dict(r, reason="below_nav") for r in all_rows if r["discount_pct"] <= -abs(threshold_pct)]

    day_drops = []
    if day_drop_pct > 0:
        day_drops = [
            dict(r, reason="day_drop")
            for r in all_rows
            if r["day_change_pct"] is not None and r["day_change_pct"] <= -abs(day_drop_pct)
        ]

    alerts = below_nav + day_drops
    alerts.sort(key=lambda r: r["discount_pct"])

    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n=== ETF Check @ {ts} ===")
    watch_note = f" (watchlist: {len(watchlist)} symbols)" if watchlist else " (all NSE ETFs)"
    print(
        f"Scanned: {len(all_rows)}{watch_note} | Below NAV (>= {threshold_pct}% discount): {len(below_nav)} "
        f"| Down >= {day_drop_pct}% today: {len(day_drops)}"
    )

    if alerts:
        print(f"{'Symbol':<15}{'LTP':>10}{'NAV':>10}{'Disc %':>10}{'Day %':>10}{'Reason':>12}")
        for r in alerts:
            day_str = f"{r['day_change_pct']:.2f}" if r["day_change_pct"] is not None else "n/a"
            print(f"{r['symbol']:<15}{r['ltp']:>10.2f}{r['nav']:>10.2f}{r['discount_pct']:>10.2f}{day_str:>10}{r['reason']:>12}")

            if r["reason"] == "below_nav":
                title = f"{r['symbol']} below NAV ({r['discount_pct']}%)"
                message = f"LTP {r['ltp']} vs NAV {r['nav']}"
            else:
                title = f"{r['symbol']} down {r['day_change_pct']}% today"
                message = f"LTP {r['ltp']} (NAV {r['nav']})"

            notify_desktop(title=title, message=message)
            notify_telegram(telegram_token, telegram_chat_id, f"{title}\n{message}")

        log_to_csv(alerts)
    else:
        print("No alerts triggered.")

    return alerts


def market_is_open(now: datetime.datetime) -> bool:
    if now.weekday() >= 5:  # Sat/Sun
        return False
    open_t = now.replace(hour=9, minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_t <= now <= close_t


def main():
    parser = argparse.ArgumentParser(description="Alert when NSE ETFs trade below NAV")
    parser.add_argument("--loop", action="store_true", help="Keep checking repeatedly during market hours")
    parser.add_argument("--interval", type=int, default=300, help="Seconds between checks in loop mode (default 300)")
    parser.add_argument("--threshold", type=float, default=0.0, help="Minimum discount %% to trigger NAV alert (default 0 = any discount)")
    parser.add_argument("--day-drop", type=float, default=0.0, help="Alert if an ETF is down this %% or more today (default 0 = disabled)")
    parser.add_argument("--telegram-token", type=str, default=TELEGRAM_BOT_TOKEN, help="Telegram bot token for mobile alerts")
    parser.add_argument("--telegram-chat-id", type=str, default=TELEGRAM_CHAT_ID, help="Telegram chat ID(s), comma-separated for multiple people")
    parser.add_argument("--symbols", type=str, default="", help="Comma-separated NSE symbols to watch, e.g. NIFTYBEES,GOLDBEES (default: all ETFs)")
    parser.add_argument("--debug", action="store_true", help="Print raw NSE field names once, for troubleshooting")
    args = parser.parse_args()

    watchlist = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or WATCHLIST

    if not args.loop:
        run_once(args.threshold, args.day_drop, args.telegram_token, args.telegram_chat_id, watchlist=watchlist, debug=args.debug)
        return

    print("Running in loop mode. Ctrl+C to stop.")
    try:
        while True:
            now = datetime.datetime.now()
            if market_is_open(now):
                try:
                    run_once(args.threshold, args.day_drop, args.telegram_token, args.telegram_chat_id, watchlist=watchlist, debug=args.debug)
                except requests.RequestException as e:
                    print(f"Network/NSE error: {e}")
            else:
                print(f"Market closed at {now.strftime('%H:%M:%S')} - skipping check.")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    sys.exit(main())
