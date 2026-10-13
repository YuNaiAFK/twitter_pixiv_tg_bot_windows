#!/bin/sh
#
# Start the Bot API server with its Telegram traffic sent through a SOCKS5 proxy.
#
# The problem this solves
# -----------------------
# TDLib connects to Telegram's data centres over MTProto and **ignores HTTP_PROXY and
# friends**, and the server's --proxy flag is webhook-only. On a network that filters
# Telegram, the server therefore starts, looks fine, and hangs on login with an empty
# log - which is easily mistaken for a wrong api_id.
#
# How it is solved
# ----------------
# redsocks listens locally and relays whatever reaches it through the SOCKS5 proxy, and
# NAT rules send every packet destined for Telegram's address ranges to redsocks. TDLib
# is never told anything, so there is nothing for it to ignore.
#
# The ranges are redirected whole rather than by address list: TDLib fetches the current
# data-centre addresses at runtime and keeps discovering new ones, so a list would work
# until it silently did not.

set -e

PROXY="${TELEGRAM_MT_PROXY:-}"

# Telegram's own network, from https://core.telegram.org/getProxyConfig and TDLib's
# default data-centre list. The /24s and wider blocks cover every address TDLib reaches.
TG_V4="149.154.160.0/20 149.154.164.0/22 149.154.167.0/24 149.154.171.0/24 \
149.154.175.0/24 91.108.4.0/22 91.108.8.0/22 91.108.12.0/22 91.108.16.0/22 \
91.108.20.0/22 91.108.56.0/22 91.105.192.0/23 185.76.151.0/24"

if [ -n "$PROXY" ]; then
    # Accept host:port with or without a scheme. `${PROXY##*:}` splits on the LAST colon,
    # which is what makes this safe for a bare "host:port" as well as for
    # "socks5://host:port" and "socks5h://host:port".
    ADDR="${PROXY#socks5h://}"
    ADDR="${ADDR#socks5://}"
    ADDR="${ADDR#http://}"
    P_HOST="${ADDR%:*}"
    P_PORT="${ADDR##*:}"

    if [ -z "$P_HOST" ] || [ -z "$P_PORT" ] || [ "$P_HOST" = "$ADDR" ]; then
        echo "[proxy] TELEGRAM_MT_PROXY='$PROXY' is not host:port; refusing to start"
        exit 1
    fi

    echo "[proxy] relaying Telegram traffic through socks5://$P_HOST:$P_PORT"

    cat > /tmp/redsocks.conf <<EOF
base {
    log_debug = off;
    log_info = on;
    daemon = on;
    user = root;
    group = root;
    redirector = iptables;
}
redsocks {
    local_ip = 127.0.0.1;
    local_port = 12345;
    ip = $P_HOST;
    port = $P_PORT;
    type = socks5;
}
EOF

    redsocks -c /tmp/redsocks.conf
    # Give it a moment to bind before the rules start pointing at it.
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        [ -f /proc/net/tcp ] && grep -qi ":3039 " /proc/net/tcp && break
        sleep 0.5
    done

    for net in $TG_V4; do
        iptables -t nat -A OUTPUT -p tcp -d "$net" -j REDIRECT --to-port 12345
    done
    echo "[proxy] redirect rules installed:"
    iptables -t nat -S OUTPUT | grep REDIRECT | head -14
else
    echo "[proxy] TELEGRAM_MT_PROXY is empty: connecting to Telegram directly"
fi

# Hand over to the image's own entrypoint so the environment-variable contract
# (TELEGRAM_LOCAL -> --local, and everything else) behaves exactly as documented.
exec /docker-entrypoint.sh "$@"
