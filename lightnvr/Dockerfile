# The enriched LightNVR image currently publishes amd64 only. The digest
# fixes both the binary and the frontend, including the mobile layout fixes.
ARG LIGHTNVR_IMAGE=caubequay00/lightnvr-enrich@sha256:4687029afd0bc64b5dc433c669779273a7d52d41c6893522f30d93ff5137108d
ARG DETECTOR_IMAGE=caubequay00/light-object-detect@sha256:46faeb17dd7d1c37dc07b27e6810e05301407176f2fbcf193ebc647ba5f0eeb3
FROM ${DETECTOR_IMAGE} AS detector
FROM ${LIGHTNVR_IMAGE}

ARG BUILD_VERSION=0.2.0
ARG BUILD_ARCH=amd64
LABEL io.hass.version="${BUILD_VERSION}" \
      io.hass.type="app" \
      io.hass.arch="${BUILD_ARCH}" \
      org.opencontainers.image.title="LightNVR Home Assistant Add-on" \
      org.opencontainers.image.source="https://github.com/Vinhuit/lightnvr-hass-addons"

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx python3 tzdata \
        libglib2.0-0t64 libgl1 libgomp1 libexpat1 libffi8 libbz2-1.0 liblzma5 libcrypt1 \
    && rm -rf /var/lib/apt/lists/*

# Reuse the detector's tested Python 3.11 environment, without copying its
# writable data or environment files into the add-on.
COPY --from=detector /usr/local/bin/python3.11 /usr/local/bin/python3.11
COPY --from=detector /usr/local/lib/libpython3.11.so.1.0 /usr/local/lib/libpython3.11.so.1.0
COPY --from=detector /usr/local/lib/python3.11 /usr/local/lib/python3.11
COPY --from=detector /opt/venv /opt/venv
COPY --from=detector /app/api /opt/light-object-detect/api
COPY --from=detector /app/backends /opt/light-object-detect/backends
COPY --from=detector /app/models /opt/light-object-detect/models
COPY --from=detector /app/utils /opt/light-object-detect/utils
COPY --from=detector /app/main.py /app/config.py /opt/light-object-detect/
RUN ln -sf python3.11 /usr/local/bin/python3 \
    && ln -sf python3 /usr/local/bin/python \
    && ldconfig

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    DEFAULT_BACKEND=onnx AVAILABLE_BACKENDS='["onnx"]' \
    ONNX_MODEL_PATH=/opt/light-object-detect/backends/onnx/models/yolo26n.onnx \
    ONNX_MODEL_TYPE=yolo26 ONNX_CONFIDENCE_THRESHOLD=0.25 \
    ONNX_INTRA_OP_THREADS=2 ONNX_INTER_OP_THREADS=1 \
    OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 \
    FACE_DET_SIZE=320 FACE_DB_PATH=/data/detector/faces.db CLASSIFICATION_ENABLED=true

# Bake face weights at build time, so inference does not download models at
# the first camera event and does not depend on an external AI service.
RUN /opt/venv/bin/python -c "from insightface.app import FaceAnalysis; app = FaceAnalysis(name='buffalo_l', providers=['CPUExecutionProvider']); app.prepare(ctx_id=-1, det_size=(320,320))"

COPY configure.py /usr/local/lib/lightnvr-addon/configure.py
COPY ingress.js /usr/local/lib/lightnvr-addon/ingress.js
COPY run.sh /usr/local/bin/lightnvr-addon-run
COPY nginx.conf /etc/nginx/lightnvr-addon.conf
RUN chmod 755 /usr/local/bin/lightnvr-addon-run

EXPOSE 8099
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl --silent --output /dev/null --fail http://127.0.0.1:8080/login.html && curl --silent --output /dev/null --fail http://127.0.0.1:8000/health || exit 1
ENTRYPOINT ["/usr/local/bin/lightnvr-addon-run"]
CMD []
