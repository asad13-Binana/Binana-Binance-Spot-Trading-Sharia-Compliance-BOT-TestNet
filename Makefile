# Imported from the V10 operational shell after review against the V8.1 issue
# ledger (developer convenience only; no execution-path changes).
SHELL := /bin/bash

.PHONY: verify test selftest audit manifest health secretscan

verify:
	bash deploy/verify_release.sh

test:
	python -m unittest discover -s tests -p 'test_*.py' -v

selftest:
	bash scripts/run_legacy_selftests.sh

audit:
	python -m pip_audit -r requirements.services.txt --strict

manifest:
	python scripts/build_manifest.py && python scripts/verify_manifest.py

health:
	bash scripts/healthcheck.sh

secretscan:
	python tests/secret_scan.py
