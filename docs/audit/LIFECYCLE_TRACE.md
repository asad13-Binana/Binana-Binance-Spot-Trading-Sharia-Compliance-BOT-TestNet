# Full Lifecycle Trace

| Transition | Preconditions | Responsible component | Persistent state / external action | Failure/idempotency/restart behavior | Evidence |
|---|---|---|---|---|---|
| Host boot → service startup | supported memory, Docker, private env | Oracle/systemd + root Compose | immutable release symlink; persistent shared volume | health failure rolls back; entries remain paused | installer static tests; Oracle runtime blocked |
| Config/secrets → live gate | exact installed release hash | sidecar `live_interlock` | installed hash, env hash, marker must match | mismatch aborts non-simulation mode | InstalledReleaseLiveGateTests |
| Startup → reconciliation | sidecar initialized | sidecar main/reconciler | entries false; exchange/local reconciliation | owner confirmation required after restart | startup state tests |
| Universe → final pairlist | active Spot/USDT, liquidity, filters, HALAL | universe service | durable historical snapshot then current pointer | stale/future/malformed universe disarms entries | universe tests |
| Candle → signal | closed current candle and entry conditions | Freqtrade strategy | atomic signal file with deterministic token | Freqtrade confirm entry always false | AST preservation + simulation integration |
| Signal → entry | current snapshot/hash/Sharia/risk/size/filters | order manager + preserved broker | `IN_PROGRESS` claim before exchange submit | unknown outcome pauses/reconciles; no replay | DurableClaimTests |
| Fill → protection | filled quantity known | broker/user stream/state store | order-list IDs, filled/protected qty, events | partial fills retained; naked/uncertain state reconciled | persistence and terminal semantics tests |
| Protection conversion | owner confirmation, active position, valid filters | core adapter | replacement intent persisted, entries paused | non-atomic; accepted uncertainty never blindly duplicates | conversion tests |
| Exit/stop → fee/state | exchange event | state store/risk guard | actual commission asset, closed/stop state | duplicate event ignored; fresh signal needed for re-entry | persistence/risk tests |
| Network/process failure → restart | persistent DB/files and exchange orders | supervisor/reconciler | no replay of IN_PROGRESS operations | owner review and resume required | offline restart/idempotency tests; external soak blocked |
| Graceful shutdown | stop signal | service main loops/Compose | exchange-native protection remains | shutdown/host reboot runtime not proven locally | external blocker |
