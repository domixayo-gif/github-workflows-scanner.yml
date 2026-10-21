
import csv
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

API = "https://api.dexscreener.com"
STATE_FILE = Path("paper_state.json")
TRADES_FILE = Path("paper_trades.csv")

MIN_LIQUIDITY = 15000
MIN_VOLUME_5M = 10000
MIN_TXNS_5M = 20
MAX_AGE_HOURS = 48
MAX_MOVE_5M = 20
TAKE_PROFIT = 5
STOP_LOSS = 4
MAX_HOLD_MINUTES = 60
POSITION_USD = 10
MAX_POSITIONS = 3
DAILY_LOSS_LIMIT = 5
ESTIMATED_COST_PERCENT = 4.0

HEADERS = {
    "User-Agent": "SolanaMemePaperBot/1.0",
    "Accept": "application/json",
}


def now():
    return datetime.now(timezone.utc).isoformat()


def get_json(url):
    request = Request(url, headers=HEADERS)
    with urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass

    return {
        "open_positions": {},
        "seen_tokens": [],
        "daily_realized_gross_usd": 0.0,
        "daily_key": "",
        "last_scan": None,
    }


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, indent=2),
        encoding="utf-8",
    )


def ensure_csv():
    if not TRADES_FILE.exists():
        with TRADES_FILE.open(
            "w", newline="", encoding="utf-8"
        ) as file:
            writer = csv.writer(file)
            writer.writerow([
                "opened_at", "closed_at", "name", "symbol",
                "token_address", "entry_usd", "exit_usd",
                "position_usd", "gross_pnl_usd",
                "estimated_cost_usd", "estimated_net_pnl_usd",
                "exit_reason", "pair_url",
            ])


def number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def find_pair(address):
    url = API + "/token-pairs/v1/solana/" + address

    try:
        pairs = get_json(url)
    except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
        print("Pair lookup error:", str(exc)[:150])
        return None

    if not isinstance(pairs, list):
        return None

    pairs = [
        pair for pair in pairs
        if pair.get("chainId") == "solana"
    ]

    pairs.sort(
        key=lambda pair: number(
            (pair.get("liquidity") or {}).get("usd")
        ),
        reverse=True,
    )

    return pairs[0] if pairs else None


def check_pair(pair):
    price = number(pair.get("priceUsd"))
    liquidity = number(
        (pair.get("liquidity") or {}).get("usd")
    )
    volume = pair.get("volume") or {}
    txns = pair.get("txns") or {}
    changes = pair.get("priceChange") or {}
    five_min = txns.get("m5") or {}

    volume_5m = number(volume.get("m5"))
    transactions = (
        int(number(five_min.get("buys")))
        + int(number(five_min.get("sells")))
    )
    move_5m = number(changes.get("m5"), 9999)

    created = pair.get("pairCreatedAt")
    age_hours = 9999

    if created:
        age_hours = max(
            0,
            (time.time() * 1000 - float(created)) / 3600000,
        )

    reasons = []

    if price <= 0:
        reasons.append("invalid price")
    if liquidity < MIN_LIQUIDITY:
        reasons.append("low liquidity")
    if volume_5m < MIN_VOLUME_5M:
        reasons.append("low five-minute volume")
    if transactions < MIN_TXNS_5M:
        reasons.append("low transaction count")
    if age_hours > MAX_AGE_HOURS:
        reasons.append("pair too old or age unknown")
    if abs(move_5m) > MAX_MOVE_5M:
        reasons.append("extreme five-minute movement")

    return (
        not reasons, reasons, price, liquidity,
        volume_5m, transactions, age_hours, move_5m,
    )


def close_position(state, address, pair, price, reason):
    position = state["open_positions"].pop(address)

    entry = number(position.get("entry_price"))
    amount = number(position.get("position_usd"))

    if entry <= 0 or price <= 0:
        print("Invalid price; cannot calculate paper result.")
        return

    change_percent = (price / entry - 1) * 100
    gross_pnl = amount * change_percent / 100

    # Rough estimate for round-trip costs, not a live quote.
    estimated_cost = (
        amount * ESTIMATED_COST_PERCENT / 100
    )
    net_estimate = gross_pnl - estimated_cost

    state["daily_realized_gross_usd"] = (
        number(state.get("daily_realized_gross_usd"))
        + gross_pnl
    )

    token = pair.get("baseToken") or {}

    with TRADES_FILE.open(
        "a", newline="", encoding="utf-8"
    ) as file:
        writer = csv.writer(file)
        writer.writerow([
            position.get("opened_at"),
            now(),
            token.get("name", position.get("name", "")),
            token.get("symbol", position.get("symbol", "")),
            address,
            entry,
            price,
            amount,
            round(gross_pnl, 4),
            round(estimated_cost, 4),
            round(net_estimate, 4),
            reason,
            pair.get("url", position.get("pair_url", "")),
        ])

    print(
        "PAPER EXIT:", token.get("symbol", "?"),
        "| reason:", reason,
        "| gross %:", round(change_percent, 2),
        "| estimated net USD:", round(net_estimate, 4),
    )


def main():
    ensure_csv()
    state = load_state()

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if state.get("daily_key") != today:
        state["daily_key"] = today
        state["daily_realized_gross_usd"] = 0.0

    state.setdefault("open_positions", {})
    state.setdefault("seen_tokens", [])
    state.setdefault("daily_realized_gross_usd", 0.0)

    print("SOLANA MEME COIN PAPER BOT")
    print("PAPER MODE ONLY: no wallet or real orders")
    print("UTC time:", now())

    # Update and close existing virtual positions.
    for address in list(state["open_positions"]):
        pair = find_pair(address)

        if not pair:
            print("Price unavailable for open position:", address)
            continue

        price = number(pair.get("priceUsd"))
        position = state["open_positions"].get(address, {})
        entry = number(position.get("entry_price"))

        if price <= 0 or entry <= 0:
            continue

        change = (price / entry - 1) * 100
        age = (
            time.time()
            - number(position.get("opened_epoch"), time.time())
        ) / 60

        if change >= TAKE_PROFIT:
            close_position(
                state, address, pair, price, "take_profit"
            )
        elif change <= -STOP_LOSS:
            close_position(
                state, address, pair, price, "stop_loss"
            )
        elif age >= MAX_HOLD_MINUTES:
            close_position(
                state, address, pair, price, "time_limit"
            )

    daily_pnl = number(state["daily_realized_gross_usd"])

    if daily_pnl <= -DAILY_LOSS_LIMIT:
        print("Daily loss limit reached. No new paper entries.")
        state["last_scan"] = now()
        save_state(state)
        return

    try:
        profiles = get_json(
            API + "/token-profiles/latest/v1"
        )
    except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
        print("Token scan failed:", str(exc)[:200])
        save_state(state)
        return

    candidates = []

    for item in profiles if isinstance(profiles, list) else []:
        if item.get("chainId") != "solana":
            continue

        address = item.get("tokenAddress", "")

        if address and address not in state["seen_tokens"]:
            candidates.append(item)

    candidates = candidates[:30]
    print("New tokens to inspect:", len(candidates))

    for item in candidates:
        address = item.get("tokenAddress", "")
        state["seen_tokens"].append(address)

        pair = find_pair(address)

        if not pair:
            print("REJECT: no available pair data")
            continue

        result = check_pair(pair)
        passed = result[0]
        reasons = result[1]
        price = result[2]
        liquidity = result[3]
        volume = result[4]
        transactions = result[5]
        age = result[6]
        move = result[7]

        token = pair.get("baseToken") or {}
        symbol = token.get("symbol", "?")

        print(
            "CHECK:", symbol,
            "| liquidity:", round(liquidity, 2),
            "| volume 5m:", round(volume, 2),
            "| txns 5m:", transactions,
            "| move 5m:", round(move, 2),
        )

        if not passed:
            print("REJECT:", symbol, "|".join(reasons))
            continue

        if address in state["open_positions"]:
            continue

        if len(state["open_positions"]) >= MAX_POSITIONS:
            print("Position limit reached.")
            continue

        state["open_positions"][address] = {
            "opened_at": now(),
            "opened_epoch": time.time(),
            "entry_price": price,
            "position_usd": POSITION_USD,
            "name": token.get("name", ""),
            "symbol": symbol,
            "pair_url": pair.get("url", ""),
        }

        print(
            "PAPER ENTRY:", symbol,
            "| entry USD:", price,
            "| target:", TAKE_PROFIT, "%",
            "| stop:", STOP_LOSS, "%",
            "| virtual size USD:", POSITION_USD,
        )

    state["seen_tokens"] = state["seen_tokens"][-5000:]
    state["last_scan"] = now()
    save_state(state)

    print(
        "Scan complete. Open paper positions:",
        len(state["open_positions"]),
    )


if __name__ == "__main__":
    main()
