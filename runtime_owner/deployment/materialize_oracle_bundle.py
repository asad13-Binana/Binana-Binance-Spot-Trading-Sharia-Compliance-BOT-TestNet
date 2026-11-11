"""Materialize a portable, stopped Spot owner bundle from public source.

No Docker, host configuration, credentials, account access, or trading commands
are executed. An optional upstream archive makes this operation fully offline.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import tempfile
import urllib.request

import materialize_clean_context as clean
import materialize_service_health as health

SUPPORT_PACKAGES = (
    "common", "market_context", "sharia_operator", "sharia_retriever",
    "sharia_rules", "sharia_screener", "sharia_egress_proxy",
    "telegram_broker", "universe_service",
)
FORBIDDEN_PACKAGES = {"execution_sidecar", "legacy_core"}
DEPLOY = Path(__file__).resolve().parent


def encode(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()


def source_bytes(root, relative):
    path = root / relative
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("SOURCE_OUTSIDE_REPOSITORY:" + relative)
    if any(p.is_symlink() for p in (path, *path.parents) if p != root.parent):
        raise ValueError("SOURCE_SYMLINK:" + relative)
    return path.read_bytes()


def missing_local_imports(services):
    """Resolve every static services import, including imports inside functions."""
    missing = set()
    for path in services.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_bytes(), str(path))):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                modules = [node.module]
            for module in modules:
                if module == "services":
                    continue
                if module.startswith("services."):
                    target = services.joinpath(*module.split(".")[1:])
                    if not (target.with_suffix(".py").is_file() or
                            (target / "__init__.py").is_file()):
                        missing.add(module)
    return sorted(missing)


def current_listing_age_cache(raw):
    """Replace only the indexed scanner's cache class with the current validator."""
    nodes = [node for node in ast.parse(raw).body
             if isinstance(node, ast.ClassDef) and node.name == "ListingAgeCache"]
    if len(nodes) != 1 or nodes[0].decorator_list:
        raise ValueError("CAPTURED_LISTING_CACHE_CLASS_MISMATCH")
    node = nodes[0]
    lines = raw.splitlines(keepends=True)
    return b"".join(lines[:node.lineno - 1] + [
        b"from services.universe_service.listing_age import ListingAgeCache\n"
    ] + lines[node.end_lineno:])


def package_environment(repo):
    """Bind packaging mode to both the repository and immutable owner profile."""
    environment = source_bytes(repo, "RELEASE_MODE").decode("ascii").strip()
    if environment not in {"testnet", "live"}:
        raise ValueError("INVALID_PACKAGE_ENVIRONMENT")
    tree = ast.parse(source_bytes(repo, "runtime_owner/owner/freqtrade/binana/package_profile.py"))
    values = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PACKAGE_ENVIRONMENT" for t in node.targets):
            values.append(ast.literal_eval(node.value))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "PACKAGE_ENVIRONMENT":
            values.append(ast.literal_eval(node.value))
    if values != [environment]:
        raise ValueError("OWNER_PACKAGE_ENVIRONMENT_MISMATCH")
    return environment


def current_monitor(raw, environment):
    """Profile only the mode label; preserve the captured monitor's lack of authority."""
    if environment not in {"testnet", "live"}:
        raise ValueError("INVALID_PACKAGE_ENVIRONMENT")
    original = b"'mode': 'testnet'"
    if raw.count(original) != 1:
        raise ValueError("CAPTURED_MONITOR_PROFILE_MISMATCH")
    return raw.replace(original, ("'mode': '" + environment + "'").encode("ascii"), 1)


def support_sources(repo):
    result = {"services/__init__.py": b""}
    for package in SUPPORT_PACKAGES:
        folder = repo / "services" / package
        for path in sorted(folder.rglob("*.py")):
            relative = path.relative_to(repo).as_posix()
            result[relative] = source_bytes(repo, relative)

    # These are the verified owner-era scanner and manual-registry dispatcher,
    # not the legacy root scanner capped at 50 with a different exclusion set.
    captured = json.loads(source_bytes(repo, "runtime_owner/deployed_services/SOURCE_INDEX.json"))
    for name in ("scanner.py", "ranker.py", "sharia_filter.py", "sharia_gate.py"):
        target = "services/universe_service/" + name
        record = captured["containers"]["universe"][target]
        raw = source_bytes(repo, record["repository_source"])
        if hashlib.sha256(raw).hexdigest() != record["sha256"]:
            raise ValueError("CAPTURED_SOURCE_MISMATCH:" + target)
        if name == "scanner.py":
            raw = current_listing_age_cache(raw)
        result[target] = raw

    market_manifest = json.loads(source_bytes(repo, "runtime_owner/market_context_repair/PROVENANCE.json"))
    for name, record in market_manifest["files"].items():
        raw = source_bytes(repo, "runtime_owner/market_context_repair/" + name)
        if hashlib.sha256(raw).hexdigest() != record["sha256"]:
            raise ValueError("MARKET_SOURCE_MISMATCH:" + name)
        result["services/market_context/" + name] = raw

    # Reuse the existing reviewed health writer transformation, not a new
    # implementation or an unverified retained Docker image.
    with tempfile.TemporaryDirectory() as temporary:
        destination = Path(temporary) / "health"
        health.materialize(repo, destination)
        for name, target in {
            "market_service.py": "services/market_context/service.py",
            "manual_registry_service.py": "services/sharia_screener/manual_registry_service.py",
            "common_atomic.py": "services/common/atomic.py",
        }.items():
            result[target] = (destination / name).read_bytes()
    # The current panel calls registry_owner_command and _sidecar_read, which
    # the historical captured broker lacks. The current reviewed broker already
    # uses the group-readable health writer and supplies those interfaces.
    result["services/telegram_broker/bot.py"] = source_bytes(repo, "services/telegram_broker/bot.py")
    for name in ("runtime_stable_panel.py", "durable_outbox.py"):
        result[name] = source_bytes(repo, "runtime_owner/runtime-fixes/telegram/" + name)
    monitor = captured["containers"]["execution-sidecar"]["monitor_only.py"]
    raw = source_bytes(repo, monitor["repository_source"])
    if hashlib.sha256(raw).hexdigest() != monitor["sha256"]:
        raise ValueError("MONITOR_SOURCE_MISMATCH")
    result["owner_monitor.py"] = current_monitor(raw, package_environment(repo))
    result["requirements.services.lock"] = source_bytes(repo, "requirements.services.lock")
    result["requirements.build.lock"] = source_bytes(repo, "runtime_owner/deployment/requirements.build.lock")
    result["RELEASE_MODE"] = (package_environment(repo) + "\n").encode("ascii")
    result["Dockerfile"] = source_bytes(repo, "runtime_owner/deployment/oracle_services.Dockerfile")
    result["validate_support.py"] = source_bytes(repo, "runtime_owner/deployment/oracle_validate_support.py")
    result["oracle_panel.py"] = source_bytes(repo, "runtime_owner/deployment/oracle_panel.py")
    controller = "HALAL_CRYPTO_SPOT_SCREENING_V19_3_PRODUCTION.json"
    result["controllers/" + controller] = source_bytes(repo, "shared/sharia/" + controller)
    result["LICENSE"] = source_bytes(repo, "LICENSE")
    return result


def reference_config(repo):
    config = json.loads(source_bytes(repo, "runtime_owner/deployment/owner_policy.reference.json"))["policy"]
    environment = package_environment(repo)
    policy = config["binana"]
    policy.update(environment=environment, allow_live=False)
    config["exchange"]["binana_testnet"] = environment == "testnet"
    if environment == "testnet":
        policy["testnet_epoch_id"] = "REPLACE_WITH_RECONCILED_TESTNET_EPOCH"
    else:
        policy.pop("testnet_epoch_id", None)
        policy.update(live_activation="", exchange_epoch_id="REPLACE_WITH_RECONCILED_LIVE_EPOCH",
                      account_scope="REPLACE_WITH_UNIQUE_LIVE_ACCOUNT_SCOPE")
    config.update(
        strategy="BinanaNfiSpot", strategy_path="/freqtrade/binana-strategies",
        initial_state="stopped",
        db_url="sqlite:////freqtrade/shared/freqtrade/binana-owner.sqlite",
        bot_name="BINANA Oracle Spot " + environment.upper(),
        telegram={"enabled": False},
        api_server={"enabled": True, "listen_ip_address": "0.0.0.0",
                    "listen_port": 8080, "verbosity": "error",
                    "enable_openapi": False, "CORS_origins": []},
    )
    return config


def required(name):
    return "${" + name + ":?set " + name + " in the private Oracle environment}"


def bind(source, target, *, read_only=False):
    return {"type": "bind", "source": source, "target": target,
            "read_only": read_only, "bind": {"create_host_path": False}}


def topology(bundle_id, environment="testnet"):
    if environment not in {"testnet", "live"}:
        raise ValueError("INVALID_PACKAGE_ENVIRONMENT")
    key_prefix = "BINANCE_" + environment.upper() + "_API_"
    state = required("BINANA_STATE_DIR")
    config = required("BINANA_CONFIG_DIR")
    release = required("BINANA_RELEASE_DIR")
    common_env = {
        "BOT_PRODUCT": "BINANA", "BOT_ENVIRONMENT": environment.upper(),
        "BOT_INSTANCE_ID": required("BOT_INSTANCE_ID"),
        "SHARED_ROOT": "/app/shared", "PYTHONDONTWRITEBYTECODE": "1",
        "ENVELOPE_RELEASE_HASH": bundle_id,
        "SHARIA_RESULT_VERIFY_PUBLIC_KEY_B64": required("SHARIA_RESULT_VERIFY_PUBLIC_KEY_B64"),
    }
    hardened = {
        "user": "999:987", "read_only": True, "init": True,
        "restart": "unless-stopped", "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"], "pids_limit": 192,
        "tmpfs": ["/tmp:rw,nosuid,nodev,size=256m,mode=1777"],
        "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
    }

    def support(command, *, memory="512m"):
        return {**hardened, "image": "binana-oracle-services:" + bundle_id[:20],
                "build": {"context": "./services", "dockerfile": "Dockerfile"},
                "command": command, "environment": dict(common_env),
                "volumes": [bind(state, "/app/shared"),
                            bind(state + "/freqtrade", "/app/shared/freqtrade", read_only=True)],
                "networks": ["control", "public-egress"], "mem_limit": memory}

    def json_health(path, ttl, *, ok=True):
        expression = "import json,time;d=json.load(open(" + repr(path) + "));"
        expression += ("assert d.get('ok') is True;" if ok else "")
        expression += "assert 0<=time.time()-float(d['ts'])<" + str(ttl)
        return {"test": ["CMD", "python", "-c", expression], "interval": "30s",
                "timeout": "8s", "retries": 5, "start_period": "60s"}

    owner = {
        **hardened, "image": "binana-oracle-owner:" + bundle_id[:20],
        "build": {"context": "./owner", "dockerfile": "Dockerfile.clean"},
        "entrypoint": ["/usr/local/bin/freqtrade"],
        "command": [
            "trade", "--config", "/freqtrade/binana-config/config.json",
            "--strategy", "BinanaNfiSpot", "--strategy-path", "/freqtrade/binana-strategies",
            "--db-url", "sqlite:////freqtrade/shared/freqtrade/binana-owner.sqlite",
            "--logfile", "/freqtrade/shared/freqtrade/logs/freqtrade-owner.log",
        ],
        "environment": {
            "PYTHONDONTWRITEBYTECODE": "1", "OPENBLAS_NUM_THREADS": "1",
            "FREQTRADE__INITIAL_STATE": "stopped",
            "FREQTRADE__DRY_RUN": "false", "FREQTRADE__TRADING_MODE": "spot",
            "FREQTRADE__EXCHANGE__NAME": "binance",
            "FREQTRADE__EXCHANGE__BINANA_TESTNET": str(environment == "testnet").lower(),
            "FREQTRADE__EXCHANGE__KEY": required(key_prefix + "KEY"),
            "FREQTRADE__EXCHANGE__SECRET": required(key_prefix + "SECRET"),
            "FREQTRADE__API_SERVER__USERNAME": required("FREQTRADE_API_USERNAME"),
            "FREQTRADE__API_SERVER__PASSWORD": required("FREQTRADE_API_PASSWORD"),
            "FREQTRADE__API_SERVER__JWT_SECRET_KEY": required("FREQTRADE_API_JWT_SECRET"),
            "FREQTRADE__API_SERVER__WS_TOKEN": required("FREQTRADE_API_WS_TOKEN"),
        },
        "volumes": [
            bind(state, "/freqtrade/shared", read_only=True),
            bind(state + "/freqtrade", "/freqtrade/shared/freqtrade"),
            bind(state + "/freqtrade", "/freqtrade/user_data"),
            bind(config, "/freqtrade/binana-config", read_only=True),
            bind(release, "/freqtrade/binana-release", read_only=True),
        ],
        "ports": ["127.0.0.1:" + ("8090" if environment == "testnet" else "8091") + ":8080"],
        "networks": ["control", "public-egress"],
        "mem_limit": "4g", "cpus": 1.1, "stop_grace_period": "2m",
        "depends_on": {"universe": {"condition": "service_healthy"}},
        "healthcheck": {"test": ["CMD", "python", "-c",
            "import urllib.request;assert urllib.request.urlopen('http://127.0.0.1:8080/api/v1/ping',timeout=4).status==200"],
            "interval": "30s", "timeout": "8s", "retries": 8, "start_period": "90s"},
    }
    universe = support(["python", "-m", "services.market_context.universe_main"], memory="1g")
    universe["volumes"].append(bind(state + "/sharia", "/app/shared/sharia", read_only=True))
    universe["cpus"] = .5
    universe["environment"].update({
        "BINANCE_PUBLIC_BASE": "https://data-api.binance.vision",
        "BINANCE_SPOT_MARKET_STREAM": "wss://data-stream.binance.vision/ws",
        "UNIVERSE_LIMIT": "${UNIVERSE_LIMIT:-0}", "UNIVERSE_REFRESH_SECONDS": "900",
        "MIN_LISTING_AGE_DAYS": "30", "MIN_QUOTE_VOLUME_USDT": "1000000",
        "MAX_SPREAD_RATIO": "0.005", "ENABLE_SPOT_MICROSTRUCTURE": "true",
        "MARKET_CONTEXT_PUBLISH_SECONDS": "5", "MARKET_CONTEXT_MAX_AGE_MS": "15000",
        "MARKET_CONTEXT_EVIDENCE_MAX_AGE_SECONDS": "30",
        "ENABLE_COINGECKO_SIGNALS": "${ENABLE_COINGECKO_SIGNALS:-false}",
        "ENABLE_CMC_TRENDING": "${ENABLE_CMC_TRENDING:-false}",
        "COINGECKO_API_KEY": "${COINGECKO_API_KEY:-}",
        "COINMARKETCAP_API_KEY": "${COINMARKETCAP_API_KEY:-}",
    })
    universe["healthcheck"] = json_health("/app/shared/runtime/market_context/health.json", 90)
    universe["depends_on"] = {"sharia-screener": {"condition": "service_healthy"}}
    sharia = support(["python", "-m", "services.sharia_screener.manual_registry_service"])
    sharia.pop("networks")
    sharia["network_mode"] = "none"
    sharia["cpus"] = .05
    sharia["volumes"].append(bind(state + "/universe", "/app/shared/universe", read_only=True))
    sharia["environment"].update({
        "SHARIA_REGISTRY_MODE": "manual",
        "SHARIA_APPROVAL_HMAC_KEY": required("SHARIA_APPROVAL_HMAC_KEY"),
        "SHARIA_RESULT_HMAC_KEY": required("SHARIA_RESULT_HMAC_KEY"),
        "SHARIA_RESULT_SIGNING_PRIVATE_KEY_B64": required("SHARIA_RESULT_SIGNING_PRIVATE_KEY_B64"),
    })
    sharia["healthcheck"] = json_health("/app/shared/runtime/sharia_screener/health.json", 180)
    telegram = support(["python", "/app/oracle_panel.py"])
    telegram["cpus"] = .1
    telegram["volumes"] += [
        bind(state + "/sharia", "/app/shared/sharia", read_only=True),
        bind(state + "/universe", "/app/shared/universe", read_only=True),
        bind(config + "/config.json", "/freqtrade/binana-config/config.json", read_only=True),
        bind(release, "/app/binana-release", read_only=True),
    ]
    telegram["environment"].update({
        "TELEGRAM_BOT_TOKEN": required("TELEGRAM_BOT_TOKEN"),
        "TELEGRAM_OWNER_CHAT_ID": required("TELEGRAM_OWNER_CHAT_ID"),
        "FREQTRADE_API_URL": "http://freqtrade:8080/api/v1",
        "FREQTRADE_API_USERNAME": required("FREQTRADE_API_USERNAME"),
        "FREQTRADE_API_PASSWORD": required("FREQTRADE_API_PASSWORD"),
        "FREQTRADE_API_WS_TOKEN": required("FREQTRADE_API_WS_TOKEN"),
        "EXECUTION_MODE": environment,
        "BINANA_RESEARCH_ENABLED": "${BINANA_RESEARCH_ENABLED:-false}",
        **{name: required(name) for name in (
            "SIGNAL_HMAC_KEY", "COMMAND_HMAC_KEY", "SHARIA_HMAC_KEY",
            "SHARIA_APPROVAL_HMAC_KEY", "SHARIA_RESULT_HMAC_KEY")},
    })
    telegram["healthcheck"] = json_health("/app/shared/runtime/telegram_health.json", 90)
    telegram["depends_on"] = {"freqtrade": {"condition": "service_healthy"}}
    monitor = support(["python", "/app/owner_monitor.py"], memory="128m")
    monitor["cpus"] = .05
    monitor.pop("networks")
    monitor["network_mode"] = "none"
    # The observer publishes compatibility summaries only. It must not be
    # able to edit the operational registry, universe or canonical databases.
    monitor["volumes"] = [bind(state, "/app/shared", read_only=True),
                          bind(state + "/runtime", "/app/shared/runtime")]
    monitor["environment"] = {"SHARED_ROOT": "/app/shared", "MONITOR_INTERVAL_SECONDS": "10"}
    monitor["healthcheck"] = json_health("/app/shared/runtime/sidecar_health.json", 40)
    research = support(["python", "-m", "services.sharia_screener.service"], memory="768m")
    research.update(profiles=["research"], cpus=.15, networks=["sharia-egress"])
    research["volumes"] = [
        bind(state, "/app/shared", read_only=True),
        bind(state + "/sharia_research", "/app/shared/sharia_research"),
        bind(state + "/runtime/sharia_research", "/app/shared/runtime/sharia_research"),
        bind(state + "/audit", "/app/shared/audit"),
    ]
    research["environment"].update({
        "SHARIA_FILE": "/app/shared/sharia_research/sharia_status.json",
        "LEGACY_HALAL_FILE": "/app/shared/sharia_research/halal_coins.json",
        "SHARIA_RUNTIME_DIR": "/app/shared/runtime/sharia_research",
        "SHARIA_QUEUE_INBOX": "/app/shared/sharia_research/queue/inbox",
        "SHARIA_QUEUE_PROCESSED": "/app/shared/sharia_research/queue/processed",
        "SHARIA_DECISION_INBOX": "/app/shared/sharia_research/decisions/inbox",
        "SHARIA_DECISION_PROCESSED": "/app/shared/sharia_research/decisions/processed",
        "SHARIA_CONTROLLER_FILE": "/app/controllers/HALAL_CRYPTO_SPOT_SCREENING_V19_3_PRODUCTION.json",
        "SHARIA_PINNED_EGRESS_PROXY": "true",
        "HTTPS_PROXY": "http://sharia-egress-proxy:8080",
        "HTTP_PROXY": "http://sharia-egress-proxy:8080",
        "NO_PROXY": "localhost,127.0.0.1",
        "BINANCE_PUBLIC_BASE": "https://data-api.binance.vision",
        "COINGECKO_API_KEY": "${COINGECKO_API_KEY:-}",
        "COINMARKETCAP_API_KEY": "${COINMARKETCAP_API_KEY:-}",
        **{name: required(name) for name in (
            "SHARIA_HMAC_KEY", "SHARIA_APPROVAL_HMAC_KEY", "SHARIA_RESULT_HMAC_KEY",
            "SHARIA_RESULT_SIGNING_PRIVATE_KEY_B64")},
    })
    research["healthcheck"] = json_health("/app/shared/runtime/sharia_research/health.json", 180)
    research["depends_on"] = {"sharia-egress-proxy": {"condition": "service_healthy"}}
    proxy = support(["python", "-m", "services.sharia_egress_proxy.server"], memory="128m")
    proxy.update(profiles=["research"], cpus=.05, networks=["sharia-egress", "public-egress"],
                 volumes=[], environment={"SHARIA_PROXY_LISTEN_HOST": "0.0.0.0",
                                          "SHARIA_PROXY_LISTEN_PORT": "8080"})
    proxy["healthcheck"] = {
        "test": ["CMD", "python", "-c",
                 "import socket;s=socket.create_connection(('127.0.0.1',8080),3);s.close()"],
        "interval": "30s", "timeout": "5s", "retries": 5,
    }
    return {
        "name": "binana-oracle-" + environment,
        "services": {"freqtrade": owner, "universe": universe, "sharia-screener": sharia,
                     "telegram-broker": telegram, "owner-monitor": monitor,
                     "sharia-research": research, "sharia-egress-proxy": proxy},
        "networks": {"control": {"internal": True}, "public-egress": {},
                     "sharia-egress": {"internal": True}},
    }


def materialize(repo, destination, raw):
    repo, destination = Path(repo).resolve(), Path(destination)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    clean.source_files(raw)  # Reject a wrong archive before creating the destination.
    environment = package_environment(repo)
    sources = support_sources(repo)
    destination.mkdir(parents=True, exist_ok=False)
    receipt = clean.clean_context(repo / "runtime_owner", destination / "owner", raw)
    receipt["context"] = "owner"
    (destination / "owner/CLEAN_SOURCE.json").write_bytes(encode(receipt))
    # The legacy retained-image Dockerfile is unnecessary and dangerous here.
    (destination / "owner/Dockerfile").unlink(missing_ok=True)
    for relative, content in sources.items():
        target = destination / "services" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    missing = missing_local_imports(destination / "services/services")
    if missing:
        raise ValueError("INCOMPLETE_SUPPORT_IMPORT_GRAPH:" + ",".join(missing))
    for path in (destination / "services/services").rglob("*.py"):
        if set(path.parts) & FORBIDDEN_PACKAGES:
            raise ValueError("FORBIDDEN_ORDER_ENGINE")
        compile(path.read_bytes(), str(path), "exec")
    (destination / "config.example.json").write_bytes(encode(reference_config(repo)))
    (destination / "research-source-registry.example.json").write_bytes(
        source_bytes(repo, "shared/sharia/source_registry.json"))
    for source, target in (
        ("runtime_owner/deployment/oracle_preflight.py", "oracle_preflight.py"),
        ("runtime_owner/deployment/oracle.env.example", "oracle.env.example"),
        ("docs/ORACLE_OWNER_BUNDLE.md", "README.md"),
    ):
        (destination / target).write_bytes(source_bytes(repo, source))
    # Host-only tooling is outside both Docker build contexts. The profile is
    # a public path example; no signing key or acceptance receipt is packaged.
    maintenance = destination / "maintenance"
    for source, target in (
        ("scripts/recovery/issue_candidate_release.py", "scripts/recovery/issue_candidate_release.py"),
        ("scripts/recovery/install_release_renewal.py", "scripts/recovery/install_release_renewal.py"),
        ("scripts/recovery/deploy_candidate_owner.py", "scripts/recovery/deploy_candidate_owner.py"),
        ("scripts/recovery/release_host.py", "scripts/recovery/release_host.py"),
        ("runtime_owner/deployment/release_authority.py", "runtime_owner/deployment/release_authority.py"),
        ("runtime_owner/deployment/release-host.oracle.example.json", "release-host.oracle.example.json"),
        ("requirements.renewal.txt", "requirements.renewal.txt"),
        ("requirements.renewal.lock", "requirements.renewal.lock"),
        ("docs/recovery/RELEASE_RENEWAL_HOST.md", "README.md"),
    ):
        path = maintenance / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(source_bytes(repo, source))
    fingerprint = hashlib.sha256()
    for path in sorted((p for p in destination.rglob("*") if p.is_file()),
                       key=lambda p: p.relative_to(destination).as_posix()):
        fingerprint.update(path.relative_to(destination).as_posix().encode() + b"\0" + path.read_bytes())
    bundle_id = fingerprint.hexdigest()
    (destination / "services/RELEASE_SHA256.txt").write_text(bundle_id + "\n", encoding="ascii", newline="\n")
    (destination / "compose.json").write_bytes(encode(topology(bundle_id, environment)))
    files = {p.relative_to(destination).as_posix(): {
        "bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
        for p in sorted(destination.rglob("*")) if p.is_file()}
    manifest = {
        "schema": "binana-oracle-owner-bundle-v1", "bundle_id": bundle_id,
        "scope": "Binance Spot " + environment.upper() + "; initial state stopped; LIVE activation disabled",
        "execution_environment": environment,
        "source_ready": True, "host_accepted": False, "trading_authorized": False,
        "retained_aws_image_required": False, "files": files,
    }
    (destination / "BUNDLE_MANIFEST.json").write_bytes(encode(manifest))
    return {"bundle_id": bundle_id, "file_count": len(files),
            "host_accepted": False, "trading_authorized": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args()
    if args.archive:
        raw = args.archive.read_bytes()
    else:
        with urllib.request.urlopen(clean.UPSTREAM_URL, timeout=60) as response:
            raw = response.read(100_000_001)
        if len(raw) > 100_000_000:
            raise ValueError("UPSTREAM_ARCHIVE_TOO_LARGE")
    print(json.dumps(materialize(DEPLOY.parents[1], args.destination, raw), sort_keys=True))


if __name__ == "__main__":
    main()
