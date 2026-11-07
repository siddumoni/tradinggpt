# Telegram advisory alerts

## Scope and safety

This integration sends advisory notifications for the integrated paper account. It has no Kite client and no order API; it cannot place, modify, or cancel broker orders.

When Telegram is enabled and configured, the paper broker first validates a conditional candidate against live quote freshness, executable bid/ask, portfolio capacity, cash, margin verification, stop/target geometry, transaction costs, net risk, and the configured net R:R gate. It then sends the complete trade plan before inserting the simulated entry into the paper ledger. If the plan alert is rate-limited, falls inside quiet hours, or cannot be delivered, that simulated entry is withheld. This prevents an enabled alerting workflow from silently opening an unannounced paper position.

When Telegram is disabled, notifications are opt-in and paper trading continues without alert gating. If alerts are explicitly enabled but credentials are missing, pre-entry alerts fail closed and conditional paper entries are withheld.

Supported notifications:

- **Conditional trade plan (before entry):** symbol, side, strategy/version, signal time, signal reference, executable quote, indicative paper fill, stop, T1/T2, quantity, risk budget, estimated net risk, net R:R at both targets, estimated margin, quote age, market regime and scanner reasons.
- **Paper entry confirmation:** simulated fill, quantity, stop and targets.
- **Paper exit:** simulated exit fill, reason and estimated net P&L.
- **Risk overrun:** a simulated loss exceeded its recorded planned risk.
- **EOD paper digest:** sent from 15:45 IST, with today's realized net P&L, cumulative P&L, unrealized P&L, open exposure/risk and version-isolated cumulative results.

All dynamic message fields are HTML-escaped and Telegram HTML parse mode is enabled, so symbols such as M&M render correctly rather than showing &amp;.

## Setup

1. In Telegram, open the official @BotFather, create a bot, and copy its token.
2. Start a private conversation with the new bot and send it /start.
3. Obtain the private chat ID using a trusted Telegram bot-update inspection method. Do not paste the bot token or chat ID into GitHub, source code, or a public issue.
4. Copy .env.example to .env and configure:

~~~dotenv
TELEGRAM_ALERTS_ENABLED=true
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_private_chat_id
TELEGRAM_QUIET_HOURS_START=22:00
TELEGRAM_QUIET_HOURS_END=07:00
TELEGRAM_MAX_ALERTS_PER_MINUTE=5
TELEGRAM_MAX_ALERTS_PER_HOUR=30
~~~

5. Restart the integrated service:

~~~powershell
python -m src.paper_trading_service
~~~

To turn notifications off, set TELEGRAM_ALERTS_ENABLED=false and restart. With alerts disabled, the notifier does not send messages and does not block paper entries.

## Delivery, deduplication and rate limits

- Disabled by default. No bot token or chat ID is stored in the repository.
- Default quiet hours are 22:00–07:00 IST. Set the start and end to the same time to disable the quiet-hours window.
- Default rate limits are 5 messages per minute and 30 per hour. They apply across trade plans, event notifications and the EOD digest.
- A trade plan is deduplicated by its paper signal ID. Successfully sent IDs and send timestamps are persisted in logs/telegram_trade_plan_state.json.
- The EOD digest is sent at or after 15:45 IST and deduplicated by session date.
- Event cursor is stored separately at logs/telegram_alert_state.json; the first startup seeds at the current event head and does not flood historical events.
- Failed event delivery does not advance the cursor, so the next poll retries it.
- If a trade-plan alert cannot be delivered while alerts are enabled, the paper broker records a rejection instead of entering the simulated position.
- A rare duplicate can occur if Telegram accepts a message but the process crashes before the local dedup state is persisted. No signal is backfilled or replayed just to create an alert.

## Troubleshooting

- **No alerts:** check TELEGRAM_ALERTS_ENABLED=true, confirm the bot token and chat ID are set in the service process environment, and ensure the private chat has sent /start to the bot.
- **Conditional setup rejected with alert-delivery reason:** check credentials, network access, quiet hours and rate limits. The paper entry was intentionally withheld.
- **Bot token invalid:** create a new token with BotFather and replace the local value. Never log or commit it.
- **Network unavailable:** paper service continues. With alerts enabled, new conditional paper entries are withheld until their plan alert can be delivered; existing positions continue to be managed.
- **Rate limit reached:** messages are deferred and retried; a new conditional entry is not opened until its pre-entry plan is delivered.
- **Duplicate notification:** inspect the local files under logs/; delivery is at-least-once by design.

All notifications describe simulated paper activity only. They are not broker orders, investment advice, guarantees of execution, or claims of profitability.
