
import csv
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.dexscreener.com"
STATE_FILE = Path("paper_state.json")
TRADES_FILE = Path("paper_trades.csv")

# PAPER TRADING ONLY
POSITION_USD = 20.0
MAX_OPEN_POSITIONS = 3
TAKE_PROFIT_USD = 10.0
STOP_LOSS_PERCENT = 4.0
DAILY_LOSS_LIMIT_USD = 5.0
MAX_HOLD_MINUTES = 60

# NEW-LAUNCH MOMENTUM FILTERS
MAX_PAIR_AGE_HOURS = 6
MIN_LIQUIDITY_USD = 15000
MIN_VOLUME_5M_USD = 10000
MIN_TXNS_5M = 20
MIN_BUY_RATIO = 1.3
MIN_PRICE_CHANGE_5M = 1.0
MAX_PRICE_CHANGE_5M = 20.0
MAX_TOKENS_PER_SCAN = 30

# Simulation estimate only, not actual trading fees.
ESTIMATED_COST_PERCENT = 4.0
REJECT_COOLDOWN = 6 * 60 * 60
API_COOLDOWN = 15 * 60

CSV_FIELDS = [
    "time_utc", "token_address", "symbol",
    "entry_price", "exit_price", "position_usd",
    "gross_pnl_usd", "estimated_cost_usd",
    "estimated_net_pnl_usd", "reason"
]


def log(message):
    print(message, flush=True)


def now():
    return datetime.now(timezone.utc)


def today():
    return now().strftime("%Y-%m-%d")


def request_json(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json"
        }
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def save_state(state):
    temp = STATE_FILE.with_suffix(".tmp")
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    temp.replace(STATE_FILE)


def load_state():
    state = {}
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError):
            log("WARNING: State could not be read.")

    if not isinstance(state.get("open_positions"), dict):
        state["open_positions"] = {}
    if not isinstance(state.get("seen_tokens"), list):
        state["seen_tokens"] = []
    if not isinstance(state.get("checked_tokens"), dict):
        state["checked_tokens"] = {}

    state.setdefault("daily_realized_gross_usd", 0.0)
    state.setdefault("daily_realized_net_usd", 0.0)
    state.setdefault("daily_key", today())
    return state


def ensure_csv():
    if not TRADES_FILE.exists() or TRADES_FILE.stat().st_size == 0:
        with open(TRADES_FILE, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=CSV_FIELDS).writeheader()


def read_trades():
    ensure_csv()
    rows = []
    with open(TRADES_FILE, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("time_utc") and row.get("gross_pnl_usd") is not None:
                rows.append(row)
    return rows


def recalculate_pnl(state):
    gross_total = 0.0
    net_total = 0.0

    try:
        for row in read_trades():
            stamp = row.get("time_utc", "")
            try:
                parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                if parsed.astimezone(timezone.utc).strftime("%Y-%m-%d") != today():
                    continue
                gross_total += float(row["gross_pnl_usd"])
                net_total += float(row["estimated_net_pnl_usd"])
            except (ValueError, TypeError, KeyError):
                log("WARNING: Skipped invalid historical trade row.")
    except (OSError, csv.Error) as error:
        log("ACCOUNTING ERROR: " + str(error))
        return False

    state["daily_key"] = today()
    state["daily_realized_gross_usd"] = round(gross_total, 4)
    state["daily_realized_net_usd"] = round(net_total, 4)
    save_state(state)
    log("Daily gross P/L: $" + str(round(gross_total, 4)))
    log("Daily estimated net P/L: $" + str(round(net_total, 4)))
    return True


def discover_tokens():
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
        if address and address not in seen:
            seen.add(address)
            addresses.append(address)
        if len(addresses) >= MAX_TOKENS_PER_SCAN:
            break

    return addresses


def fetch_pairs(addresses):
    result = {}
    if not addresses:
        return result

    url = API + "/latest/dex/tokens/" + ",".join(addresses)
    data = request_json(url)
    pairs = data.get("pairs", []) if isinstance(data, dict) else []
    wanted = set(addresses)

    for pair in pairs:
        if not isinstance(pair, dict) or pair.get("chainId") != "solana":
            continue
        base = pair.get("baseToken") or {}
        address = base.get("address")
        if address in wanted:
            result.setdefault(address, []).append(pair)

    return result


def best_pair(pairs):
    if not pairs:
        return None

    def liquidity(pair):
        try:
            return float((pair.get("liquidity") or {}).get("usd") or 0)
        except (TypeError, ValueError):
            return 0.0

    return max(pairs, key=liquidity)


def validate_pair(pair):
    if not pair:
        return False, "NO PAIR"

    try:
        price = float(pair.get("priceUsd") or 0)
        liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
        volume = float((pair.get("volume") or {}).get("m5") or 0)
        txns = (pair.get("txns") or {}).get("m5") or {}
        buys = int(txns.get("buys") or 0)
        sells = int(txns.get("sells") or 0)
        change = float((pair.get("priceChange") or {}).get("m5") or 0)
        created = pair.get("pairCreatedAt")
    except (TypeError, ValueError):
        return False, "INVALID MARKET DATA"

    if price <= 0:
        return False, "INVALID PRICE"
    if not created:
        return False, "PAIR AGE UNKNOWN"

    try:
        age_hours = (time.time() * 1000 - float(created)) / 3600000
    except (TypeError, ValueError):
        return False, "INVALID PAIR AGE"

    if age_hours < 0 or age_hours > MAX_PAIR_AGE_HOURS:
        return False, "NOT A NEW PAIR"
    if liquidity < MIN_LIQUIDITY_USD:
        return False, "LOW LIQUIDITY"
    if volume < MIN_VOLUME_5M_USD:
        return False, "LOW 5M VOLUME"
    if buys + sells < MIN_TXNS_5M:
        return False, "TOO FEW TRANSACTIONS"
    if sells <= 0 or buys < sells * MIN_BUY_RATIO:
        return False, "WEAK BUY PRESSURE"
    if change < MIN_PRICE_CHANGE_5M:
        return False, "NO UPWARD MOMENTUM"
    if change > MAX_PRICE_CHANGE_5M:
        return False, "PRICE SURGE TOO EXTREME"

    return True, "NEW-LAUNCH MOMENTUM OK"


def close_position(state, address, pair, reason):
    position = state["open_positions"].get(address)
    if not position or not pair:
        return

    try:
        entry = float(position["entry_price"])
        exit_price = float(pair.get("priceUsd") or 0)
        amount = float(position["position_usd"])
    except (KeyError, TypeError, ValueError):
        return

    if entry <= 0 or exit_price <= 0:
        return

    gross = amount * (exit_price / entry - 1.0)
    cost = amount * ESTIMATED_COST_PERCENT / 100.0
    net = gross - cost

    row = {
        "time_utc": now().isoformat(),
        "token_address": address,
        "symbol": position.get("symbol", "UNKNOWN"),
        "entry_price": entry,
        "exit_price": exit_price,
        "position_usd": amount,
        "gross_pnl_usd": round(gross, 4),
        "estimated_cost_usd": round(cost, 4),
        "estimated_net_pnl_usd": round(net, 4),
        "reason": reason
    }

    ensure_csv()
    with open(TRADES_FILE, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=CSV_FIELDS).writerow(row)

    del state["open_positions"][address]
    save_state(state)
    log("PAPER CLOSE: " + row["symbol"] + " | " + reason
        + " | estimated net $" + str(round(net, 4)))


def manage_positions(state):
    addresses = list(state["open_positions"].keys())
    if not addresses:
        return

    try:
        pairs_by_address = fetch_pairs(addresses)
    except Exception as error:
        log("Price update failed: " + str(error))
        return

    for address in addresses:
        position = state["open_positions"].get(address)
        if not position:
            continue

        pair = best_pair(pairs_by_address.get(address, []))
        if not pair:
            log("No current price for " + address)
            continue

        try:
            entry = float(position["entry_price"])
            price = float(pair.get("priceUsd") or 0)
            opened = float(position["entry_time"])
            amount = float(position["position_usd"])
        except (KeyError, TypeError, ValueError):
            continue

        if entry <= 0 or price <= 0:
            continue

        change = (price / entry - 1.0) * 100.0
        net = amount * (price / entry - 1.0)
        net -= amount * ESTIMATED_COST_PERCENT / 100.0
        age = (time.time() - opened) / 60.0

        log("POSITION: " + str(position.get("symbol"))
            + " | change " + str(round(change, 2)) + "%"
            + " | estimated net $" + str(round(net, 4)))

        if net >= TAKE_PROFIT_USD:
            close_position(state, address, pair, "TAKE_PROFIT")
        elif change <= -STOP_LOSS_PERCENT:
            close_position(state, address, pair, "STOP_LOSS")
        elif age >= MAX_HOLD_MINUTES:
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
        "position_usd": POSITION_USD
    }

    if address not in state["seen_tokens"]:
        state["seen_tokens"].append(address)

    state["checked_tokens"][address] = time.time() + REJECT_COOLDOWN
    save_state(state)
    log("PAPER OPEN: " + symbol + " | simulated $" + str(POSITION_USD)
        + " | entry price " + str(price))
    return True


def scan_entries(state):
    if len(state["open_positions"]) >= MAX_OPEN_POSITIONS:
        log("MAX OPEN POSITIONS REACHED")
        return

    daily_net = float(state.get("daily_realized_net_usd", 0))
    if daily_net <= -DAILY_LOSS_LIMIT_USD:
        log("DAILY LOSS LIMIT REACHED. No new entries.")
        return

    try:
        addresses = discover_tokens()
    except Exception as error:
        log("DISCOVERY ERROR: " + str(error))
        return

    log("Latest Solana token profiles: " + str(len(addresses)))
    candidates = []

    for address in addresses:
        if address in state["open_positions"]:
            continue
        if address in state["seen_tokens"]:
            continue

        expiry = state["checked_tokens"].get(address, 0)
        try:
            if float(expiry) > time.time():
                continue
        except (TypeError, ValueError):
            pass

        candidates.append(address)

    if not candidates:
        log("No new eligible token addresses.")
        return

    try:
        pairs_by_address = fetch_pairs(candidates)
    except Exception as error:
        log("PAIR LOOKUP ERROR: " + str(error))
        for address in candidates:
            state["checked_tokens"][address] = time.time() + API_COOLDOWN
        save_state(state)
        return

    opened = 0
    for address in candidates:
        if len(state["open_positions"]) >= MAX_OPEN_POSITIONS:
            break

        if float(state.get("daily_realized_net_usd", 0)) <= -DAILY_LOSS_LIMIT_USD:
            log("DAILY LOSS LIMIT REACHED. Blocking entries.")
            break

        pair = best_pair(pairs_by_address.get(address, []))
        valid, reason = validate_pair(pair)

        if not valid:
            log("REJECT: " + reason + " | " + address)
            state["checked_tokens"][address] = time.time() + REJECT_COOLDOWN
            continue

        if open_position(state, address, pair):
            opened += 1

    save_state(state)
    log("New paper positions opened: " + str(opened))
    log("Open positions total: " + str(len(state["open_positions"])))


def main():
    log("=" * 40)
    log("NEW-LAUNCH MOMENTUM PAPER SCALPER")
    log("UTC: " + now().isoformat())
    log("PAPER TRADING ONLY — NO REAL ORDERS")
    log("Simulated position: $" + str(POSITION_USD))
    log("Take-profit: $" + str(TAKE_PROFIT_USD))
    log("Daily net-loss limit: $" + str(DAILY_LOSS_LIMIT_USD))
    log("=" * 40)

    ensure_csv()
    state = load_state()

    manage_positions(state)

    if not recalculate_pnl(state):
        log("Accounting failed. Blocking new entries.")
        return

    scan_entries(state)
    recalculate_pnl(state)
    save_state(state)

    log("FINAL OPEN POSITIONS: " + str(len(state["open_positions"])))
    log("Run complete.")


if __name__ == "__main__":
    main()
