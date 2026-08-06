# Telegram Menu and Handler Coverage

**Authorization boundary:** exact owner sender ID and private owner chat; unauthorized updates are rejected. Dangerous actions use one-time expiring confirmation tokens. Sidecar commands use durable command IDs and an `IN_PROGRESS` claim before side effects.

| Command/control | Handler/action | Confirmation | Exchange/state effect | Coverage |
|---|---|---:|---|---|
| `/start` | `entries_on_confirm` → `resume_entries` | Yes | Fresh universe/risk checks; arm sidecar; start Freqtrade | callback expiry/replay, strict boolean, stale universe, owner auth |
| `/stop`, `/pause` | `entries_off` | No | Disarms sidecar **before** Freqtrade pause | fail-closed ordering regression |
| `/status`, `/balance`, `/profit`, `/logs` | read-only routes | No | Read-only status/audit | offline mock coverage; live API unavailable |
| `/restartws`, `/reload` | confirmed actions | Yes | Restart stream / reload config+Sharia | failure truthfulness tests |
| `/fixed_oco`, `/trailing_only`, `/oco_trailing` | `set_mode` | Yes | Changes default protection mode | request-builder tests |
| `/convert` | `convert` | Yes | Non-atomic cancel/replace with prevalidation and reconciliation latch | conversion fault tests |
| `/breakeven`, `/lockprofit` | replacement commands | Yes | Replaces protection; never lowers stop | numeric and request tests |
| `/emergency` | emergency exit | Yes | Sidecar-owned exit; entries remain paused | failure truthfulness + durable claim |
| `/setsize`, `/setmax` | sizing controls | Yes | Preserved core sizing/slot settings | finite/range/result tests |
| `/reconcile` | reconcile | No | Exchange/local state reconciliation | offline adapter tests; real Testnet blocked |
| `/universe`, `/sharia`, `/deploy`, `/lastsignal`, `/settings`, `/selftest`, `/backtest` | read-only routes | No | No exchange action | static handler map and full suite |

## Unverified external Telegram paths

Real Telegram API delivery failure, long polling interruption, Oracle restart during a confirmed command, and real exchange timeout after acceptance remain blocked by the missing external environment.
