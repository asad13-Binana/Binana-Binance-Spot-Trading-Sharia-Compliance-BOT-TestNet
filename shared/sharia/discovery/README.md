# Sharia source-discovery records

The `current/` and `archive/` directories are runtime storage on the Oracle
persistent volume. The source tree keeps only these directory placeholders;
generated records are deliberately ignored by Git.

Each current record binds one Binance Spot/USDT symbol to a CoinGecko or
CoinMarketCap candidate identity and its advertised official website and,
when available, whitepaper. Historical snapshots are retained for the
configured 90-day period. These records are source candidates only:
`owner_verified` and `trade_permission` are always false.

The runtime `current/_binance_spot_usdt_index.json` file records coverage for
the complete current Binance Spot/USDT universe, including missing, stale and
delisted-orphaned candidate records. It records a full-sweep completion time
only when every listed base has a valid record that is not due for refresh.
The index is also non-authoritative and can never grant trade permission.

The signed V19.1 report, exact content hashes, claim bindings, named Sharia
screener evidence and owner approval remain mandatory before a trade-eligible
Sharia status can be created.
