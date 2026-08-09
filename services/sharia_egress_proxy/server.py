"""Minimal fail-closed HTTPS CONNECT proxy with pinned public destinations.

The Sharia screener is attached only to an internal Docker network and must
use this proxy for Internet access.  The proxy resolves the requested host
once, rejects the entire answer set if any address is non-global, and connects
to one of those numeric addresses directly.  TLS remains end-to-end between
``requests`` and the destination, so normal hostname/certificate verification
is preserved while DNS rebinding cannot redirect the actual connection.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import selectors
import socket
import socketserver
import threading


log = logging.getLogger("sharia-egress-proxy")
LISTEN_HOST = os.environ.get("SHARIA_PROXY_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("SHARIA_PROXY_LISTEN_PORT", "8080"))
CONNECT_TIMEOUT = float(os.environ.get("SHARIA_PROXY_CONNECT_TIMEOUT", "15"))
IDLE_TIMEOUT = float(os.environ.get("SHARIA_PROXY_IDLE_TIMEOUT", "30"))
MAX_HEADER_BYTES = 16 * 1024
MAX_CONNECTIONS = int(os.environ.get("SHARIA_PROXY_MAX_CONNECTIONS", "16"))
_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_slots = threading.BoundedSemaphore(MAX_CONNECTIONS)


class ProxyRefused(ValueError):
    """The CONNECT target is malformed or not globally routable."""


def canonical_target(authority: str) -> tuple[str, int]:
    """Return an IDNA hostname and port; only credential-free HTTPS is valid."""
    value = str(authority).strip()
    if (not value or "@" in value or "/" in value or "\\" in value or
            value.startswith("[") or value.count(":") != 1):
        raise ProxyRefused("CONNECT target must be hostname:443")
    host, raw_port = value.rsplit(":", 1)
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ProxyRefused("CONNECT port is invalid") from exc
    if port != 443:
        raise ProxyRefused("only HTTPS port 443 is allowed")
    host = host.strip().lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ProxyRefused("IP-literal CONNECT targets are not allowed")
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ProxyRefused("CONNECT hostname is not valid IDNA") from exc
    labels = ascii_host.split(".")
    if (len(labels) < 2 or any(not _HOST_LABEL.fullmatch(label)
                               for label in labels)):
        raise ProxyRefused("CONNECT hostname is malformed or overly broad")
    return ascii_host, port


def resolve_public(host: str, port: int) -> list[tuple[int, tuple]]:
    """Resolve once and reject the complete set if any answer is non-public."""
    try:
        infos = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise ProxyRefused(f"cannot resolve CONNECT hostname: {exc}") from exc
    candidates: list[tuple[int, tuple]] = []
    seen = set()
    for family, socktype, proto, _canon, sockaddr in infos:
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except (ValueError, IndexError, TypeError) as exc:
            raise ProxyRefused("resolver returned an unusable address") from exc
        if not address.is_global or address.is_multicast:
            raise ProxyRefused(
                f"CONNECT hostname resolved to non-public address {address}")
        key = (family, sockaddr)
        if key not in seen:
            seen.add(key)
            candidates.append((family, sockaddr))
    if not candidates:
        raise ProxyRefused("CONNECT hostname resolved to no usable address")
    return candidates


def connect_pinned(candidates: list[tuple[int, tuple]]) -> socket.socket:
    """Connect to a numeric resolver result without performing another lookup."""
    last_error: OSError | None = None
    for family, sockaddr in candidates:
        outbound = socket.socket(family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        outbound.settimeout(CONNECT_TIMEOUT)
        try:
            outbound.connect(sockaddr)
            outbound.settimeout(None)
            return outbound
        except OSError as exc:
            last_error = exc
            outbound.close()
    raise ProxyRefused(f"all public CONNECT addresses failed: {last_error}")


def _relay(left: socket.socket, right: socket.socket) -> None:
    selector = selectors.DefaultSelector()
    selector.register(left, selectors.EVENT_READ, right)
    selector.register(right, selectors.EVENT_READ, left)
    try:
        while True:
            ready = selector.select(IDLE_TIMEOUT)
            if not ready:
                return
            for key, _mask in ready:
                data = key.fileobj.recv(65536)
                if not data:
                    return
                key.data.sendall(data)
    finally:
        selector.close()


class ConnectHandler(socketserver.BaseRequestHandler):
    """Accept exactly one bounded CONNECT request and relay its TLS tunnel."""

    def _reply(self, status: str) -> None:
        self.request.sendall(
            f"HTTP/1.1 {status}\r\nConnection: close\r\n\r\n".encode("ascii"))

    def handle(self) -> None:
        if not _slots.acquire(blocking=False):
            self._reply("503 Busy")
            return
        outbound: socket.socket | None = None
        try:
            self.request.settimeout(CONNECT_TIMEOUT)
            header = bytearray()
            while b"\r\n\r\n" not in header:
                chunk = self.request.recv(4096)
                if not chunk:
                    return
                header.extend(chunk)
                if len(header) > MAX_HEADER_BYTES:
                    raise ProxyRefused("CONNECT headers exceed the limit")
            head, remainder = bytes(header).split(b"\r\n\r\n", 1)
            try:
                request_line = head.split(b"\r\n", 1)[0].decode("ascii")
                method, authority, version = request_line.split(" ")
            except (UnicodeDecodeError, ValueError) as exc:
                raise ProxyRefused("malformed CONNECT request line") from exc
            if method != "CONNECT" or version not in {"HTTP/1.0", "HTTP/1.1"}:
                raise ProxyRefused("only HTTP CONNECT is supported")
            host, port = canonical_target(authority)
            outbound = connect_pinned(resolve_public(host, port))
            self._reply("200 Connection Established")
            if remainder:
                outbound.sendall(remainder)
            self.request.settimeout(None)
            _relay(self.request, outbound)
        except (ProxyRefused, OSError) as exc:
            log.warning("CONNECT refused from %s: %s", self.client_address, exc)
            try:
                self._reply("403 Forbidden")
            except OSError:
                pass
        finally:
            if outbound is not None:
                outbound.close()
            _slots.release()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = MAX_CONNECTIONS


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    with Server((LISTEN_HOST, LISTEN_PORT), ConnectHandler) as server:
        log.info("Sharia egress proxy listening on %s:%s", LISTEN_HOST, LISTEN_PORT)
        server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
