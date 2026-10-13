"""
Relay container traffic to a host-local SOCKS5 proxy.

Why this is needed
------------------
The proxy on the host (127.0.0.1:7897) listens on the **loopback interface only**, so a
Docker container cannot reach it: `host.docker.internal` resolves to the Docker VM's
address and the container's own gateway answers nothing on that port. The container was
therefore unable to use the proxy at all, whatever it was configured with.

This accepts connections on every interface and forwards the bytes straight to the
loopback proxy, unchanged. It is a **TCP relay, not a proxy**: it does not speak SOCKS5
itself, so the client must still do the SOCKS5 handshake - the bytes simply have a route
to the proxy that the container could not otherwise reach.

Deliberately dependency-free (stdlib only) so it can run anywhere without an install.

Usage:
    python host_proxy_relay.py                 # listen on 0.0.0.0:17900 -> 127.0.0.1:7897
    python host_proxy_relay.py --list-port 17900 --proxy-port 7897
"""

from __future__ import annotations

import argparse
import socket
import socketserver
import sys
import threading

PROXY_HOST = "127.0.0.1"


class Relay(socketserver.BaseRequestHandler):
    """One client connection, forwarded to the proxy."""

    def handle(self) -> None:
        proxy_host = self.server.proxy_host
        proxy_port = self.server.proxy_port
        try:
            upstream = socket.create_connection((proxy_host, proxy_port), timeout=10)
        except OSError as exc:
            # Worth printing: a refused connection here means the proxy is not running,
            # which is a different problem from the container being unable to reach it.
            print(f"  relay: proxy {proxy_host}:{proxy_port} refused ({exc})",
                  flush=True)
            return

        # Two threads, one per direction. Either side closing ends the pair, and the
        # shutdown() calls unblock the other thread's recv.
        def pump(src, dst):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        threading.Thread(target=pump, args=(self.request, upstream), daemon=True).start()
        pump(upstream, self.request)
        upstream.close()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    # Set from the command line before serving.
    proxy_host = PROXY_HOST
    proxy_port = 7897


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-port", type=int, default=17900)
    parser.add_argument("--proxy-port", type=int, default=7897)
    parser.add_argument("--proxy-host", default=PROXY_HOST)
    args = parser.parse_args()

    Server.proxy_port = args.proxy_port
    Server.proxy_host = args.proxy_host

    # A quick self-check: relaying to a port nothing listens on produces a wall of
    # refusals, which is confusing to debug from inside a container.
    try:
        with socket.create_connection((args.proxy_host, args.proxy_port), timeout=5):
            pass
    except OSError as exc:
        print(f"  WARNING: {args.proxy_host}:{args.proxy_port} is not accepting "
              f"connections ({exc}). The relay will start but every connection will "
              f"fail. Is the proxy running?", flush=True)

    try:
        server = Server(("0.0.0.0", args.list_port), Relay)
    except OSError as exc:
        print(f"  cannot listen on 0.0.0.0:{args.list_port}: {exc}")
        return 1

    print(f"  relaying 0.0.0.0:{args.list_port} -> "
          f"{args.proxy_host}:{args.proxy_port}", flush=True)
    print(f"  point the container at: socks5://<host-ip>:{args.list_port}", flush=True)
    print("  Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
