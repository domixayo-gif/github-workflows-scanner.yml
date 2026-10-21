
import csv
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.dexscreener.com"

STATE_FILE = Path("paper_state.json")
TRADES_FILE = Path("paper_trades.csv")

# NEW POSITION SETTINGS
POSITION_USD = 20.0
MAX_OPEN_POSITIONS = 3

# PROFIT AND RISK SETTINGS
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

# SIMULATION ASSUMPTION, NOT AN ACTUAL FEE QUOTE
ESTIMATED_COST_PERCENT = 4.0

MAX_ADDRESSES_PER_SCAN = 30
REJECT_COOLDOWN_SECONDS = 6 * 60 * 60
API_FAILURE_COOLDOWN_SECONDS = 15 *  60

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


def utc_now():
    return datetime.now(timezone.utc)


def today_key():
    return utc_now().strftime("%Y-%m-%d")


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
    temp_file = STATE_FILE.with_suffix(".tmp")

    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)

    temp_file.replace(STATE_FILE)


def ensure_trade_file():
    if not TRADES_FILE.exists() or TRADES_FILE.stat().st_size == 0:
        with open(TRADES_FILE, "w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
            writer.writeheader()


def load_state():
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as file:
                state = json.load(file)
        except (json.JSONDecodeError, OSError):
            log("WARNING: State file could not be read.")
            log("Starting empty state; existing trade CSV is preserved.")
            state = {}
    else:
        state = {}

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


def recalculate_daily_pnl(state):
    """
    Rebuild today's totals from the trade CSV.
    This repairs missing or incorrect daily totals in saved state.
    """
    gross_total = 0.0
    net_total = 0.0
    key = today_key()

    ensure_trade_file()

    try:
        with open(TRADES_FILE, "r", newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)

            for row in reader:
                timestamp = row.get("time_utc", "")

                try:
                    trade_date = datetime.fromisoformat(
                        timestamp.replace("Z", "+00:00")
                    ).astimezone(timezone.utc).strftime("%Y-%m-%d")

                    if trade_date != key:
                        continue

                    gross_total += float(row.get("gross_pnl_usd") or 0)
                    net_total += float(
                        row.get("estimated_net_pnl_usd") or 0
                    )

                except (ValueError, TypeError):
                    log("WARNING: Skipped an invalid trade CSV row.")

    except OSError as error:
        log("WARNING: Could not read trade history: " + str(error))

    state["daily_key"] = key
    state["daily_realized_gross_usd"] = round(gross_total, 4)
    state["daily_realized_net_usd"] = round(net_total, 4)

    save_state(state)

    log("Daily gross P/L: $" + str(round(gross_total, 4)))
    log("Daily estimated net P/L: $" + str(round(net_total, 4)))


def mark_checked(state, address, cooldown=None):
    if cooldown is None:
        cooldown = REJECT_COOLDOWN_SECONDS

    state["checked_tokens"][address] = time.time() + cooldown


def already_checked(state, address):
    expiry = state["checked_tokens"].get(address)

    if expiry is None:
        return False

    try:
        expiry = float(expiry)
    except (ValueError, TypeError):
        return False

    if expiry <= time.time():
        del state["checked_tokens"][address]
        return False

    return True


def fetch_latest_solana_addresses():
    url = API + "/token-profiles/latest/v1"
    data = request_json(url)

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
    pairs_by_address = {}

    if not addresses:
        return pairs_by_address

    # DexScreener supports multiple token addresses in one request.
    url = API + "/latest/dex/tokens/" + ",".join(addresses)
    data = request_json(url)
    pairs = data.get("pairs", []) if isinstance(data, dict) else []

    for pair in pairs:
        if not isinstance(pair, dict):
            continue

        if pair.get("chainId") != "solana":
            continue

        base = pair.get("baseToken") or {}
        address = base.get("address")

        if not address:
            continue

        if address not in addresses:
            continue

        if address not in pairs_by_address:
            pairs_by_address[address] = []

        pairs_by_address[address].append(pair)

    return pairs_by_address


def best_pair(pairs):
    if not pairs:
        return None

    def liquidity_value(pair):
        liquidity = pair.get("liquidity") or {}
        try:
            return float(liquidity.get("usd") or 0)
        except (ValueError, TypeError):
            return 0.0

    return max(pairs, key=liquidity_value)


def validate_pair(pair):
    if not pair:
        return False, "NO USABLE SOLANA PAIR"

    try:
        liquidity = float(
            (pair.get("liquidity") or {}).get("usd") or 0
        )
        volume_5m = float(
            (pair.get("volume") or {}).get("m5") or 0
        )
        txns = (pair.get("txns") or {}).get("m5") or {}
        buys = int(txns.get("buys") or 0)
        sells = int(txns.get("sells") or 0)
        price_change = float(
            (pair.get("priceChange") or {}).get("m5") or 0
        )
        price = float(pair.get("priceUsd") or 0)
    except (ValueError, TypeError):
        return False, "INVALID MARKET DATA"

    if price <= 0:
        return False, "INVALID PRICE"

    if liquidity < MIN_LIQUIDITY_USD:
        return False, "LOW LIQUIDITY: $" + str(round(liquidity))

    if volume_5m < MIN_VOLUME_5M_USD:
        return False, "LOW 5M VOLUME: $" + str(round(volume_5m))

    if buys + sells < MIN_TXNS_5M:
        return False, "LOW 5M TRANSACTION COUNT"

    if buys <= sells:
        return False, "SELLS NOT OUTNUMBERED BY BUYS"

    if abs(price_change) > MAX_ABS_PRICE_CHANGE_5M:
        return False, "EXTREME 5M PRICE CHANGE"

    created = pair.get("pairCreatedAt")

    if created:
        try:
            age_hours = (time.time() * 1000 - float(created)) / 3600000

            if age_hours < 0 or age_hours > MAX_PAIR_AGE_HOURS:
                return False, "PAIR AGE OUTSIDE LIMIT"
        except (ValueError, TypeError):
            return False, "INVALID PAIR AGE"

    return True, "OK"


def close_position(state, address, pair, reason):
    position = state["open_positions"].get(address)

    if not position or not pair:
        return

    try:
        entry_price = float(position["entry_price"])
        exit_price = float(pair.get("priceUsd") or 0)
        position_usd = float(position["position_usd"])
    except (ValueError, TypeError, KeyError):
        log("WARNING: Cannot calculate position: " + address)
        return

    if entry_price <= 0 or exit_price <= 0:
        return

    gross_pnl = position_usd * (exit_price / entry_price - 1.0)
    estimated_cost = position_usd * ESTIMATED_COST_PERCENT / 100.0
    net_pnl = gross_pnl - estimated_cost

    row = {
        "time_utc": utc_now().isoformat(),
        "token_address": address,
        "symbol": position.get("symbol", "UNKNOWN"),
        "entry_price": entry_price,
        "exit_price": exit_price,
        "position_usd": position_usd,
        "gross_pnl_usd": round(gross_pnl, 4),
        "estimated_cost_usd": round(estimated_cost, 4),
        "estimated_net_pnl_usd": round(net_pnl, 4),
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
        + " | gross: $" + str(round(gross_pnl, 4))
        + " | estimated net: $" + str(round(net_pnl, 4))
    )


def manage_positions(state):
    positions = state["open_positions"]

    if not positions:
        return

    addresses = list(positions.keys())

    try:
        pairs_by_address = fetch_solana_pairs(addresses)
    except Exception as error:
        log("WARNING: Position price update failed: " + str(error))
        return

    for address in list(positions.keys()):
        position = positions.get(address)

        if not position:
            continue

        pair = best_pair(pairs_by_address.get(address, []))

        if not pair:
            log("No current pair price for open position: " + address)
            continue

        try:
            entry_price = float(position["entry_price"])
            current_price = float(pair.get("priceUsd") or 0)
            position_usd = float(position["position_usd"])
            entry_time = float(position["entry_time"])
        except (ValueError, TypeError, KeyError):
            log("WARNING: Invalid open position data: " + address)
            continue

        if entry_price <= 0 or current_price <= 0:
            continue

        price_change_percent = (
            current_price / entry_price - 1.0
        ) * 100.0

        gross_pnl = position_usd * (
            current_price / entry_price - 1.0
        )
        estimated_cost = position_usd * ESTIMATED_COST_PERCENT / 100.0
        estimated_net = gross_pnl - estimated_cost

        age_minutes = (time.time() - entry_time) / 60.0

        log(
            "POSITION: " + str(position.get("symbol", "UNKNOWN"))
            + " | change: " + str(round(price_change_percent, 2)) + "%"
            + " | estimated net: $" + str(round(estimated_net, 4))
        )

        if estimated_net >= TAKE_PROFIT_USD:
            close_position(state, address, pair, "TAKE_PROFIT_USD")

        elif price_change_percent <= -STOP_LOSS_PERCENT:
            close_position(state, address, pair, "STOP_LOSS")

        elif age_minutes >= MAX_HOLD_MINUTES:
            close_position(state, address, pair, "TIME_LIMIT")


def open_position(state, address, pair):
    base = pair.get("baseToken") or {}
    symbol = base.get("symbol") or "UNKNOWN"

    try:
        price = float(pair.get("priceUsd") or 0)
    except (ValueError, TypeError):
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
    open_count = len(state["open_positions"])

    if open_count >= MAX_OPEN_POSITIONS:
        log("MAXIMUM POSITIONS REACHED.")
        return

    daily_net = float(state.get("daily_realized_net_usd", 0.0))

    if daily_net <= -DAILY_LOSS_LIMIT_USD:
        log(
            "DAILY LOSS LIMIT REACHED: $"
            + str(round(daily_net, 4))
            + ". No new positions will be opened."
        )
        return

    try:
        addresses = fetch_latest_solana_addresses()
    except Exception as error:
        log("ERROR: Could not discover tokens: " + str(error))
        return

    log("Solana addresses selected: " + str(len(addresses)))

    addresses_to_check = []

    for address in addresses:
        if address in state["open_positions"]:
            continue

        if address in state["seen_tokens"]:
            continue

        if already_checked(state, address):
            continue

        addresses_to_check.append(address)

    if not addresses_to_check:
        log("No new addresses to check.")
        return

    try:
        pairs_by_address = fetch_solana_pairs(addresses_to_check)
    except Exception as error:
        log("ERROR: Pair lookup failed: " + str(error))

        for address in addresses_to_check:
            mark_checked(
                state,
                address,
                API_FAILURE_COOLDOWN_SECONDS,
            )

        save_state(state)
        return

    pairs_checked = 0
    rejected = 0
    opened = 0

    for address in addresses_to_check:
        if len(state["open_positions"]) >= MAX_OPEN_POSITIONS:
            log("MAXIMUM POSITIONS REACHED.")
            break

        # Re-check the daily limit before every new entry.
        daily_net = float(state.get("daily_realized_net_usd", 0.0))

        if daily_net <= -DAILY_LOSS_LIMIT_USD:
            log("DAILY LOSS LIMIT REACHED. Blocking new entries.")
            break

        pair = best_pair(pairs_by_address.get(address, []))

        if not pair:
            log("NO USABLE SOLANA PAIR: " + address)
            mark_checked(state, address)
            rejected += 1
            continue

        pairs_checked += 1
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
    log("Pairs checked: " + str(pairs_checked))
    log("Candidates rejected: " + str(rejected))
    log("New simulated positions opened: " + str(opened))
    log("Total open positions: " + str(len(state["open_positions"])))


def main():
    log("=" * 45)
    log("SOLANA MEME COIN PAPER BOT")
    log("UTC: " + utc_now().isoformat())
    log("MODE: PAPER TRADING ONLY")
    log("New simulated position: $" + str(POSITION_USD))
    log("Take-profit target per trade: $" + str(TAKE_PROFIT_USD))
    log("Daily net-loss limit: $" + str(DAILY_LOSS_LIMIT_USD))
    log("=" * 45)

    ensure_trade_file()
    state = load_state()

    log("Open positions: " + str(len(state["open_positions"])))
    log("Previously opened tokens: " + str(len(state["seen_tokens"])))

    # Always manage existing positions first.
    manage_positions(state)

    # Rebuild today's totals after any positions that just closed.
    recalculate_daily_pnl(state)

    # Only scan for new entries if the daily limit allows it.
    scan_for_entries(state)

    # Final totals and state save.
    recalculate_daily_pnl(state)
    save_state(state)

    log("FINAL OPEN POSITIONS: " + str(len(state["open_positions"])))
    log("Daily gross P/L: $" + str(state["daily_realized_gross_usd"]))
    log("Daily estimated net P/L: $" + str(state["daily_realized_net_usd"]))
    log("Run complete.")


if __name__ == "__main__":
    main()
