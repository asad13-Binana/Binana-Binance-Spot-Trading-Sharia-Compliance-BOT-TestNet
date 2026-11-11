# Build only the allowlisted services context emitted by the Oracle bundler.
FROM python:3.12.15-slim-trixie@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app HOME=/tmp
# Disable retries while upgrading the base's vulnerable APT (CVE-2026-109518).
RUN apt-get -o Acquire::Retries=0 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update \
    && apt-get -o Acquire::Retries=0 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 install -y --no-install-recommends \
        apt=3.0.4 libapt-pkg7.0=3.0.4 zlib1g=1:1.3.dfsg+really1.3.1-1+deb13u1 \
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
USER 999:987
# Building/running this image without a service command cannot start a bot.
CMD ["python", "-c", "print('BINANA support image: choose an explicit service in compose.json')"]
