"""The startup probe must match the listener's reusable TCP bind semantics."""
from contextlib import ExitStack
import errno
from pathlib import Path
import socket
import sys
from unittest import mock

import pytest

from monitoring import control


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux TCP TIME_WAIT regression')
def test_clean_server_close_leaves_time_wait_but_does_not_block_restart(monkeypatch):
    with ExitStack() as cleanup:
        listener = cleanup.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.settimeout(2)
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        listener.listen(1)
        client = cleanup.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        client.settimeout(2)
        client.connect(('127.0.0.1', port))
        accepted, _ = listener.accept()
        cleanup.enter_context(accepted)
        accepted.settimeout(2)
        listener.close()
        # The server actively closes first. EOF synchronizes both FINs without
        # sleeps, leaving only the server port's kernel TIME_WAIT record.
        accepted.shutdown(socket.SHUT_WR)
        assert client.recv(1) == b''
        client.shutdown(socket.SHUT_WR)
        assert accepted.recv(1) == b''
        accepted.close()
        client.close()
        states = [row.split()[3] for row in Path('/proc/net/tcp').read_text().splitlines()[1:]
                  if int(row.split()[1].split(':')[1], 16) == port]
        assert '06' in states, 'fixture did not create server-side TIME_WAIT'
        assert '0A' not in states, 'fixture unexpectedly retained a listener'
        monkeypatch.setattr(control.CONFIG, 'bind_host', '127.0.0.1')
        monkeypatch.setattr(control.CONFIG, 'port', port)
        available, reason = control.monitor_port_available()
        assert available, reason


def _loopback_listener(family):
    listener = socket.socket(family, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        host = '::1' if family == socket.AF_INET6 else '127.0.0.1'
        listener.bind((host, 0))
        listener.listen(1)
        return listener, host
    except OSError as exc:
        listener.close()
        if family == socket.AF_INET6 and exc.errno in {errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL}:
            pytest.skip('IPv6 loopback is unavailable on this kernel')
        raise


@pytest.mark.parametrize('family', [socket.AF_INET, socket.AF_INET6])
def test_active_loopback_listener_is_still_rejected(monkeypatch, family):
    listener, host = _loopback_listener(family)
    with listener:
        monkeypatch.setattr(control.CONFIG, 'bind_host', host)
        monkeypatch.setattr(control.CONFIG, 'port', listener.getsockname()[1])
        available, reason = control.monitor_port_available()
        assert available is False
        assert 'occupied' in reason


@pytest.mark.parametrize('family', [socket.AF_INET, socket.AF_INET6])
def test_unused_loopback_port_is_available(monkeypatch, family):
    listener, host = _loopback_listener(family)
    port = listener.getsockname()[1]
    listener.close()
    monkeypatch.setattr(control.CONFIG, 'bind_host', host)
    monkeypatch.setattr(control.CONFIG, 'port', port)
    assert control.monitor_port_available()[0] is True


@pytest.mark.parametrize('stage', ['socket', 'setsockopt', 'bind'])
def test_unknown_socket_errors_fail_closed_and_close_any_probe(monkeypatch, stage):
    probe = mock.Mock()
    failure = OSError(errno.EACCES, 'injected socket permission failure')
    factory = mock.Mock(return_value=probe)
    if stage == 'socket':
        factory.side_effect = failure
    else:
        getattr(probe, stage).side_effect = failure
    monkeypatch.setattr(control.socket, 'socket', factory)
    assert control.monitor_port_available()[0] is False
    if stage == 'socket':
        probe.close.assert_not_called()
    else:
        probe.close.assert_called_once_with()
