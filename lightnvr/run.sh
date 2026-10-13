#!/bin/bash
set -euo pipefail

lightnvr_pid=""
nginx_pid=""
# shellcheck disable=SC2329 # Invoked indirectly by the EXIT trap below.
cleanup() {
    trap - EXIT TERM INT
    for child in "$lightnvr_pid" "$nginx_pid"; do
        if [ -n "$child" ]; then
            kill -TERM "$child" 2>/dev/null || true
        fi
    done
    for child in "$lightnvr_pid" "$nginx_pid"; do
        if [ -n "$child" ]; then
            wait "$child" 2>/dev/null || true
        fi
    done
}
trap cleanup EXIT
trap 'exit 0' TERM INT

python3 /usr/local/lib/lightnvr-addon/configure.py
nginx -t -c /etc/nginx/lightnvr-addon.conf
/bin/lightnvr --generate-go2rtc-config -c /etc/lightnvr/lightnvr.ini

echo "Starting LightNVR and Home Assistant Ingress"
/bin/lightnvr -c /etc/lightnvr/lightnvr.ini &
lightnvr_pid=$!
nginx -c /etc/nginx/lightnvr-addon.conf -g 'daemon off;' &
nginx_pid=$!

# Exit if either service stops; Supervisor can then restart the entire add-on.
set +e
wait -n "$lightnvr_pid" "$nginx_pid"
result=$?
exit "$result"
