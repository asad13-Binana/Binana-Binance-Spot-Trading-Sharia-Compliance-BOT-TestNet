# Exact Verification Commands

```bash
python -m compileall -q .
python -m unittest discover -s tests -p 'test*.py' -v
python tests/secret_scan.py
python -m services.universe_service.validate_sharia shared/sharia/sharia_status.json
python scripts/build_manifest.py
python scripts/verify_manifest.py
find . -name '*.json' -type f -print0 | xargs -0 -n1 python -m json.tool >/dev/null
bash -n deploy/*.sh freqtrade/deploy/*.sh freqtrade/scripts/*.sh
python legacy_core/binance_bot_V4.9.16_ALL_IN_ONE.py --selftest
```

Static tools used where available:

```bash
pyflakes services scripts tests
ruff check --select F,E9 services scripts tests
bandit -r services scripts -f txt
```

Docker/Compose, GitHub Actions, Binance Spot Testnet and Oracle commands must run in their respective external environments and are not represented as locally completed.
