"""Exercise the patched SDK through the monitor's real MCP server factory."""
import asyncio
import math

from monitoring.mcp import monitor_mcp_server as bridge


def test_streamable_http_sessions_have_finite_retention_and_capacity():
    # GHSA-84m7-p3x7-pcfv: the SDK must bound sessions without application overrides.
    server = bridge.create_server()
    server.streamable_http_app()
    manager = server.session_manager
    assert manager.session_idle_timeout is not None
    assert math.isfinite(manager.session_idle_timeout)
    assert manager.session_idle_timeout > 0
    assert manager.max_sessions is not None
    assert manager.max_sessions > 0


def test_patched_sdk_registers_monitor_tools_and_dispatches_clamped_report(monkeypatch):
    server = bridge.create_server()
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == {
        'bot_health', 'bot_status', 'bot_performance', 'bot_recent_trades',
        'bot_errors', 'bot_crashes', 'bot_binance_latency', 'bot_system_resources',
        'bot_deployment_status', 'bot_sharia_status', 'bot_daily_report', 'bot_custom_report',
    }
    calls = []

    def read_only_get(path, **params):
        calls.append((path, params))
        return {'ok': True}

    monkeypatch.setattr(bridge, '_get', read_only_get)
    asyncio.run(server.call_tool('bot_custom_report', {'days': 1000}))
    assert calls == [('/report', {'days': 90})]
