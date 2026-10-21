
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
    if not STATE_FILE.exists():
        return {
            "open_positions": {},
            "seen_tokens": [],
            "daily_realized_gross_usd": 0.0,
            "daily_key": today_key(),
        }

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            state = json.load(file)
    except (OSError, json.JSONDecodeError):
        print("WARNING: State file could not be read. Starting fresh.")
        state = {}

    state.setdefault("open_positions", {})
    state.setdefault("seen_tokens", [])
    state.setdefault("daily_realized_gross_usd", 0.0)
    state.setdefault("daily_key", today_key())

    if state["daily_key"] != today_key():
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
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print("API error:", str(exc))
        return None


def get_best_pair(token_address):
    data = api_get("/token-pairs/v1/solana/" + token_address)

    if not isinstance(data, list):
        return None

    pairs = []
    for pair in data:
        if not isinstance(pair, dict):
            continue
        if pair.get("chainId") != "solana":
            continue
        liquidity = pair.get("liquidity") or {}
        try:
            liquidity_usd = float(liquidity.get("usd") or 0)
        except (TypeError, ValueError):
            liquidity_usd = 0.0
        if liquidity_usd > 0:
            pairs.append((liquidity_usd, pair))

    if not pairs:
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
        buys = int((txns.get("buys") or 0))
        sells = int((txns.get("sells") or 0))
        price = float(pair.get("priceUsd") or 0)
        movement = float(
            (pair.get("priceChange") or {}).get("m5") or 0
        )
        created_ms = int(pair.get("pairCreatedAt") or 0)
    except (TypeError, ValueError):
        return False

    if price <= 0:
        return False
    if liquidity < MIN_LIQUIDITY_USD:
        return False
    if volume < MIN_VOLUME_5M_USD:
        return False
    if buys + sells < MIN_TXNS_5M:
        return False
    if abs(movement) > MAX_MOVE_5M_PERCENT:
        return False

    if created_ms <= 0:
        return False

    age_hours = (
        now_utc().timestamp() * 1000 - created_ms
    ) / 3600000

    if age_hours < 0 or age_hours > MAX_PAIR_AGE_HOURS:
        return False

    if buys <= sells:
        return False

    return True


def ensure_trade_file():
    if TRADES_FILE.exists():
        return

    with TRADES_FILE.open("w", newline="", encoding="utf-8") as file:
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
    position = state["open_positions"].pop(address, None)
    if not position:
        return

    entry = float(position["entry_price"])
    entry_time = float(position["entry_time"])
    if entry <= 0 or price <= 0:
        print("Invalid close price for", address)
        state["open_positions"][address] = position
        return

    gross_pnl = POSITION_USD * ((price - entry) / entry)
    estimated_cost = POSITION_USD * ESTIMATED_COST_PERCENT / 100.0
    net_pnl = gross_pnl - estimated_cost

    state["daily_realized_gross_usd"] += gross_pnl

    ensure_trade_file()
    with TRADES_FILE.open("a", newline="", encoding="utf-8") as file:
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

    held_minutes = (time.time() - entry_time) / 60.0
    print(
        "PAPER CLOSE:",
        position.get("symbol", "UNKNOWN"),
        "reason=", reason,
        "gross=", round(gross_pnl, 4),
        "estimated_net=", round(net_pnl, 4),
        "held_min=", round(held_minutes, 1),
    )


def manage_positions(state):
    addresses = list(state["open_positions"].keys())

    for address in addresses:
        pair = get_best_pair(address)
        if not pair:
            print("No current pair data for", address)
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

        entry = float(position["entry_price"])
        change_percent = ((price - entry) / entry) * 100
        held_minutes = (
            time.time() - float(position["entry_time"])
        ) / 60.0

        print(
            "WATCH:",
            position.get("symbol", "UNKNOWN"),
            "change=", round(change_percent, 2), "%",
        )

        if change_percent >= TAKE_PROFIT:
            close_position(state, address, price, "TAKE_PROFIT")
        elif change_percent <= -STOP_LOSS:
            close_position(state, address, price, "STOP_LOSS")
        elif held_minutes >= MAX_HOLD_MINUTES:
            close_position(state, address, price, "TIME_LIMIT")


def discover_tokens():
    data = api_get("/token-profiles/latest/v1")
    if not isinstance(data, list):
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

    return addresses


def main():
    print("=" * 48)
    print("SOLANA MEME COIN PAPER SCANNER")
    print("Mode: SIMULATION ONLY — NO REAL ORDERS")
    print("UTC time:", now_utc().isoformat())
    print("=" * 48)

    state = load_state()
    ensure_trade_file()

    manage_positions(state)

    if state["daily_realized_gross_usd"] <= -DAILY_LOSS_LIMIT:
        print("Daily gross loss limit reached. No new entries.")
        save_state(state)
        return

    open_positions = state["open_positions"]
    seen_tokens = set(state["seen_tokens"])

    if len(open_positions) >= MAX_POSITIONS:
        print("Maximum open positions reached.")
        save_state(state)
        return

    addresses = discover_tokens()
    if not addresses:
        print("No new Solana token profiles returned.")
        save_state(state)
        return

    for address in addresses:
        if address in seen_tokens:
            continue

        pair = get_best_pair(address)
        if not pair:
            continue

        if not check_pair(pair):
            continue

        try:
            entry_price = float(pair.get("priceUsd") or 0)
        except (TypeError, ValueError):
            continue

        if entry_price <= 0:
            continue

        base = pair.get("baseToken") or {}
        symbol = str(base.get("symbol") or "UNKNOWN")[:30]

        open_positions[address] = {
            "symbol": symbol,
            "entry_price": entry_price,
            "entry_time": time.time(),
        }

        seen_tokens.add(address)
        print(
            "PAPER OPEN:",
            symbol,
            "entry=", entry_price,
            "simulated_position=$", POSITION_USD,
        )

        if len(open_positions) >= MAX_POSITIONS:
            break

        time.sleep(0.2)

    state["seen_tokens"] = list(seen_tokens)[-5000:]
    save_state(state)

    print("Open positions:", len(state["open_positions"]))
    print(
        "Daily gross realized P/L: $",
        round(state["daily_realized_gross_usd"], 4),
    )
    print("Scan complete.")


if __name__ == "__main__":
    main()
