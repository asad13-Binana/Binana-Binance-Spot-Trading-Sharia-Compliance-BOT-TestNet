# Telegram owner-bot setup

This package uses one owner-only Telegram bot for status, pause/resume and
confirmed safety actions.  The bot token and the numeric owner chat ID are
**private deployment secrets**.  Put them only in the deployment `.env` file;
never paste either value into Python, `docker-compose.yml`, a Git commit, a
support message, or a release ZIP.

The ZIP intentionally contains no populated `.env`.  Extract the ZIP first,
then create a private `.env` beside `.env.example` in that extracted package.
Do not edit a code file to add Telegram details.  If testnet and live-capable
stacks will ever run at the same time, use a separate bot token for each so an
operator cannot confuse the environments.

## 1. Create the bot

1. In Telegram, open the verified **@BotFather** account.
2. Send `/newbot`, choose a display name and a unique username, and copy the
   token BotFather returns.
3. Treat the token like a password.  Anyone holding it can operate the bot
   endpoint as that bot.  If it is exposed, use BotFather's revoke-token flow,
   update the private `.env`, and restart the stack.

## 2. Obtain the owner chat ID

1. Open a **private direct chat** with the new bot and send `/start`.
2. From a private Git Bash terminal, run the standard-library helper below.
   The token is entered at a hidden prompt, is not placed in shell history,
   and the helper prints only private-chat IDs rather than the full update.

   ```bash
   python - <<'PY'
   from getpass import getpass
   import json
   import urllib.request

   token = getpass("Telegram bot token (hidden): ")
   try:
       with urllib.request.urlopen(
               f"https://api.telegram.org/bot{token}/getUpdates",
               timeout=20) as response:
           payload = json.load(response)
   except Exception:
       raise SystemExit("Telegram request failed; details suppressed to protect the token")
   found = False
   for update in payload.get("result", []):
       message = update.get("message") or update.get("edited_message") or {}
       chat = message.get("chat") or {}
       if chat.get("type") == "private" and isinstance(chat.get("id"), int):
           print(f"private owner chat ID: {chat['id']}")
           found = True
   if not found:
       print("No private update found; send the bot a new /start and run this again.")
   token = ""
   PY
   ```

3. Copy the integer shown after `private owner chat ID:`.  It is not your
   `@username`, phone number, bot username, or message ID.  Use the direct-chat
   ID only; do not configure a group, channel, or another person's chat.  The
   broker deliberately requires an exact private owner-chat match.

## 3. Add the values to the deployment environment

On the machine that will run the package, create the private deployment file
from the example:

```bash
cp .env.example .env
chmod 600 .env
```

Set only these two Telegram lines in `.env` (replace the examples with your
real private values):

```dotenv
TELEGRAM_BOT_TOKEN=REPLACE_WITH_THE_PRIVATE_TOKEN_FROM_BOTFATHER
TELEGRAM_OWNER_CHAT_ID=123456789
```

Save the file, then validate presence and shape without printing either
secret:

```bash
python - <<'PY'
import pathlib
import re
values = {}
for raw in pathlib.Path('.env').read_text(encoding='utf-8').splitlines():
    line = raw.strip()
    if line and not line.startswith('#') and '=' in line:
        key, value = line.split('=', 1)
        values[key.strip()] = value.strip()
token = values.get('TELEGRAM_BOT_TOKEN', '')
owner = values.get('TELEGRAM_OWNER_CHAT_ID', '')
print('Telegram token configured:', bool(token) and 'REPLACE_' not in token)
print('Owner chat ID is an integer:', bool(re.fullmatch(r'-?[0-9]{1,16}', owner)))
PY
```

Do **not** edit `.env.example`: it is a safe template that is intentionally
included in the package.  Do **not** put the values in
`services/telegram_broker/bot.py`, `docker-compose.yml`, the strategy, or any
other code file.  Docker Compose passes the private values to the
`telegram-broker` service at runtime.

The same two fields exist in both the testnet and live-capable packages.  The
testnet package is the required first deployment; the live-capable package
still defaults to simulation and has additional release/evidence gates.

## 4. Start and verify safely

After saving `.env`, start or restart the local stack with the package's
documented deployment command.  In the private chat, first use only
read-only commands such as `/status`, `/settings`, `/universe`, and `/sharia`.
Then verify that an unauthorized account and a group chat cannot control the
bot.  Dangerous actions display one-time confirmation controls; never treat
a Telegram message alone as proof that an exchange action succeeded—check the
bot's reported command result and reconciliation status.

Keep `TELEGRAM_BOT_TOKEN` and `TELEGRAM_OWNER_CHAT_ID` out of screenshots,
logs, copied ZIPs, and GitHub.  The release secret scan intentionally rejects
real Telegram bot tokens if one is accidentally added to the source tree.

## 5. Owner menu

`/menu` opens a bounded safe panel for Dashboard, Trading Status, Balance,
Open Trades, Trade History, Market Scanner, Sharia Status, Safety & Health,
Alerts, Bot Controls, Emergency Stop and Help. Inline-button navigation edits
the same message when Telegram permits it; if an edit is unavailable, the
broker sends the same screen as a new message. That fallback is
presentation-only and never retries a trading or Sharia command.

The Sharia section is registry-only in the default deployment. It supports
read-only coin lookups, registry health, the current approved list and update
instructions. Automatic scans, batch queues and evidence-card approval are
disabled; only a validated update to
`shared/sharia/halal_coins.json` changes the trading allowlist. A missing,
malformed, expired or unprojected registry remains fail-closed.

All existing owner-only checks, one-time confirmation tokens, durable Telegram
update claims, signed command buses and fail-closed trading controls remain in
force. Menu layout does not alter strategy, signal, execution, risk or Sharia
decision behaviour.

Official references: Telegram's BotFather guide is at
`https://core.telegram.org/bots/features#botfather`; the `getUpdates` method
and update structure are documented at `https://core.telegram.org/bots/api`.
