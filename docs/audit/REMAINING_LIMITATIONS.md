# Remaining Risks and Limitations

1. **No Binance Spot Testnet lifecycle run.** Endpoint acceptance and real event ordering for OTO/OTOCO/OCO/trailing, partial fills, accepted timeouts, cancel/fill races and restart remain unproven.
2. **Protection conversion is non-atomic.** Cancel-and-replace can create a brief protection gap even with prevalidation, persisted intent, entry pause and emergency recovery.
3. **No Oracle runtime/soak.** Architecture, resource, disk, network, NTP, reboot, OOM and rollback behavior are statically prepared but not measured.
4. **No Docker build in this audit container.** The workflow requires image build and extracted-artifact retest in GitHub CI.
5. **Sharia coverage is incomplete.** The seed has only explicitly reviewed records and must never fabricate approvals to fill 50 slots.
6. **Commission asset is exchange-determined.** The bot does not buy or depend on BNB, but a buy fill may deduct commission from the acquired base asset; a per-order USDT-only fee guarantee is unavailable.
7. **Historical dynamic-universe replay is incomplete.** Exact top-50/Sharia snapshots must accumulate before unbiased replay.
8. **Dependency CVE scan was blocked by network timeout.** Exact pins were reviewed; a connected CI scanner is still required.
9. **Preserved 9,387-line legacy core.** It is byte-preserved and its 33 self-tests pass, but no audit can honestly prove zero defects or guaranteed uptime.
10. **Live hash marker is only an interlock.** Even all three matching hashes do not substitute for testnet evidence, Oracle soak or human approval.
