# Read-only API credential preflight

The repository includes a root-run preflight for the four external API groups
used by the deployed stack. It verifies authentication and identity binding;
it does **not** place, test, cancel, amend, or otherwise modify an order.
The Oracle monitoring installer also schedules the same preflight every six
hours. Provider failures are reported as health failures and do not silently
change execution mode or trading behaviour.

## What it verifies

| Provider | Read-only request | What PASS means |
|---|---|---|
| Binance Spot | server time, then signed `GET /api/v3/account` | the package-bound TestNet or LIVE credentials authenticate for account data; `canTrade` is reported without exposing balances |
| Telegram | `getMe`, then `getChat` for `TELEGRAM_OWNER_CHAT_ID` | the bot token is valid and the configured owner chat is accessible |
| CoinGecko | authenticated Demo `GET /ping` | the free Demo key is accepted |
| CoinMarketCap | authenticated `GET /v1/key/info` | the free key is accepted; this endpoint costs no API credit, although it counts toward the minute rate limit |

The package mode is read from the installed immutable release. A TestNet
package cannot be redirected to the production Binance endpoint, and a LIVE
package cannot be redirected to TestNet by a command-line option.

## Oracle procedure

1. Install the release and put real credentials only in
   `/etc/binana-freqtrade-v101/.env` (`root:root`, mode `0600`).
2. Run:

   ```bash
   sudo /opt/binana-freqtrade-v101/current/deploy/api_preflight.sh
   ```

3. Expect `api_readiness=PASS` and `network_operations=GET_ONLY_NO_ORDERS`.
4. Read the sanitised status through the authenticated loopback monitoring API
   under `api_readiness`, or directly as root from
   `/var/lib/binana-freqtrade-v101/shared/runtime/api_readiness_status.json`.

The status contains no API key, secret, Telegram token, chat title, account
balance, asset balance, order, trade or wallet information. A failed required
provider makes the overall result fail. CoinGecko and CoinMarketCap are
required while automatic Sharia source discovery is enabled.

## What this does not prove

A PASS is credential and endpoint readiness only. It is not an authenticated
order-lifecycle test, an Oracle soak test, a strategy-profitability result, a
Sharia approval, or LIVE-money certification. This TestNet package remains
structurally incapable of LIVE execution; the separate LIVE package must stay
in simulation until its signed promotion evidence is complete.

## Official API references

- [Binance Spot REST security and signing](https://developers.binance.com/en/docs/products/spot/rest-api)
- [Binance account information endpoint](https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/account)
- [Telegram Bot API `getMe` and `getChat`](https://core.telegram.org/bots/api)
- [CoinGecko Demo API key setup](https://docs.coingecko.com/docs/setting-up-your-api-key)
- [CoinMarketCap key information endpoint](https://coinmarketcap.com/api/documentation/pro-api-reference/tools)
