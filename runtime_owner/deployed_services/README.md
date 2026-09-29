# Deployed AWS service source inventory

SOURCE_INDEX.json records every Python source file under /app/services and the active runtime_stable_panel.py / monitor_only.py overlays in the six named BINANA containers. All 494 file references were compared by SHA256 to repository bytes. Forty distinct files absent from the recovery candidate are preserved under overrides; all other references reuse existing source files.

This snapshot is archival and is not imported by root Compose. It deliberately preserves what is running, including old research code and existing limitations. It does not claim the v19.3 research candidate has been deployed. Credentials, databases, private environment values and generated account state are excluded. Effective Compose/configuration reconstruction and deployment acceptance remain outstanding.

The Freqtrade owner source is separately indexed in ../deployed_r17/SOURCE_INDEX.json.
