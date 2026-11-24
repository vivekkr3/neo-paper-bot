# Kotak Neo NIFTY & SENSEX — unattended paper-only edition

This revision is deliberately paper-only. The original trading strategy and JSON settings are retained; actual orders, login, TOTP and MPIN calls have been removed from the adapter. `--mode live` is rejected even if the old live opt-in environment variable is present. Kotak's documented quotes, expiries and option-chain methods use only the consumer API token. The SDK itself is a general-purpose SDK; do not modify this bot to call its order methods. No real Kotak account, real order, Telegram account or VPS was used for integration testing.

## Strategy (IST)

- NIFTY and SENSEX: find nearest actual listed expiry via Kotak. Trade 1 DTE (previous trading session, accounting for holidays) or 0 DTE. Normal weeks: NIFTY Mon/Tue, SENSEX Wed/Thu. No hardcoded lot size. One lot, one basket at a time by default, ₹3 lakh planning allocation.
- 09:18 optional ₹1–₹2 OTM wings; both must fill, otherwise close the incomplete basket. 09:20 short entry, no later than 09:21. Pre-expiry: absolute 0.20 model delta CE/PE (Black–Scholes IV inferred from each quote, tolerance 0.06); `strike_mode=distance` uses ±0.8% OTM instead. Expiry: nearest common ATM CE/PE. Initial simulated SL-limit +35% (1 DTE), +30% (0 DTE), based on actual simulated short fills.
- One filled stop: move surviving stop to its entry price, or close if already crossed. Both filled: close wings. No same-index re-entry. On 0 DTE at/after 14:30, if both shorts remain open and combined LTP is below 15% of original combined fill premiums, close. 15:15 initiate exit, shorts before wings. Default ₹6,000 gross daily loss exit trigger, not a guaranteed loss cap.
- Quote freshness, entry liquidity, incomplete basket recovery, persistent order journal, kill marker and duplicate prevention are retained. All orders and stop triggers are **local simulations**. No exchange-side orders are sent. Simulated funds are not an actual margin check. Prices are based on REST polling and displayed bid/ask/depth, not tick-by-tick fills. Fees, queue position, partial fills, actual exchange latency and realistic slippage are not modeled. Results are gross, not net; no guarantee of profitability or live execution.

## 1. First installation on DigitalOcean

Ubuntu 24.04 LTS, SSH key, preferably 1 GB RAM (512 MB with swap may work; monitor memory). No public web server required. Check time synchronization. Restrict SSH to trusted IPs. For the small plan, add swap before installing if necessary. Do not expose the secret file over HTTP.

On your computer (terminal or PowerShell):

```bash
scp neo_expiry_bot_paper.zip root@YOUR_VPS_IP:/root/
ssh root@YOUR_VPS_IP
```

On the VPS:

```bash
apt update && apt install -y unzip
unzip neo_expiry_bot_paper.zip
cd neo_expiry_bot_paper
bash deploy/install.sh
```

The installer creates the `neobot` Linux user, a Python virtual environment, private runtime directory and systemd timer. It does not enable the timer automatically. It uses a no-cache pip install to save disk space. The SDK is pinned to `kotakneoapi==3.0.13`.

## 2. One-time market-data configuration

Kotak Neo app/web → More → Trade API → Generate Application → copy API token. Do not share your token or credentials in chat. If Kotak requests a static IP, whitelist the VPS actual egress IPv4 (`curl -4 https://api.ipify.org`). Live order API restrictions do not imply daily login is required for the documented read-only data endpoints. Account token validity and data access still need verification.

```bash
sudoedit /opt/neo-expiry/.env
```

```dotenv
NEO_CONSUMER_KEY='YOUR_API_TOKEN'
TELEGRAM_BOT_TOKEN=''
TELEGRAM_CHAT_ID=''
```

No daily login, mobile number, UCC, MPIN, TOTP seed or `ENABLE_LIVE_TRADING` is required. Remove unused old secrets from the file if upgrading. Ensure owner `neobot` and mode 600:

```bash
sudo chown neobot:neobot /opt/neo-expiry/.env
sudo chmod 600 /opt/neo-expiry/.env
sudo -u neobot /opt/neo-expiry/run.sh --mode paper --preflight
```

Preflight checks expiry and chain for each index and reports lot size and spot (spot freshness may fail outside market hours). Verify during market hours before relying on the simulation. A revoked/expired token must be replaced manually. Before a fresh morning session, the bot checks expiry API connectivity and fails closed on error. systemd retries failures with a 60-second delay, up to five starts within 10 minutes. If the service exhausts this limit, inspect logs, fix the issue and restart it manually before entry. Starting too late skips trades.

## 3. Automatic schedule

```bash
sudo systemctl enable --now neo-paper.timer
sudo systemctl list-timers neo-paper.timer
# Optional: manually start today, before the strategy entry window:
sudo systemctl start neo-paper.service
sudo journalctl -u neo-paper.service -f
```

The enabled timer runs every weekday at 08:55 Asia/Kolkata, including after VPS reboots (provided it is up at the scheduled time). Missed starts are not replayed. Exchange holidays are filtered by the trading calendar. The bot normally stops at 15:35 when all simulated positions are flat. The VPS and valid API token are required; your laptop/phone need not remain on. No trade is guaranteed on any day; chain availability, hedges, liquidity and entry filters can skip a session.

The bundled calendar covers 2026-11-24 through **2026-12-31 only**. New entries outside verified coverage are blocked. Update `calendar.json` for both indices from official NSE/BSE derivatives holiday circulars before 2027 trading (and when dates change). Do not blindly extend dates. Special weekend sessions are not supported. Existing calendar and settings are preserved on upgrade.

## 4. Optional Telegram alerts

Create your own bot with Telegram @BotFather; save its token privately. Send `/start` to your bot from your Telegram account. On the VPS, with the two values privately available, obtain your chat ID from Telegram's getUpdates endpoint (avoid pasting the token in a browser or shell history):

```bash
read -rsp 'Telegram bot token: ' TGB; echo
printf 'url = "https://api.telegram.org/bot%s/getUpdates"\n' "$TGB" | curl -s --config -
unset TGB
```

Find `message.chat.id` in the returned JSON. Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` to `/opt/neo-expiry/.env` using sudoedit. Restart the service only while flat or before the next session. The bot sends startup, data initialization/check failure, short-entry, stop-fill/trail, exit reason, completed-basket gross P&L and session-end gross P&L notifications. Alerts are best-effort on a bounded background queue; duplicate identical messages within five minutes are suppressed. Telegram failure never blocks a trade simulation. Delivery is not guaranteed; use journal logs as the primary record. Leave both fields blank to disable. No account credentials are sent to Telegram.

## 5. Upgrade an already deployed paper bot

Only upgrade while all simulated baskets are flat. If you used the original package for real trading, independently verify any live positions/orders and stop that separate process before replacing files. This paper version cannot manage real positions.

```bash
sudo systemctl disable --now neo-paper.timer
sudo systemctl stop neo-paper.service
# Upload/unzip the new ZIP as above, then in neo_expiry_bot_paper:
sudo bash deploy/install.sh
sudoedit /opt/neo-expiry/.env
sudo -u neobot /opt/neo-expiry/run.sh --mode paper --preflight
sudo systemctl enable --now neo-paper.timer
```

The installer retains existing `.env`, `config.json`, `calendar.json` and `runtime` (including any KILL marker). Keep a protected backup. Check preserved settings before resuming; no migration of live journals is performed.

## 6. Monitoring

```bash
sudo journalctl -u neo-paper.service -n 100 --no-pager
sudo cat /opt/neo-expiry/runtime/paper/state.json
sudo cat /opt/neo-expiry/runtime/paper/trades.csv
# Request simulated flatten and prevent new paper entries:
sudo -u neobot touch /opt/neo-expiry/runtime/paper/KILL
```

`trades.csv` appears after the first completed basket and includes gross P&L. `state.json` and `paper_book.json` preserve the simulated order journal. Do not delete or edit them to force retries. The persistent KILL marker blocks new entries until removed after review. It does not affect your real Kotak account. SIGTERM requests simulated flatten; stop the service while flat. Service logs and runtime files use local disk; monitor disk and RAM (`df -h`, `free -h`).

## 7. Tests

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile bot.py alerts.py
```

Tests use synthetic market data or mocked SDK/Telegram. See TEST_RESULTS.txt. Passing these does not verify live data connectivity, actual Telegram delivery, VPS installation, real margin or strategy performance.

Sources: official SDK https://github.com/Kotak-Neo/kotak-neo-python ; market-data authentication https://github.com/Kotak-Neo/kotak-neo-python/blob/main/docs/functions/market_data/option_chain.md ; expiry dates https://github.com/Kotak-Neo/Kotak-Neo/blob/main/docs/market-data-apis/expiries.md ; Telegram Bot API https://core.telegram.org/bots/api#sendmessage .
