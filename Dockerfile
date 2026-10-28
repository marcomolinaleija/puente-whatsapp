# Base pública exacta; el código de este repositorio sustituye las fuentes.
ARG BRIDGE_BASE=ghcr.io/marcomolinaleija/cliente-xmpp-bridge:v35@sha256:5cc36c7f2d1cfc08632f6d6e88cd38c009138915069fc0de282c3a43c60de86e
ARG GO_BUILDER=golang:1.25-trixie@sha256:2c4c60ef415fbfa5e90300722293bef36c5e63fae17570ce18f580af933dbd73

FROM ${BRIDGE_BASE} AS baseline

FROM ${GO_BUILDER} AS native-builder
RUN apt-get update && apt-get install -y --no-install-recommends libmupdf-dev && \
    rm -rf /var/lib/apt/lists/*
COPY --from=baseline /usr/local/include/python3.13 /usr/local/include/python3.13
COPY --from=baseline /usr/local/lib/libpython3.13.so.1.0 /usr/local/lib/libpython3.13.so.1.0
RUN ln -s libpython3.13.so.1.0 /usr/local/lib/libpython3.13.so
COPY src/slidge_whatsapp/ /src/slidge_whatsapp/
COPY build/gopy_whatsapp_exports.map /tmp/gopy_whatsapp_exports.map
WORKDIR /src/slidge_whatsapp
ENV GOFLAGS=-mod=mod GOMAXPROCS=2 GOMEMLIMIT=2GiB
RUN go mod vendor
COPY src-overrides/whatsmeow/ /src/slidge_whatsapp/vendor/go.mau.fi/whatsmeow/
WORKDIR /src/slidge_whatsapp/generated
ENV GOFLAGS=-mod=vendor
RUN go build -buildmode=c-shared -tags "mupdf extlib" \
    -ldflags="-s -w -extldflags=-Wl,--version-script=/tmp/gopy_whatsapp_exports.map" \
    -o _whatsapp.cpython-313-x86_64-linux-gnu.so .

FROM native-builder AS go-tests
COPY tests/go/ /src/slidge_whatsapp/
COPY tests/whatsmeow/ /src/slidge_whatsapp/vendor/go.mau.fi/whatsmeow/
WORKDIR /src/slidge_whatsapp
RUN go test go.mau.fi/whatsmeow -run '^TestCANStickerPackThumbnailEncryption$' && \
    go test . && go test -race .

FROM baseline AS runtime
USER root
COPY src/slidge/ /venv/lib/python3.13/site-packages/slidge/
COPY src/slidge_whatsapp/ /venv/lib/python3.13/site-packages/slidge_whatsapp/
COPY src-overrides/whatsmeow/ /venv/lib/python3.13/site-packages/slidge_whatsapp/vendor/go.mau.fi/whatsmeow/
COPY --from=native-builder /src/slidge_whatsapp/generated/_whatsapp.cpython-313-x86_64-linux-gnu.so /venv/lib/python3.13/site-packages/slidge_whatsapp/generated/
USER slidge

FROM runtime AS runtime-tests
USER root
COPY tests/runtime/ /opt/can-tests/
USER slidge
RUN python -B /opt/can-tests/smoke_bridge_number_change_runtime.py && \
    python -B /opt/can-tests/smoke_bridge_read_state_runtime.py && \
    python -B /opt/can-tests/smoke_bridge_message_info_runtime.py && \
    python -B /opt/can-tests/smoke_bridge_call_duration_v33_runtime.py && \
    python -B /opt/can-tests/smoke_bridge_avatar_safety_runtime.py && \
    python -B /opt/can-tests/smoke_bridge_audio_modes_runtime.py && \
    python -B /opt/can-tests/smoke_bridge_native_sticker_pack_runtime.py && \
    python -B /opt/can-tests/smoke_bridge_sticker_delivery_runtime.py && \
    python -B /opt/can-tests/smoke_bridge_sticker_accessibility_runtime.py && \
    python -B /opt/can-tests/smoke_bridge_stickers_runtime.py && \
    python -B /opt/can-tests/smoke_bridge_no_auto_join_runtime.py

FROM runtime AS release
