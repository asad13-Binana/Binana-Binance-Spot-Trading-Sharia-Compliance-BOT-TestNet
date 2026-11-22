SHELL := /bin/bash
.PHONY: verify test manifest oracle-bundle
verify:
	bash deploy/verify_release.sh
test:
	python -m pytest -q tests monitoring/tests
manifest:
	python scripts/build_manifest.py && python scripts/verify_manifest.py
oracle-bundle:
	test -n "$(DEST)"
	python runtime_owner/deployment/materialize_oracle_bundle.py "$(DEST)"
