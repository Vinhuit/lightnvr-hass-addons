#!/bin/bash
set -euo pipefail

lightnvr_pid=""
nginx_pid=""
detector_pid=""
ytstream_pid=""
# shellcheck disable=SC2329 # Invoked indirectly by the EXIT trap below.
cleanup() {
    trap - EXIT TERM INT
    if [ -n "$ytstream_pid" ]; then
        kill -TERM -- "-$ytstream_pid" 2>/dev/null || true
    fi
    for child in "$lightnvr_pid" "$nginx_pid" "$detector_pid" "$ytstream_pid"; do
        if [ -n "$child" ]; then
            kill -TERM "$child" 2>/dev/null || true
        fi
    done
    for child in "$lightnvr_pid" "$nginx_pid" "$detector_pid" "$ytstream_pid"; do
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

echo "Starting internal object detection, face recognition and classification"
cd /opt/light-object-detect
/opt/venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000 &
detector_pid=$!
ready=false
for _ in {1..90}; do
    if curl --silent --output /dev/null --fail http://127.0.0.1:8000/health; then
        ready=true
        break
    fi
    if ! kill -0 "$detector_pid" 2>/dev/null; then
        echo "Internal detection service exited during startup" >&2
        exit 1
    fi
    sleep 1
done
if [ "$ready" != true ]; then
    echo "Internal detection service did not become ready" >&2
    exit 1
fi

echo "Starting LightNVR and Home Assistant Ingress"
export FLASK_SECRET_KEY
FLASK_SECRET_KEY=$(cat /data/ytstream/flask_secret)
cd /data/ytstream
setsid /opt/ytstream-venv/bin/python /opt/ytstream/ytstream.py --all-streams-from-json-- &
ytstream_pid=$!
ready=false
for _ in {1..60}; do
    if curl --silent --output /dev/null --fail http://127.0.0.1:8081/health; then
        ready=true
        break
    fi
    if ! kill -0 "$ytstream_pid" 2>/dev/null; then
        echo "Internal YTStream exited during startup" >&2
        exit 1
    fi
    sleep 1
done
if [ "$ready" != true ]; then
    echo "Internal YTStream did not become ready" >&2
    exit 1
fi

/bin/lightnvr -c /etc/lightnvr/lightnvr.ini &
lightnvr_pid=$!
nginx -c /etc/nginx/lightnvr-addon.conf -g 'daemon off;' &
nginx_pid=$!

# Exit if any service stops; Supervisor can then restart the entire add-on.
set +e
wait -n "$lightnvr_pid" "$nginx_pid" "$detector_pid" "$ytstream_pid"
result=$?
exit "$result"
