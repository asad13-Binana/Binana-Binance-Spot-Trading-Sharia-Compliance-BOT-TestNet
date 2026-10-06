# This recovery build deliberately depends on the retained AWS r17 image.
# A clean-host rebuild of that base is a separate, still-open release blocker.
ARG BASE_IMAGE=binana-testnet-freqtrade@sha256:87c25dfc5b3058891491b6fd0647c86fe9a71f96d8ba41a2693b42797fcada83
FROM ${BASE_IMAGE}
USER root
COPY --chown=999:987 owner/freqtrade/ /freqtrade/freqtrade/
COPY --chown=999:987 owner/binana-strategies/ /freqtrade/binana-strategies/
COPY --chown=999:987 tests/ /freqtrade/binana_tests/
COPY --chown=999:987 runtime-fixes/ /freqtrade/runtime-fixes/
COPY --chown=999:987 PROVENANCE.json validate_image.py /freqtrade/recovery_validation/
ENV PYTHONDONTWRITEBYTECODE=1 OPENBLAS_NUM_THREADS=1
USER binana
ENTRYPOINT ["python", "/freqtrade/recovery_validation/validate_image.py"]
CMD []
