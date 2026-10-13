"""
A minimal synchronous SOCKS5 dialer for httpx, with **remote DNS**.

Why this exists
---------------
httpx supports ``socks5://`` proxies only when the optional ``socksio`` package is
installed, and it is not installed here. Rather than add a dependency, this provides the
part that matters: the CONNECT negotiation.

**Remote DNS is the whole point.** The hostname is sent to the proxy unresolved
(ATYP=3), so the connection targets a *name* rather than an address this machine already
resolved. e-hentai throttles and filters by IP here, so this is the difference between
using the proxy and merely routing through it.

Only CONNECT is implemented - no BIND, no UDP - which is all an HTTP client needs.

Scope: e-hentai only. The Bot API server's own Telegram traffic is handled separately by
the NAT redirect inside its container, not by this.
"""

from __future__ import annotations

import socket
import struct


class Socks5Error(OSError):
    """A SOCKS5 negotiation failure, raised like the connection error it is."""


def _recv_exactly(sock: socket.socket, count: int) -> bytes:
    """Read exactly ``count`` bytes, or raise: a short read must not be silent."""
    chunks = []
    remaining = count
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise Socks5Error("SOCKS5 proxy closed the connection mid-handshake")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def socks5_connect(proxy_host: str, proxy_port: int, target_host: str,
                   target_port: int, timeout: float | None = 30.0) -> socket.socket:
    """
    Open a TCP connection to ``target_host:target_port`` through a SOCKS5 proxy.

    ``target_host`` is sent **as a name**, so the proxy resolves it. Sending an address
    instead would defeat the purpose on a network that filters by IP, which is exactly
    the case this is for.

    The returned socket has no timeout left on it: httpcore manages its own deadlines,
    and a leftover timeout would abort a slow but healthy archive download.
    """
    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    try:
        sock.settimeout(timeout)

        # Greeting: SOCKS5, one offered method, "no authentication".
        sock.sendall(b"\x05\x01\x00")
        version, method = _recv_exactly(sock, 2)
        if version != 5:
            raise Socks5Error(f"not a SOCKS5 proxy (version {version})")
        if method != 0:
            raise Socks5Error(f"SOCKS5 proxy demands authentication (method {method})")

        try:
            host_bytes = target_host.encode("idna")
        except UnicodeError:
            host_bytes = target_host.encode("utf-8")
        if len(host_bytes) > 255:
            raise Socks5Error(f"hostname too long for SOCKS5: {target_host!r}")
        sock.sendall(b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes
                     + struct.pack(">H", target_port))

        version, reply, _reserved, atyp = _recv_exactly(sock, 4)
        if reply != 0:
            reasons = {
                1: "general failure", 2: "not allowed by ruleset",
                3: "network unreachable", 4: "host unreachable",
                5: "connection refused", 6: "TTL expired",
                7: "command not supported", 8: "address type not supported",
            }
            raise Socks5Error(
                f"SOCKS5 connect to {target_host}:{target_port} failed: "
                f"{reasons.get(reply, f'code {reply}')}")

        # The bound address must be consumed before the stream is usable.
        if atyp == 1:
            _recv_exactly(sock, 4 + 2)
        elif atyp == 4:
            _recv_exactly(sock, 16 + 2)
        elif atyp == 3:
            _recv_exactly(sock, _recv_exactly(sock, 1)[0] + 2)
        else:
            raise Socks5Error(f"unexpected address type in reply: {atyp}")

        sock.settimeout(None)
        return sock
    except BaseException:
        sock.close()
        raise


def _as_network_stream(sock: socket.socket):
    """
    Wrap a raw socket in httpcore's stream type.

    httpcore does not accept a bare socket: it calls ``read``/``write``/``close`` and,
    for https, ``start_tls``. Returning the socket directly failed with
    ``'socket' object has no attribute 'start_tls'``.
    """
    from httpcore._backends.sync import SyncStream

    return SyncStream(sock)


def build_socks5_http_client(proxy_url: str, *, timeout: float, headers=None,
                             follow_redirects: bool = True):
    """
    An ``httpx.Client`` that dials every connection through a SOCKS5 proxy.

    ``proxy_url`` is ``socks5://host:port`` (a ``socks5h`` scheme is accepted and means
    the same here, because remote DNS is always used).

    Implementation note: httpx builds its connection pool internally and exposes no hook
    for this, so the pool's ``connect_tcp`` is replaced on the network backend. That is
    the single point where a socket is created, which makes it the correct seam - a
    transport wrapper would have to reproduce httpcore's connection handling instead.
    """
    import httpx

    if not proxy_url:
        raise ValueError("proxy_url is empty")

    body = proxy_url.split("://", 1)[-1].rstrip("/")
    proxy_host, separator, port_text = body.rpartition(":")
    if not separator or not proxy_host:
        raise ValueError(f"SOCKS5 proxy needs host:port, got {proxy_url!r}")
    try:
        proxy_port = int(port_text)
    except ValueError as exc:
        raise ValueError(f"bad SOCKS5 proxy port: {port_text!r}") from exc

    transport = httpx.HTTPTransport()
    backend = transport._pool._network_backend

    def proxied_connect_tcp(host, port, timeout=None, local_address=None,
                            socket_options=None):
        # httpcore calls this with the keywords host/port/timeout/local_address/
        # socket_options - read from its own _sync/connection.py, not guessed. An earlier
        # version used target_host/target_port and every request raised TypeError.
        #
        # local_address and socket_options belong to the socket reaching the *proxy*, not
        # the target, and httpcore's defaults are equivalent to leaving them unset.
        return _as_network_stream(
            socks5_connect(proxy_host, proxy_port, host, port,
                           timeout=timeout if timeout else 30.0))

    backend.connect_tcp = proxied_connect_tcp

    backend.connect_tcp = proxied_connect_tcp

    return httpx.Client(transport=transport, timeout=timeout, headers=headers,
                        follow_redirects=follow_redirects)
