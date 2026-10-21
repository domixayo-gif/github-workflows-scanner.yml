
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
ESTIMATED_COST_PERCENT = 4.0

MIN_LIQUIDITY_USD = 15000
MIN_VOLUME_5M_USD = 10000
MIN_TXNS_5M = 20
MAX_PAIR_AGE_HOURS = 48
MAX_MOVE_5M_PERCENT = 20.0
MAX_NEW_TOKENS_PER_SCAN = 30


def now_utc():
    return datetime.now(timezone.utc)


def today_key():
    return now_utc().strftime("%Y-%m-%d")


def load_state():
    default_state = {
        "open_positions": {},
        "seen_tokens": [],
        "daily_realized_gross_usd": 0.0,
        "daily_key": today_key(),
    }

    if not STATE_FILE.exists():
        return default_state

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            state = json.load(file)

        if not isinstance(state, dict):
            raise ValueError("State must be a JSON object")

    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print("STATE WARNING:", str(exc))
        print("Using a fresh default state.")
        return default_state

    if not isinstance(state.get("open_positions"), dict):
        state["open_positions"] = {}

    if not isinstance(state.get("seen_tokens"), list):
        state["seen_tokens"] = []

    try:
        state["daily_realized_gross_usd"] = float(
            state.get("daily_realized_gross_usd", 0.0)
        )
    except (TypeError, ValueError):
        state["daily_realized_gross_usd"] = 0.0

    if state.get("daily_key") != today_key():
        state["daily_key"] = today_key()
        state["daily_realized_gross_usd"] = 0.0

    return state


def save_state(state):
    temporary = STATE_FILE.with_suffix(".tmp")

    with temporary.open("w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)

    temporary.replace(STATE_FILE)


def api_get(path):
    url = API + path
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Solana-Paper-Scanner/1.0"},
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8")
            return json.loads(body)

    except urllib.error.HTTPError as exc:
        print("API HTTP ERROR:", exc.code, path)
        return None

    except urllib.error.URLError as exc:
        print("API CONNECTION ERROR:", str(exc.reason), path)
        return None

    except (TimeoutError, json.JSONDecodeError) as exc:
        print("API RESPONSE ERROR:", str(exc), path)
        return None


def get_best_pair(token_address):
    path = "/token-pairs/v1/solana/" + token_address
    data = api_get(path)

    if not isinstance(data, list):
        print("PAIR DATA UNAVAILABLE:", token_address)
        return None

    pairs = []

    for pair in data:
        if not isinstance(pair, dict):
            continue

        if pair.get("chainId") != "solana":
            continue

        liquidity_data = pair.get("liquidity") or {}

        try:
            liquidity = float(liquidity_data.get("usd") or 0)
        except (TypeError, ValueError):
            liquidity = 0.0

        if liquidity > 0:
            pairs.append((liquidity, pair))

    if not pairs:
        print("NO LIQUIDITY DATA:", token_address)
        return None

    pairs.sort(key=lambda item: item[0], reverse=True)
    return pairs[0][1]


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

    except (TypeError, ValueError):
        return False, "INVALID MARKET DATA"

    if price <= 0:
        return False, "PRICE MISSING"

    if liquidity < MIN_LIQUIDITY_USD:
        return False, "LOW LIQUIDITY: $" + str(round(liquidity))

    if volume < MIN_VOLUME_5M_USD:
        return False, "LOW 5M VOLUME: $" + str(round(volume))

    if buys + sells < MIN_TXNS_5M:
        return False, "LOW 5M TRANSACTIONS: " + str(buys + sells)

    if abs(movement) > MAX_MOVE_5M_PERCENT:
        return False, "5M MOVEMENT TOO HIGH: " + str(round(movement, 2))

    if created_ms <= 0:
        return False, "PAIR CREATION TIME MISSING"

    age_hours = (
        now_utc().timestamp() * 1000 - created_ms
    ) / 3600000

    if age_hours < 0:
        return False, "INVALID PAIR AGE"

    if age_hours > MAX_PAIR_AGE_HOURS:
        return False, "PAIR TOO OLD: " + str(round(age_hours, 1)) + "h"

    if buys <= sells:
        return False, "SELLS EQUAL OR EXCEED BUYS"

    return True, "PASS"


def ensure_trade_file():
    if TRADES_FILE.exists():
        return

    with TRADES_FILE.open(
        "w", newline="", encoding="utf-8"
    ) as file:
        writer = csv.writer(file)
        writer.writerow([
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
        ])


def close_position(state, address, price, reason):
    position = state["open_positions"].get(address)

    if not position:
        return

    try:
        entry = float(position["entry_price"])
        entry_time = float(position["entry_time"])
    except (KeyError, TypeError, ValueError):
        print("POSITION DATA ERROR:", address)
        return

    if entry <= 0 or price <= 0:
        print("INVALID EXIT PRICE:", address)
        return

    gross_pnl = POSITION_USD * ((price - entry) / entry)
    estimated_cost = POSITION_USD * ESTIMATED_COST_PERCENT / 100.0
    net_pnl = gross_pnl - estimated_cost

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
            POSITION_USD,
            round(gross_pnl, 4),
            round(estimated_cost, 4),
            round(net_pnl, 4),
            reason,
        ])

    state["daily_realized_gross_usd"] += gross_pnl
    del state["open_positions"][address]

    held_minutes = (time.time() - entry_time) / 60.0

    print(
        "PAPER CLOSE:", position.get("symbol", "UNKNOWN"),
        "| reason:", reason,
        "| gross P/L: $", round(gross_pnl, 4),
        "| estimated net P/L: $", round(net_pnl, 4),
        "| minutes held:", round(held_minutes, 1),
    )


def manage_positions(state):
    addresses = list(state["open_positions"].keys())

    for address in addresses:
        pair = get_best_pair(address)

        if not pair:
            print("POSITION PRICE UNAVAILABLE:", address)
            continue

        try:
            price = float(pair.get("priceUsd") or 0)
        except (TypeError, ValueError):
            continue

        if price <= 0:
            continue

        position = state["open_positions"].get(address)

        if not position:
            continue

        try:
            entry = float(position["entry_price"])
            entry_time = float(position["entry_time"])
        except (KeyError, TypeError, ValueError):
            print("POSITION DATA ERROR:", address)
            continue

        if entry <= 0:
            continue

        change = ((price - entry) / entry) * 100
        held = (time.time() - entry_time) / 60.0

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
    print("Checking DexScreener token profiles...")

    data = api_get("/token-profiles/latest/v1")

    if data is None:
        print("DISCOVERY FAILED: API returned no usable response.")
        return []

    if not isinstance(data, list):
        print("DISCOVERY ERROR: Expected a list.")
        print("Response type:", type(data).__name__)
        return []

    print("Profiles returned:", len(data))

    addresses = []
    solana_count = 0

    for item in data:
        if not isinstance(item, dict):
            continue

        if item.get("chainId") != "solana":
            continue

        solana_count += 1
        address = item.get("tokenAddress")

        if address and address not in addresses:
            addresses.append(address)

        if len(addresses) >= MAX_NEW_TOKENS_PER_SCAN:
            break

    print("Solana profiles found:", solana_count)
    print("Unique addresses selected:", len(addresses))

    return addresses


def main():
    print("=" * 50)
    print("SOLANA MEME COIN PAPER SCANNER")
    print("MODE: SIMULATION ONLY - NO REAL ORDERS")
    print("UTC:", now_utc().isoformat())
    print("Take profit:", TAKE_PROFIT, "%")
    print("Stop loss:", STOP_LOSS, "%")
    print("Simulated position: $", POSITION_USD)
    print("Maximum positions:", MAX_POSITIONS)
    print("=" * 50)

    state = load_state()
    ensure_trade_file()

    manage_positions(state)

    if state["daily_realized_gross_usd"] <= -DAILY_LOSS_LIMIT:
        print("DAILY LOSS LIMIT REACHED. No new entries.")
        save_state(state)
        return

    open_positions = state["open_positions"]
    seen_tokens = set(state["seen_tokens"])

    print("Currently open positions:", len(open_positions))
    print("Previously seen tokens:", len(seen_tokens))

    if len(open_positions) >= MAX_POSITIONS:
        print("MAXIMUM OPEN POSITIONS REACHED.")
        save_state(state)
        return

    addresses = discover_tokens()

    if not addresses:
        print("NO TOKEN ADDRESSES TO CHECK.")
        save_state(state)
        return

    checked = 0
    rejected = 0
    opened = 0

    for address in addresses:
        if address in seen_tokens:
            print("SKIP: Already seen token:", address)
            continue

        pair = get_best_pair(address)

        if not pair:
            rejected += 1
            continue

        checked += 1
        passed, reason = check_pair(pair)

        base = pair.get("baseToken") or {}
        symbol = str(base.get("symbol") or "UNKNOWN")[:30]

        if not passed:
            rejected += 1
            print("REJECT:", symbol, "|", reason)
            continue

        try:
            entry_price = float(pair.get("priceUsd") or 0)
        except (TypeError, ValueError):
            rejected += 1
            print("REJECT:", symbol, "| INVALID ENTRY PRICE")
            continue

        if entry_price <= 0:
            rejected += 1
            print("REJECT:", symbol, "| INVALID ENTRY PRICE")
            continue

        open_positions[address] = {
            "symbol": symbol,
            "entry_price": entry_price,
            "entry_time": time.time(),
        }

        seen_tokens.add(address)
        opened += 1

        print(
            "PAPER OPEN:", symbol,
            "| entry price:", entry_price,
            "| simulated amount: $", POSITION_USD,
        )

        save_state(state)

        if len(open_positions) >= MAX_POSITIONS:
            print("MAXIMUM POSITIONS REACHED.")
            break

        time.sleep(0.2)

    state["seen_tokens"] = list(seen_tokens)[-5000:]
    save_state(state)

    print("-" * 50)
    print("SCAN SUMMARY")
    print("Addresses discovered:", len(addresses))
    print("Pairs checked:", checked)
    print("Candidates rejected:", rejected)
    print("New simulated positions opened:", opened)
    print("Total open positions:", len(open_positions))
    print(
        "Daily gross realized P/L: $",
        round(state["daily_realized_gross_usd"], 4),
    )
    print("Scan complete.")


if __name__ == "__main__":
    main()
