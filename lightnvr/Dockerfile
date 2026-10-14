# Reuse the released all-in-one NVR/detector runtime with baked AI models.
ARG ADDON_BASE_IMAGE=caubequay00/lightnvr-hass-addon@sha256:88e0219a9b79b762031bffbbbffaaf8a797c3b3dfb718fd0d94440296e64cbaf
FROM ${ADDON_BASE_IMAGE}

ARG BUILD_VERSION=0.3.0
ARG BUILD_ARCH=amd64
LABEL io.hass.version="${BUILD_VERSION}" \
      io.hass.type="app" \
      io.hass.arch="${BUILD_ARCH}" \
      org.opencontainers.image.title="LightNVR Home Assistant Add-on" \
      org.opencontainers.image.source="https://github.com/Vinhuit/lightnvr-hass-addons"

# Keep YouTube dependencies separate from the detector's Python environment.
COPY ytstream/requirements.ytstream.txt /opt/ytstream/requirements.ytstream.txt
RUN /usr/local/bin/python3.11 -m venv /opt/ytstream-venv \
    && /opt/ytstream-venv/bin/pip install --no-cache-dir -r /opt/ytstream/requirements.ytstream.txt
COPY ytstream/ /opt/ytstream/

ENV YTSTREAM_CONFIG_DIR=/data/ytstream LIGHTNVR_RTSP_URL=rtsp://127.0.0.1:8554 \
    PORT=8081 YTSTREAM_BIND_HOST=127.0.0.1

COPY configure.py /usr/local/lib/lightnvr-addon/configure.py
COPY ingress.js /usr/local/lib/lightnvr-addon/ingress.js
COPY run.sh /usr/local/bin/lightnvr-addon-run
COPY nginx.conf /etc/nginx/lightnvr-addon.conf
RUN chmod 755 /usr/local/bin/lightnvr-addon-run

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD curl --silent --output /dev/null --fail http://127.0.0.1:8080/login.html && curl --silent --output /dev/null --fail http://127.0.0.1:8000/health && curl --silent --output /dev/null --fail http://127.0.0.1:8081/health || exit 1
ENTRYPOINT ["/usr/local/bin/lightnvr-addon-run"]
CMD []
