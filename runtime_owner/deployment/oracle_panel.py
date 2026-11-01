"""Portable panel launcher: reject unavailable research before it is queued.

The underlying owner authorization, signed requests, Sharia decisions, and
registry approval implementation are unchanged.
"""
from __future__ import annotations

import os
import time
import math


class ResearchUnavailable(RuntimeError):
    pass


def research_ready(health, *, enabled, now=None):
    if enabled is not True or not isinstance(health, dict):
        return False
    try:
        age = (time.time() if now is None else now) - float(health["ts"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    return (0 <= age < 180 and health.get("ok") is True
            and health.get("ready_for_screening") is True)


def guarded_action(action, ready, notify):
    def invoke(*args, **kwargs):
        if not ready():
            message = ("Sharia research is unavailable or not ready on this host. "
                       "No request or decision was queued. The operational halal "
                       "registry is unchanged. Enable and verify the research profile first.")
            notify(message)
            raise ResearchUnavailable("SHARIA_RESEARCH_UNAVAILABLE_NOT_QUEUED")
        return action(*args, **kwargs)
    return invoke


def install_panel_transport(panel):
    """Keep all panel POSTs on the broker's shared flood-control transport."""
    base = panel.base

    def request(method, endpoint, *, data=None, timeout=15, attempts=4):
        if (str(method).upper() != 'POST' or not isinstance(endpoint, str)
                or not endpoint.startswith('/') or not 0 < float(timeout) <= 30):
            raise ValueError('BOUNDED_TELEGRAM_POST_REQUIRED')
        limit = min(4, max(1, int(attempts)))
        for attempt in range(limit):
            base._telegram_rate_guard()
            if endpoint in {'/sendMessage', '/editMessageText'} and isinstance(data, dict) and data.get('chat_id'):
                delay = float(panel._notification_outbox().reserve_chat(data['chat_id']))
                if not math.isfinite(delay) or not 0 <= delay <= 30:
                    raise RuntimeError('TELEGRAM_CHAT_WAIT_EXCEEDS_BUDGET')
                time.sleep(delay)
            try:
                response = base._telegram_post(endpoint, data=data, timeout=timeout)
                if response.status_code in {500, 502, 503, 504}:
                    raise RuntimeError('Telegram transient server failure')
                response.raise_for_status()
                if response.json().get('ok') is not True:
                    raise RuntimeError('Telegram acknowledgement unavailable')
                return response
            except Exception:
                # A 429 updates the shared deadline in _telegram_post. Return
                # to the durable worker immediately rather than sleeping an
                # untrusted retry_after or bypassing the guard on another thread.
                base._telegram_rate_guard()
                if attempt + 1 == limit:
                    raise
                time.sleep(min(4.0, .5 * (2 ** attempt)))

    panel._tg_request = request


def main():
    import runtime_stable_panel as panel
    base = panel.base
    install_panel_transport(panel)

    def ready():
        enabled = os.getenv("BINANA_RESEARCH_ENABLED", "false").lower() == "true"
        health = base.read_json(base.SHARIA_RESEARCH_RUNTIME_DIR / "health.json", {}) or {}
        return research_ready(health, enabled=enabled)

    def notify(message):
        base.send(message, base.OWNER)

    for name in ("sharia_scan_request", "sharia_bounded_scan_requests", "sharia_owner_decision"):
        setattr(base, name, guarded_action(getattr(base, name), ready, notify))
    panel.main()


if __name__ == "__main__":
    main()
