# Official Binance Spot Microstructure Evidence

This release adds a public, credential-free observation layer for the Binana
Spot universe. It is deliberately outside the protected `IctSmcStrategy`, the
legacy core, Sharia decisions, risk sizing, and order lifecycle.

## Implemented inputs

- `<symbol>@aggTrade`
- `<symbol>@bookTicker`

Only official Binance Spot market streams are accepted. The immutable TestNet
package uses Binance's market-data-only `data-stream.binance.vision` host so
the evidence matches the production public market observed by its Freqtrade
dry-run signal engine; authenticated order execution remains on Spot Testnet.
LIVE uses `stream.binance.com`. Configuration cannot cross those bindings.

No Futures feed, funding rate, open interest, leverage, margin, Futures API
credential, depth stream, kline stream, or third-party market provider is used.
Existing RVOL, VWAP, RSI, ADX, EMA, MACD, and candle-volume calculations are
not duplicated.

## Derived evidence

For each current universe symbol, the service publishes:

- aggressive buy/sell quote notional over 10, 30, and 60 seconds;
- taker-buy ratios over those windows;
- rolling and session-scoped Spot quote CVD;
- trade counts and trade-flow intensity;
- 10-second versus prior-20-second trade-flow acceleration;
- best bid/ask and their quantities;
- spread and spread basis points;
- top-of-book quantity pressure;
- exchange `aggTrade` time and local monotonic `bookTicker` freshness.

`m=false` is classified as aggressive BUY. `m=true` means the buyer was the
maker, so the aggressive side is SELL. `bookTicker` has no exchange event time;
the implementation does not invent one.

## Safety boundary

The non-core universe launcher starts the collector as a failure-isolated daemon
inside the existing `universe` container, then invokes the byte-preserved
scanner. That container receives no Binance trading key and has no order method.
This avoids another Oracle container and preserves the four-bot CPU and memory
envelope.

The existing non-core guarded execution launcher installs an observation-only
wrapper around signal processing. The protected `OrderManager` source remains
byte-for-byte unchanged. The wrapper freezes the latest snapshot before the
original method runs, always calls that original method exactly once, returns
its unchanged result, and records a bounded copy after processing. Every record
states:

```json
{
  "advisory_only": true,
  "used_for_trade_decision": false
}
```

Missing, malformed, disconnected, or stale context does not approve, reject,
delay, resize, or otherwise change a signal. It is evidence for later A/B
research only. Any future enforcement would be a separate strategy change and
is not part of this implementation.

## Runtime files

- `shared/market_context/current.json`
- `shared/runtime/market_context/health.json`
- `shared/market_context/signal_evidence/<signal_id>.json`

Both are runtime-only and ignored by Git. Values used for financial arithmetic
are calculated with `Decimal` and serialized as decimal strings.

## Monitoring API

The existing bearer-protected, loopback-only monitoring API exposes:

```text
GET /api/v1/market-context
GET /api/v1/market-context?symbol=ETHUSDT
```

It is read-only. It exposes public market evidence and health, never Binance,
Telegram, Sharia-provider, monitoring, or HMAC credentials.

## Oracle configuration

The defaults are ready in `.env.example`. No new API key is required. The
collector uses one multiplexed socket for at most 50 symbols / 100 streams,
one-second in-memory aggregation buckets, bounded reconnect backoff with jitter,
ping/pong, stale detection, dynamic subscription updates, and proactive
rotation before Binance's 24-hour connection lifetime.

Deployment still requires normal Oracle network access and a real TestNet soak
to prove the host can maintain the WebSocket from its assigned region.
