
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

TAKE_PROFIT = 10.0
STOP_LOSS = 4.0
MAX_HOLD_MINUTES = 60
POSITION_USD = 10.0
MAX_POSITIONS = 3
DAILY_LOSS_LIMIT = 5.0

# Assumed total trading costs, not verified live fees.
ESTIMATED_COST_PERCENT = 4.0

MIN_LIQUIDITY_USD = 15000
MIN_VOLUME_5M_USD = 10000
MIN_TXNS_5M = 20
MAX_PAIR_AGE_HOURS = 48
MAX_MOVE_5M_PERCENT = 20.0
MAX_NEW_TOKENS_PER_SCAN = 30

# Recheck rejected tokens after this many seconds.
REJECT_COOLDOWN_SECONDS = 6 * 60 * 60
API_FAILURE_COOLDOWN_SECONDS = 10 * 60

CSV_HEADERS = [
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


def load_state():
    defaults = {
        "open_positions": {},
        "seen_tokens": [],
        "checked_tokens": {},
        "daily_realized_gross_usd": 0.0,
        "daily_realized_net_usd": 0.0,
        "daily_key": today_key(),
    }

    if not STATE_FILE.exists():
        return defaults

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            state = json.load(file)

        if not isinstance(state, dict):
            raise ValueError("State must be an object")

    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print("STATE WARNING:", str(exc))
        print("Using default state.")
        return defaults

    for key, value in defaults.items():
        if key not in state:
            state[key] = value

    if not isinstance(state["open_positions"], dict):
        state["open_positions"] = {}

    if not isinstance(state["seen_tokens"], list):
        state["seen_tokens"] = []

    if not isinstance(state["checked_tokens"], dict):
        state["checked_tokens"] = {}

    for key in (
        "daily_realized_gross_usd",
        "daily_realized_net_usd",
    ):
        try:
            state[key] = float(state.get(key, 0.0))
        except (TypeError, ValueError):
            state[key] = 0.0

    if state.get("daily_key") != today_key():
        state["daily_key"] = today_key()
        state["daily_realized_gross_usd"] = 0.0
        state["daily_realized_net_usd"] = 0.0

    # Migrate positions created by the previous scanner.
    for position in state["open_positions"].values():
        if isinstance(position, dict):
            position.setdefault("position_usd", POSITION_USD)

    return state


def save_state(state):
    temporary = STATE_FILE.with_suffix(".tmp")

    with temporary.open("w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)

    temporary.replace(STATE_FILE)


def api_get(path):
    request = urllib.request.Request(
        API + path,
        headers={"User-Agent": "Solana-Paper-Scanner/2.0"},
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    except urllib.error.HTTPError as exc:
        print("API HTTP ERROR:", exc.code, path)
    except urllib.error.URLError as exc:
        print("API CONNECTION ERROR:", str(exc.reason), path)
    except (TimeoutError, OSError, ValueError) as exc:
        print("API RESPONSE ERROR:", str(exc), path)

    return None


def get_best_pair(address):
    data = api_get("/token-pairs/v1/solana/" + address)

    if not isinstance(data, list):
        print("PAIR DATA UNAVAILABLE:", address)
        return None

    best_pair = None
    best_liquidity = 0.0

    for pair in data:
        if not isinstance(pair, dict):
            continue

        if pair.get("chainId") != "solana":
            continue

        try:
            liquidity = float(
                (pair.get("liquidity") or {}).get("usd") or 0
            )
        except (TypeError, ValueError):
            continue

        if liquidity > best_liquidity:
            best_liquidity = liquidity
            best_pair = pair

    if best_pair is None:
        print("NO USABLE SOLANA PAIR:", address)

    return best_pair


def check_pair(pair):
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
        price = float(pair.get("priceUsd") or 0)
        movement = float(
            (pair.get("priceChange") or {}).get("m5") or 0
        )
        created_ms = int(pair.get("pairCreatedAt") or 0)

    except (TypeError, ValueError, AttributeError):
        return False, "INVALID MARKET DATA"

    if price <= 0:
        return False, "PRICE MISSING"

    if liquidity < MIN_LIQUIDITY_USD:
        return False, "LOW LIQUIDITY: $" + str(round(liquidity))

    if volume < MIN_VOLUME_5M_USD:
        return False, "LOW 5M VOLUME: $" + str(round(volume))

    if buys + sells < MIN_TXNS_5M:
        return False, "LOW 5M TRANSACTIONS"

    if abs(movement) > MAX_MOVE_5M_PERCENT:
        return False, "5M MOVEMENT TOO HIGH"

    if created_ms <= 0:
        return False, "PAIR CREATION TIME MISSING"

    age_hours = (
        now_utc().timestamp() * 1000 - created_ms
    ) / 3600000

    if age_hours < 0:
        return False, "INVALID PAIR AGE"

    if age_hours > MAX_PAIR_AGE_HOURS:
        return False, "PAIR TOO OLD"

    if buys <= sells:
        return False, "BUYS DO NOT EXCEED SELLS"

    return True, "PASS"


def ensure_trade_file():
    if TRADES_FILE.exists():
        return

    with TRADES_FILE.open(
        "w", newline="", encoding="utf-8"
    ) as file:
        csv.writer(file).writerow(CSV_HEADERS)


def close_position(state, address, price, reason):
    position = state["open_positions"].get(address)

    if not position:
        return

    try:
        entry = float(position["entry_price"])
        amount = float(position.get("position_usd", POSITION_USD))
    except (TypeError, ValueError, KeyError):
        print("POSITION DATA ERROR:", address)
        return

    if entry <= 0 or price <= 0 or amount <= 0:
        print("INVALID EXIT DATA:", address)
        return

    gross = amount * ((price - entry) / entry)
    cost = amount * ESTIMATED_COST_PERCENT / 100.0
    net = gross - cost

    ensure_trade_file()

    with TRADES_FILE.open(
        "a", newline="", encoding="utf-8"
    ) as file:
        writer = csv.writer(file)
        writer.writerow([
            now_utc().isoformat(),
            address,
            position.get("symbol", "UNKNOWN"),
            entry,
            price,
            amount,
            round(gross, 4),
            round(cost, 4),
            round(net, 4),
            reason,
        ])

    state["daily_realized_gross_usd"] += gross
    state["daily_realized_net_usd"] += net

    symbol = position.get("symbol", "UNKNOWN")
    del state["open_positions"][address]

    print(
        "PAPER CLOSE:", symbol,
        "| reason:", reason,
        "| gross: $", round(gross, 4),
        "| estimated net: $", round(net, 4),
    )


def manage_positions(state):
    for address in list(state["open_positions"].keys()):
        pair = get_best_pair(address)

        if not pair:
            print("POSITION PRICE UNAVAILABLE:", address)
            continue

        try:
            price = float(pair.get("priceUsd") or 0)
            position = state["open_positions"][address]
            entry = float(position["entry_price"])
            entry_time = float(position["entry_time"])
        except (TypeError, ValueError, KeyError):
            print("POSITION DATA ERROR:", address)
            continue

        if price <= 0 or entry <= 0:
            continue

        change = (price / entry - 1) * 100
        held = (time.time() - entry_time) / 60

        print(
            "WATCH:", position.get("symbol", "UNKNOWN"),
            "| change:", round(change, 2), "%",
            "| held:", round(held, 1), "minutes",
        )

        if change >= TAKE_PROFIT:
            close_position(state, address, price, "TAKE_PROFIT")
        elif change <= -STOP_LOSS:
            close_position(state, address, price, "STOP_LOSS")
        elif held >= MAX_HOLD_MINUTES:
            close_position(state, address, price, "TIME_LIMIT")


def discover_tokens():
    print("Checking latest DexScreener token profiles...")

    data = api_get("/token-profiles/latest/v1")

    if not isinstance(data, list):
        print("DISCOVERY FAILED: No usable profile list.")
        return []

    addresses = []

    for item in data:
        if not isinstance(item, dict):
            continue

        if item.get("chainId") != "solana":
            continue

        address = item.get("tokenAddress")

        if address and address not in addresses:
            addresses.append(address)

        if len(addresses) >= MAX_NEW_TOKENS_PER_SCAN:
            break

    print("Solana addresses selected:", len(addresses))
    return addresses


def main():
    print("=" * 50)
    print("SOLANA MEME COIN PAPER SCANNER")
    print("SIMULATION ONLY - NO REAL ORDERS")
    print("UTC:", now_utc().isoformat())
    print("Take profit:", TAKE_PROFIT, "%")
    print("Stop loss:", STOP_LOSS, "%")
    print("Estimated cost:", ESTIMATED_COST_PERCENT, "%")
    print("Position size: $", POSITION_USD)
    print("=" * 50)

    state = load_state()
    ensure_trade_file()

    manage_positions(state)
    save_state(state)

    if state["daily_realized_net_usd"] <= -DAILY_LOSS_LIMIT:
        print("DAILY NET LOSS LIMIT REACHED. No new entries.")
        return

    open_positions = state["open_positions"]
    seen_tokens = set(state["seen_tokens"])
    checked_tokens = state["checked_tokens"]
    now = time.time()

    print("Open positions:", len(open_positions))
    print("Previously opened tokens:", len(seen_tokens))
    print("Daily gross P/L: $", round(
        state["daily_realized_gross_usd"], 4
    ))
    print("Daily estimated net P/L: $", round(
        state["daily_realized_net_usd"], 4
    ))

    if len(open_positions) >= MAX_POSITIONS:
        print("MAXIMUM OPEN POSITIONS REACHED.")
        return

    addresses = discover_tokens()

    if not addresses:
        print("NO TOKEN ADDRESSES TO CHECK.")
        save_state(state)
        return

    checked = 0
    rejected = 0
    opened = 0
    skipped = 0

    for address in addresses:
        if address in open_positions or address in seen_tokens:
            skipped += 1
            continue

        try:
            last_check = float(checked_tokens.get(address, 0))
        except (TypeError, ValueError):
            last_check = 0

        if now - last_check < REJECT_COOLDOWN_SECONDS:
            skipped += 1
            continue

        pair = get_best_pair(address)

        if not pair:
            checked_tokens[address] = time.time()
            rejected += 1
            continue

        checked += 1
        passed, reason = check_pair(pair)

        base = pair.get("baseToken") or {}
        symbol = str(base.get("symbol") or "UNKNOWN")[:30]

        if not passed:
            checked_tokens[address] = time.time()
            rejected += 1
            print("REJECT:", symbol, "|", reason)
            continue

        try:
            entry_price = float(pair.get("priceUsd") or 0)
        except (TypeError, ValueError):
            entry_price = 0.0

        if entry_price <= 0:
            checked_tokens[address] = time.time()
            rejected += 1
            print("REJECT:", symbol, "| INVALID ENTRY PRICE")
            continue

        open_positions[address] = {
            "symbol": symbol,
            "entry_price": entry_price,
            "entry_time": time.time(),
            "position_usd": POSITION_USD,
        }

        seen_tokens.add(address)
        checked_tokens[address] = time.time()
        opened += 1

        print(
            "PAPER OPEN:", symbol,
            "| entry:", entry_price,
            "| simulated amount: $", POSITION_USD,
        )

        save_state(state)

        if len(open_positions) >= MAX_POSITIONS:
            print("MAXIMUM POSITIONS REACHED.")
            break

        time.sleep(0.2)

    # Keep the most recently checked tokens to limit state-file growth.
    cutoff = time.time() - REJECT_COOLDOWN_SECONDS
    cleaned = {}

    for address, stamp in checked_tokens.items():
        try:
            if float(stamp) >= cutoff:
                cleaned[address] = float(stamp)
        except (TypeError, ValueError):
            continue

    state["checked_tokens"] = cleaned
    state["seen_tokens"] = list(seen_tokens)[-5000:]
    save_state(state)

    print("-" * 50)
    print("SCAN SUMMARY")
    print("Addresses discovered:", len(addresses))
    print("Pairs checked:", checked)
    print("Candidates rejected:", rejected)
    print("Tokens skipped:", skipped)
    print("New simulated positions opened:", opened)
    print("Total open positions:", len(open_positions))
    print("Daily gross P/L: $", round(
        state["daily_realized_gross_usd"], 4
    ))
    print("Daily estimated net P/L: $", round(
        state["daily_realized_net_usd"], 4
    ))
    print("Scan complete.")


if __name__ == "__main__":
    main()
