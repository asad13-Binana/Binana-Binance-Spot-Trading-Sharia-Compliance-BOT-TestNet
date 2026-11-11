# Build only the allowlisted services context emitted by the Oracle bundler.
FROM python:3.14.10-slim-trixie@sha256:3cabfd7d7aaf394e6040e79fa1eec6ca1c52b74ed9188650472695d9a9326999 AS native
RUN apt-get -o Acquire::Retries=0 update \
    && apt-get -o Acquire::Retries=0 install -y --no-install-recommends build-essential pkg-config xz-utils
COPY build_native_runtime.sh /tmp/build_native_runtime.sh
RUN sh /tmp/build_native_runtime.sh

FROM python:3.12.15-slim-trixie@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704 AS builder
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app HOME=/tmp
# Disable retries while upgrading the base's vulnerable APT (CVE-2026-109518).
RUN apt-get -o Acquire::Retries=0 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update \
    && apt-get -o Acquire::Retries=0 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 install -y --no-install-recommends \
        apt=3.0.4 libapt-pkg7.0=3.0.4 zlib1g=1:1.3.dfsg+really1.3.1-1+deb13u1 \
        libgomp1 libgdbm-compat4t64 libdb5.3t64 libzstd1 \
        liblzma5=5.8.1-1+deb13u2 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.services.lock requirements.build.lock /app/
RUN python -m pip install --no-cache-dir --no-deps --require-hashes --only-binary=:all: -r requirements.build.lock \
    && python -m pip install --no-cache-dir --no-deps --require-hashes -r requirements.services.lock \
    && python -m pip check \
    && groupadd --gid 987 binana \
    && useradd --uid 999 --gid 987 --no-create-home --shell /usr/sbin/nologin binana
COPY services /app/services
COPY runtime_stable_panel.py durable_outbox.py owner_monitor.py validate_support.py oracle_panel.py /app/
COPY controllers /app/controllers
COPY RELEASE_MODE RELEASE_SHA256.txt LICENSE /app/
RUN chmod 0444 /app/RELEASE_MODE /app/RELEASE_SHA256.txt
COPY stage_runtime.py /tmp/stage_runtime.py
RUN python /tmp/stage_runtime.py && python -m pip uninstall -y pip

FROM gcr.io/distroless/cc-debian13:nonroot@sha256:cc66d222f0a7ec996b78a99fe76a969bc80b539a5016ad866c14d1949bc6d0ab
ENV PATH=/usr/local/bin LD_LIBRARY_PATH=/usr/local/lib \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app HOME=/tmp
COPY --from=builder /usr/local /usr/local
COPY --from=builder /runtime-extra/ /
COPY --from=native /opt/native/lib/ /usr/local/lib/
COPY --from=native /opt/native/share/binana-native-runtime.json /usr/local/share/binana-native-runtime.json
COPY --from=native /tmp/native-source/ncurses-6.6/COPYING /usr/local/share/licenses/ncurses-6.6.txt
COPY --from=native /tmp/native-source/util-linux-2.42.4/libuuid/COPYING /usr/local/share/licenses/libuuid-2.42.4.txt
COPY --from=builder /etc/passwd /etc/group /etc/
COPY --from=builder /app /app
COPY validate_runtime_security.py /usr/local/share/validate_runtime_security.py
WORKDIR /app
USER 999:987
# Building/running this image without a service command cannot start a bot.
CMD ["python", "-c", "print('BINANA support image: choose an explicit service in compose.json')"]
