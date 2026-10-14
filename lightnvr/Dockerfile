# The enriched LightNVR image currently publishes amd64 only. The digest
# fixes both the binary and the frontend, including the mobile layout fixes.
ARG LIGHTNVR_IMAGE=caubequay00/lightnvr-enrich@sha256:4687029afd0bc64b5dc433c669779273a7d52d41c6893522f30d93ff5137108d
FROM ${LIGHTNVR_IMAGE}

ARG BUILD_VERSION=0.1.4
ARG BUILD_ARCH=amd64
LABEL io.hass.version="${BUILD_VERSION}" \
      io.hass.type="app" \
      io.hass.arch="${BUILD_ARCH}" \
      org.opencontainers.image.title="LightNVR Home Assistant Add-on" \
      org.opencontainers.image.source="https://github.com/Vinhuit/lightnvr-hass-addons"

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx python3 tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY configure.py /usr/local/lib/lightnvr-addon/configure.py
COPY ingress.js /usr/local/lib/lightnvr-addon/ingress.js
COPY run.sh /usr/local/bin/lightnvr-addon-run
COPY nginx.conf /etc/nginx/lightnvr-addon.conf
RUN chmod 755 /usr/local/bin/lightnvr-addon-run

EXPOSE 8099
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl --silent --output /dev/null --fail http://127.0.0.1:8080/login.html || exit 1
ENTRYPOINT ["/usr/local/bin/lightnvr-addon-run"]
CMD []
