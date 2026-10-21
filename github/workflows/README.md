
# Solana Meme Coin Paper Bot

A paper-trading bot that scans Solana meme coins using public DexScreener market data.

## Trading rules

- Take profit: +5% gross price movement.
- Stop loss: -4% gross price movement.
- Maximum holding time: 60 minutes.
- Minimum liquidity: $15,000.
- Minimum 5-minute volume: $10,000.
- Minimum 5-minute transactions: 20.
- Maximum open paper positions: 3.
- Daily gross loss limit: $5.
- Virtual position size: $10 per trade.

## Safety

This bot is for paper trading only.

It does not connect to a wallet, sign transactions, or buy or sell real cryptocurrency.

Market prices may change quickly. The estimated trading costs are not guaranteed, and a 5% gross price increase does not mean a 5% net profit.

The bot does not guarantee profits or verify every token's safety.

## Files

- `scanner.py` — scanner and paper-trading logic.
- `paper_state.json` — saved paper positions and scanner state.
- `paper_trades.csv` — paper-trading history.
- `requirements.txt` — dependency information.
- `.github/workflows/scanner.yml` — scheduled GitHub Actions workflow.

## How to run

Open the Actions tab and manually run the workflow named `Solana Meme Coin Paper Bot`.

The workflow is scheduled to run every five minutes.

No wallet keys or trading secrets are required.
