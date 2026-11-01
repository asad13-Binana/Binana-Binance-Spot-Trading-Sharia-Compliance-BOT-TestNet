# Build only the allowlisted services context emitted by the Oracle bundler.
FROM python:3.12.15-slim-trixie@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app HOME=/tmp
COPY requirements.services.lock /app/
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.services.lock \
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
