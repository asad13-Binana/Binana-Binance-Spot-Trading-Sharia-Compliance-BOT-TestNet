# Exact deployed AWS r17 owner source

SOURCE_INDEX.json maps every active BINANA owner module, the six modified Freqtrade integration files, the wrapper and pinned upstream NFI strategy to exact SHA256-verified repository bytes. Fourteen files differ from the undeployed recovery candidate; those bytes are preserved under overrides. Ten unchanged files reuse the existing candidate paths to avoid duplication.

This snapshot records the running image sha256:87c25dfc5b3058891491b6fd0647c86fe9a71f96d8ba41a2693b42797fcada83. It does not activate that code, bundle credentials or include account databases. The running r17 code has known recovery limitations; use the newer owner candidate and complete deployment/acceptance work before declaring those limitations fixed. Root Compose remains the legacy deployment layout.

The two historical repair scripts under scripts/recovery were applied separately; they modify narrowly checked metadata and one identity binding, not these image source files.
