# Self-hosted Sharia screening on Oracle

The active screening backend is `local-oracle-v1`. It uses no ChatGPT,
OpenAI, Anthropic, hosted model, model key, or separately billed screening
API. It fetches public HTTPS sources directly from the Oracle host, parses
HTML and text-bearing PDFs locally, stores the exact response bytes by
SHA-256, and applies the pinned V19.1 controller through deterministic rules.

The rules engine cannot directly authorise a trade. It creates a fail-closed
`NO_TRADE_INFO` review proposal. The Telegram owner reads the quoted evidence
with `/shariareport BASE` and chooses APPROVE or REJECT. That decision travels
over a dedicated HMAC key. The screener then verifies the one-time decision,
the asset and request identity, the exact proposal-file SHA-256, all retained
source-byte hashes, every GREEN proof check, and any explicit disclaimer-scope
confirmation. Only that complete path can create an Ed25519-attested GREEN
record. The execution gate rejects the record after seven days.

## Source registry

Ticker-to-project identity is not guessed. Before an asset can be screened,
the owner must add an entry to the persistent file configured by
`SHARIA_SOURCE_REGISTRY` (normally
`/var/lib/binance-freqtrade-v101/shared/sharia/source_registry.json`). Release
upgrades do not overwrite this file.

Each asset entry requires:

- one or more exact official project hosts;
- at least one identity-matched official HTTPS source;
- evidence-bound `token_type`, `utility`, and `revenue` claims whose verbatim
  quotes occur in a fetched source;
- a value, quote and real host-bound URL for each named Sharia screener used
  by the V19.1 proof gate.

Example structure (illustrative placeholders only; it cannot pass as shipped):

```json
{
  "schema_version": 1,
  "assets": {
    "BASE": {
      "official_hosts": ["official-project.example"],
      "sources": [
        {
          "url": "https://official-project.example/whitepaper.pdf",
          "identity_match": true
        },
        {
          "url": "https://musaffa.com/asset/base",
          "identity_match": false
        }
      ],
      "claims": {
        "token_type": {
          "value": "PAYMENT",
          "quote": "[exact quote from the official source]",
          "url": "https://official-project.example/whitepaper.pdf"
        },
        "utility": {
          "value": "real utility",
          "quote": "[exact utility quote from the official source]",
          "url": "https://official-project.example/whitepaper.pdf"
        },
        "revenue": {
          "value": "clean",
          "quote": "[exact revenue quote from the official source]",
          "url": "https://official-project.example/whitepaper.pdf"
        }
      },
      "screeners": {
        "musaffa": {
          "value": "[verdict shown by Musaffa]",
          "quote": "[exact quote shown by Musaffa]",
          "url": "https://musaffa.com/asset/base"
        }
      }
    }
  }
}
```

All seven controller-named screeners must be present for a GREEN proposal:
`cryptoummah`, `sharlife`, `islamicfinanceguru`, `saraf`, `halalscreener`,
`gethalalcrypto`, and `musaffa`. Each name is report-bound to fetched evidence
and structurally bound to its real domain. Missing, blocked, redirected,
changed or contradictory evidence fails closed.

## Oracle secrets and settings

Generate a separate random value for `SHARIA_APPROVAL_HMAC_KEY`; only the
Telegram broker and Sharia screener receive it. Keep the existing request,
result and Ed25519 keys separate. Use:

```bash
python -c "import secrets; print(secrets.token_hex(24))"
```

Keep `SHARIA_SIGNAL_GATE_MODE=cached`, `SHARIA_RESCAN_INTERVAL_DAYS=7`, and
the source/evidence paths on the persistent shared disk. The installer and
Compose file enforce the cached design and seed the registry only when it is
absent.

## Operator flow

1. Register and independently verify the asset's official and screener URLs.
2. Run `/scan BASE/USDT` in the owner Telegram chat.
3. Run `/shariareport BASE` and read the disposition, proof checks, exact
   report hash and quoted adverse/disclaimer evidence.
4. Select REJECT to keep the asset blocked, or APPROVE only when the card is
   mechanically promotable and the evidence supports that decision.
5. Confirm the signed cache shows the expected result. The weekly scanner
   re-fetches the sources; stale or changed evidence blocks new entries until
   a new exact proposal is approved.

This is research screening only, not a fatwa and not evidence that the trading
strategy is profitable.
