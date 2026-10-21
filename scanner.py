
import csv
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.dexscreener.com"
STATE_FILE = Path("paper_state.json")
TRADES_FILE = Path("paper_trades.csv")

# NEW PAPER POSITIONS
POSITION_USD = 20.0
MAX_OPEN_POSITIONS = 3

# PROFIT AND RISK
TAKE_PROFIT_USD = 10.0
STOP_LOSS_PERCENT = 4.0
DAILY_LOSS_LIMIT_USD = 5.0
MAX_HOLD_MINUTES = 60

# MARKET FILTERS
MIN_LIQUIDITY_USD = 15000
MIN_VOLUME_5M_USD = 10000
MIN_TXNS_5M = 20
MAX_PAIR_AGE_HOURS = 48
MAX_ABS_PRICE_CHANGE_5M = 20.0
MAX_ADDRESSES_PER_SCAN = 30

# Simulation estimate only, not a verified fee.
ESTIMATED_COST_PERCENT = 4.0

REJECT_COOLDOWN_SECONDS = 6 * 60 * 60
API_FAILURE_COOLDOWN_SECONDS = 15 * 60

CSV_FIELDS = [
    "time_utc",
    "token_address",
    "symbol",
    "entry_price",
    "exit_price",
    "position_usd",
    "gross_pnl_usd",
    "estimated_cost_usd",
    "estimated_net_pnl_usd",
    "reason",
]


def now_utc():
    return datetime.now(timezone.utc)


def today_key():
    return now_utc().strftime("%Y-%m-%d")


def log(message):
    print(message, flush=True)


def request_json(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def save_state(state):
    temp = STATE_FILE.with_suffix(".tmp")
    with open(temp, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)
    temp.replace(STATE_FILE)


def ensure_trade_file():
    if not TRADES_FILE.exists() or TRADES_FILE.stat().st_size == 0:
        with open(TRADES_FILE, "w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
            writer.writeheader()


def load_state():
    state = {}

    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as file:
                state = json.load(file)
        except (OSError, json.JSONDecodeError) as error:
            log("WARNING: Could not load state: " + str(error))
            log("Starting empty state. Trade history is preserved.")

    if not isinstance(state.get("open_positions"), dict):
        state["open_positions"] = {}

    if not isinstance(state.get("seen_tokens"), list):
        state["seen_tokens"] = []

    if not isinstance(state.get("checked_tokens"), dict):
        state["checked_tokens"] = {}

    state.setdefault("daily_realized_gross_usd", 0.0)
    state.setdefault("daily_realized_net_usd", 0.0)
    state.setdefault("daily_key", today_key())

    return state


def read_trade_rows():
    """
    Read CSV by locating column names, not fixed column positions.
    Tolerates a BOM, blank leading columns, and extra columns.
    """
    ensure_trade_file()
    rows = []

    with open(
        TRADES_FILE, "r", newline="", encoding="utf-8-sig"
    ) as file:
        reader = csv.reader(file)

        header = None
        indexes = {}

        for row in reader:
            cleaned = [cell.strip() for cell in row]

            if not cleaned or not any(cleaned):
                continue

            normalized = [cell.lower() for cell in cleaned]

            if "time_utc" in normalized and "gross_pnl_usd" in normalized:
                header = normalized
                indexes = {
                    name: header.index(name)
                    for name in CSV_FIELDS
                    if name in header
                }
                continue

            if header is None:
                continue

            # Ignore blank leading columns and align values to header.
            if len(cleaned) < len(header):
                cleaned += [""] * (len(header) - len(cleaned))

            record = {}
            for name, index in indexes.items():
                if index < len(cleaned):
                    record[name] = cleaned[index]
                else:
                    record[name] = ""

            rows.append(record)

    return rows


def recalculate_daily_pnl(state):
    gross_total = 0.0
    net_total = 0.0
    key = today_key()
    valid_rows = 0

    try:
        rows = read_trade_rows()

        for row in rows:
            timestamp = row.get("time_utc", "").strip()

            try:
                parsed = datetime.fromisoformat(
                    timestamp.replace("Z", "+00:00")
                )

                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)

                trade_date = parsed.astimezone(
                    timezone.utc
                ).strftime("%Y-%m-%d")

                if trade_date != key:
                    continue

                gross = float(row.get("gross_pnl_usd", ""))
                net = float(row.get("estimated_net_pnl_usd", ""))

                gross_total += gross
                net_total += net
                valid_rows += 1

            except (ValueError, TypeError):
                log("WARNING: Skipped invalid trade row.")

    except (OSError, csv.Error) as error:
        log("ERROR: Could not read trade history: " + str(error))
        return False

    state["daily_key"] = key
    state["daily_realized_gross_usd"] = round(gross_total, 4)
    state["daily_realized_net_usd"] = round(net_total, 4)
    save_state(state)

    log("Today's valid closed trades: " + str(valid_rows))
    log("Daily gross P/L: $" + str(round(gross_total, 4)))
    log("Daily estimated net P/L: $" + str(round(net_total, 4)))

    return True


def mark_checked(state, address, seconds=None):
    if seconds is None:
        seconds = REJECT_COOLDOWN_SECONDS
    state["checked_tokens"][address] = time.time() + seconds


def already_checked(state, address):
    expiry = state["checked_tokens"].get(address)

    if expiry is None:
        return False

    try:
        expiry = float(expiry)
    except (TypeError, ValueError):
        return False

    if expiry <= time.time():
        del state["checked_tokens"][address]
        return False

    return True


def fetch_latest_solana_addresses():
    data = request_json(API + "/token-profiles/latest/v1")
    addresses = []
    seen = set()

    if not isinstance(data, list):
        return addresses

    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("chainId") != "solana":
            continue

        address = item.get("tokenAddress")
        if not address or address in seen:
            continue

        seen.add(address)
        addresses.append(address)

        if len(addresses) >= MAX_ADDRESSES_PER_SCAN:
            break

    return addresses


def fetch_solana_pairs(addresses):
    result = {}

    if not addresses:
        return result

    url = API + "/latest/dex/tokens/" + ",".join(addresses)
    data = request_json(url)
    pairs = data.get("pairs", []) if isinstance(data, dict) else []

    wanted = set(addresses)

    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        if pair.get("chainId") != "solana":
            continue

        base = pair.get("baseToken") or {}
        address = base.get("address")

        if address not in wanted:
            continue

        result.setdefault(address, []).append(pair)

    return result


def best_pair(pairs):
    if not pairs:
        return None

    def liquidity(pair):
        try:
            return float(
                (pair.get("liquidity") or {}).get("usd") or 0
            )
        except (TypeError, ValueError):
            return 0.0

    return max(pairs, key=liquidity)


def validate_pair(pair):
    if not pair:
        return False, "NO USABLE SOLANA PAIR"

    try:
        liquidity = float(
            (pair.get("liquidity") or {}).get("usd") or 0
        )
        volume = float(
            (pair.get("volume") or {}).get("m5") or 0
        )
        txns = (pair.get("txns") or {}).get("m5") or {}
        buys = int(txns.get("buys") or 0)
        sells = int(txns.get("sells") or 0)
        change = float(
            (pair.get("priceChange") or {}).get("m5") or 0
        )
        price = float(pair.get("priceUsd") or 0)
    except (TypeError, ValueError):
        return False, "INVALID MARKET DATA"

    if price <= 0:
        return False, "INVALID PRICE"
    if liquidity < MIN_LIQUIDITY_USD:
        return False, "LOW LIQUIDITY: $" + str(round(liquidity))
    if volume < MIN_VOLUME_5M_USD:
        return False, "LOW 5M VOLUME: $" + str(round(volume))
    if buys + sells < MIN_TXNS_5M:
        return False, "LOW 5M TRANSACTION COUNT"
    if buys <= sells:
        return False, "BUYS DO NOT EXCEED SELLS"
    if abs(change) > MAX_ABS_PRICE_CHANGE_5M:
        return False, "EXTREME 5M PRICE CHANGE"

    created = pair.get("pairCreatedAt")

    if created:
        try:
            age_hours = (
                time.time() * 1000 - float(created)
            ) / 3600000

            if age_hours < 0 or age_hours > MAX_PAIR_AGE_HOURS:
                return False, "PAIR AGE OUTSIDE LIMIT"
        except (TypeError, ValueError):
            return False, "INVALID PAIR AGE"

    return True, "OK"


def close_position(state, address, pair, reason):
    position = state["open_positions"].get(address)

    if not position or not pair:
        return

    try:
        entry = float(position["entry_price"])
        exit_price = float(pair.get("priceUsd") or 0)
        amount = float(position["position_usd"])
    except (KeyError, TypeError, ValueError):
        log("WARNING: Invalid position data: " + address)
        return

    if entry <= 0 or exit_price <= 0:
        return

    gross = amount * (exit_price / entry - 1.0)
    cost = amount * ESTIMATED_COST_PERCENT / 100.0
    net = gross - cost

    row = {
        "time_utc": now_utc().isoformat(),
        "token_address": address,
        "symbol": position.get("symbol", "UNKNOWN"),
        "entry_price": entry,
        "exit_price": exit_price,
        "position_usd": amount,
        "gross_pnl_usd": round(gross, 4),
        "estimated_cost_usd": round(cost, 4),
        "estimated_net_pnl_usd": round(net, 4),
        "reason": reason,
    }

    ensure_trade_file()

    with open(TRADES_FILE, "a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
        writer.writerow(row)

    del state["open_positions"][address]
    save_state(state)

    log(
        "PAPER CLOSE: " + str(position.get("symbol", "UNKNOWN"))
        + " | reason: " + reason
        + " | gross: $" + str(round(gross, 4))
        + " | estimated net: $" + str(round(net, 4))
    )


def manage_positions(state):
    addresses = list(state["open_positions"].keys())

    if not addresses:
        return

    try:
        pairs_by_address = fetch_solana_pairs(addresses)
    except Exception as error:
        log("WARNING: Could not update positions: " + str(error))
        return

    for address in addresses:
        position = state["open_positions"].get(address)
        if not position:
            continue

        pair = best_pair(pairs_by_address.get(address, []))

        if not pair:
            log("No current price for: " + address)
            continue

        try:
            entry = float(position["entry_price"])
            price = float(pair.get("priceUsd") or 0)
            amount = float(position["position_usd"])
            opened = float(position["entry_time"])
        except (KeyError, TypeError, ValueError):
            log("WARNING: Invalid open position: " + address)
            continue

        if entry <= 0 or price <= 0:
            continue

        change_pct = (price / entry - 1.0) * 100.0
        gross = amount * (price / entry - 1.0)
        cost = amount * ESTIMATED_COST_PERCENT / 100.0
        net = gross - cost
        age_minutes = (time.time() - opened) / 60.0

        log(
            "POSITION: " + str(position.get("symbol", "UNKNOWN"))
            + " | change: " + str(round(change_pct, 2)) + "%"
            + " | estimated net: $" + str(round(net, 4))
        )

        if net >= TAKE_PROFIT_USD:
            close_position(state, address, pair, "TAKE_PROFIT_USD")
        elif change_pct <= -STOP_LOSS_PERCENT:
            close_position(state, address, pair, "STOP_LOSS")
        elif age_minutes >= MAX_HOLD_MINUTES:
            close_position(state, address, pair, "TIME_LIMIT")


def open_position(state, address, pair):
    base = pair.get("baseToken") or {}
    symbol = base.get("symbol") or "UNKNOWN"

    try:
        price = float(pair.get("priceUsd") or 0)
    except (TypeError, ValueError):
        return False

    if price <= 0:
        return False

    state["open_positions"][address] = {
        "symbol": symbol,
        "entry_price": price,
        "entry_time": time.time(),
        "position_usd": POSITION_USD,
    }

    if address not in state["seen_tokens"]:
        state["seen_tokens"].append(address)

    mark_checked(state, address)
    save_state(state)

    log(
        "PAPER OPEN: " + symbol
        + " | entry: " + str(price)
        + " | simulated amount: $" + str(POSITION_USD)
    )
    return True


def scan_for_entries(state):
    if len(state["open_positions"]) >= MAX_OPEN_POSITIONS:
        log("MAXIMUM POSITIONS REACHED.")
        return

    daily_net = float(state.get("daily_realized_net_usd", 0))

    if daily_net <= -DAILY_LOSS_LIMIT_USD:
        log(
            "DAILY LOSS LIMIT REACHED: $"
            + str(round(daily_net, 4))
            + ". No new positions."
        )
        return

    try:
        addresses = fetch_latest_solana_addresses()
    except Exception as error:
        log("ERROR: Token discovery failed: " + str(error))
        return

    log("Solana addresses selected: " + str(len(addresses)))

    candidates = []

    for address in addresses:
        if address in state["open_positions"]:
            continue
        if address in state["seen_tokens"]:
            continue
        if already_checked(state, address):
            continue
        candidates.append(address)

    if not candidates:
        log("No new addresses to check.")
        return

    try:
        pairs_by_address = fetch_solana_pairs(candidates)
    except Exception as error:
        log("ERROR: Pair lookup failed: " + str(error))

        for address in candidates:
            mark_checked(
                state, address, API_FAILURE_COOLDOWN_SECONDS
            )

        save_state(state)
        return

    checked = 0
    rejected = 0
    opened = 0

    for address in candidates:
        if len(state["open_positions"]) >= MAX_OPEN_POSITIONS:
            log("MAXIMUM POSITIONS REACHED.")
            break

        daily_net = float(state.get("daily_realized_net_usd", 0))
        if daily_net <= -DAILY_LOSS_LIMIT_USD:
            log("DAILY LOSS LIMIT REACHED. Blocking new entries.")
            break

        pair = best_pair(pairs_by_address.get(address, []))

        if not pair:
            log("NO USABLE SOLANA PAIR: " + address)
            mark_checked(state, address)
            rejected += 1
            continue

        checked += 1
        valid, reason = validate_pair(pair)

        if not valid:
            symbol = (pair.get("baseToken") or {}).get(
                "symbol", "UNKNOWN"
            )
            log("REJECT: " + symbol + " | " + reason)
            mark_checked(state, address)
            rejected += 1
            continue

        if open_position(state, address, pair):
            opened += 1

    save_state(state)
    log("SCAN SUMMARY")
    log("Addresses discovered: " + str(len(addresses)))
    log("Pairs checked: " + str(checked))
    log("Candidates rejected: " + str(rejected))
    log("New simulated positions opened: " + str(opened))
    log("Total open positions: " + str(len(state["open_positions"])))


def main():
    log("=" * 45)
    log("SOLANA MEME COIN PAPER BOT")
    log("UTC: " + now_utc().isoformat())
    log("MODE: PAPER TRADING ONLY")
    log("New simulated position: $" + str(POSITION_USD))
    log("Take-profit per trade: $" + str(TAKE_PROFIT_USD))
    log("Daily net-loss limit: $" + str(DAILY_LOSS_LIMIT_USD))
    log("=" * 45)

    ensure_trade_file()
    state = load_state()

    log("Open positions: " + str(len(state["open_positions"])))
    log("Previously opened tokens: " + str(len(state["seen_tokens"])))

    # Manage existing positions before checking new entries.
    manage_positions(state)

    # Rebuild P/L from the CSV after any closures.
    if not recalculate_daily_pnl(state):
        log("Accounting failed. Blocking new entries for safety.")
        return

    scan_for_entries(state)

    recalculate_daily_pnl(state)
    save_state(state)

    log("FINAL OPEN POSITIONS: " + str(len(state["open_positions"])))
    log("Daily gross P/L: $" + str(state["daily_realized_gross_usd"]))
    log("Daily estimated net P/L: $" + str(state["daily_realized_net_usd"]))
    log("Run complete.")


if __name__ == "__main__":
    main()
