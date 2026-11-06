# Telegram advisory alerts

## Scope and safety

This integration is a notification adapter for the integrated Champion paper account. It reads the broker's persisted event feed and sends selected simulated events to Telegram. It has no Kite client, no order API, and no path to place, modify, or cancel orders. Telegram delivery failure never changes a paper decision or stops the scan loop.

Supported events:

- `PAPER_ENTRY`: simulated entry fill, quantity, stop and targets.
- `PAPER_EXIT`: simulated exit fill, reason and estimated net P&L.
- `RISK_OVERRUN`: a simulated loss exceeded its recorded planned risk.

The alert body explicitly says that no broker order was placed. Candidate/watch-shadow observations and every scanner candidate are intentionally not sent, to avoid turning unapproved or overlapping research observations into actionable signal noise.

## Setup

1. In Telegram, open the official `@BotFather`, create a bot, and copy its token.
2. Start a private conversation with the new bot and send it `/start`.
3. Obtain the private chat ID using a trusted Telegram bot-update inspection method. Do not paste the bot token or chat ID into GitHub, source code, or a public issue.
4. Copy `.env.example` to `.env` and configure:

```dotenv
TELEGRAM_ALERTS_ENABLED=true
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_private_chat_id
```

5. Restart the integrated service:

```powershell
python -m src.paper_trading_service
```

To turn alerts off, set `TELEGRAM_ALERTS_ENABLED=false` and restart.

## Delivery behavior

- Disabled by default. Missing credentials fail closed and leave alerts disabled.
- Polls the persisted paper event feed every two seconds.
- Sends only Champion `PAPER_ENTRY`, `PAPER_EXIT`, and `RISK_OVERRUN` events.
- Stores the last processed event ID at `logs/telegram_alert_state.json`; this file is local runtime state and is excluded by the existing logs ignore rule.
- A failed send does not advance the cursor past that event, so the next poll retries it.
- Uses a bounded network timeout and at most two attempts per poll.
- Delivery is at-least-once: a rare duplicate can occur if the process crashes after Telegram accepts a message but before the local cursor is persisted.
- On first startup without a cursor file, the notifier seeds at the latest event ID and does not send historical events.

## Troubleshooting

- No alert: check `TELEGRAM_ALERTS_ENABLED=true`, confirm the bot token and chat ID are set in the service process environment, and ensure the private chat has sent `/start` to the bot.
- Bot token invalid: create a new token with BotFather and replace the local value. Never log or commit it.
- Network unavailable: paper trading continues; the notifier logs a delivery warning and retries later.
- Duplicate notification: inspect `logs/telegram_alert_state.json`; delivery is at-least-once by design.

All alerts are simulated paper-account events. They are not broker orders, investment advice, or guarantees of execution or profitability.
