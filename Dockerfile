# Base pública exacta; el código de este repositorio sustituye las fuentes.
ARG BRIDGE_BASE=ghcr.io/marcomolinaleija/cliente-xmpp-bridge:v35@sha256:5cc36c7f2d1cfc08632f6d6e88cd38c009138915069fc0de282c3a43c60de86e
ARG GO_BUILDER=golang:1.26.4-trixie@sha256:68b7145ec43d1820b9a56704554b53d1520aa2a15cb5233e374188a31b2a1bce

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
# Test vectors may include recordings. Fetch the pinned public upstream only
# in this throwaway test stage; they never enter the repository or release image.
RUN curl -fsSL 'https://codeload.github.com/JotaDev66/WaCalls/tar.gz/edeb31f0427aba896639db503153b777a405eccf' -o /tmp/voice-upstream.tar.gz && \
    echo '09de36959a0f378569de2767b985714cc7a9d59c30e9f0ea64f149c386e4dd86  /tmp/voice-upstream.tar.gz' | sha256sum -c - && \
    mkdir -p /tmp/voice-upstream && tar -xzf /tmp/voice-upstream.tar.gz -C /tmp/voice-upstream --strip-components=1 && \
    cp -a /tmp/voice-upstream/internal/voip/media/mlow/testdata /src/slidge_whatsapp/voicecore/internal/voip/media/mlow/ && \
    rm -rf /tmp/voice-upstream /tmp/voice-upstream.tar.gz
RUN --network=none go test go.mau.fi/whatsmeow -run '^TestCANStickerPackThumbnailEncryption$' && \
    go test . ./voicecore/... && go test -race . ./voicecore/...

FROM baseline AS runtime
USER root
COPY src/slidge/ /venv/lib/python3.13/site-packages/slidge/
COPY src/slidge_whatsapp/ /venv/lib/python3.13/site-packages/slidge_whatsapp/
COPY src-overrides/whatsmeow/ /venv/lib/python3.13/site-packages/slidge_whatsapp/vendor/go.mau.fi/whatsmeow/
COPY --from=native-builder /src/slidge_whatsapp/generated/_whatsapp.cpython-313-x86_64-linux-gnu.so /venv/lib/python3.13/site-packages/slidge_whatsapp/generated/
COPY runtime/voice_bridge_entrypoint.py /opt/voice-tools/voice_bridge_entrypoint.py
USER slidge
ENV CAN_VOICE_ENABLE=0
ENTRYPOINT ["python", "-B", "/opt/voice-tools/voice_bridge_entrypoint.py"]

FROM runtime AS runtime-tests
USER root
COPY tests/runtime/ /opt/can-tests/
USER slidge
RUN --network=none python -B /opt/can-tests/smoke_bridge_number_change_runtime.py && \
    python -B /opt/can-tests/smoke_bridge_read_state_runtime.py && \
    python -B /opt/can-tests/smoke_bridge_message_info_runtime.py && \
    python -B /opt/can-tests/smoke_bridge_call_duration_v33_runtime.py && \
    python -B /opt/can-tests/smoke_bridge_avatar_safety_runtime.py && \
    python -B /opt/can-tests/smoke_bridge_audio_modes_runtime.py && \
    python -B /opt/can-tests/smoke_bridge_native_sticker_pack_runtime.py && \
    python -B /opt/can-tests/smoke_bridge_sticker_delivery_runtime.py && \
    python -B /opt/can-tests/smoke_bridge_sticker_accessibility_runtime.py && \
    python -B /opt/can-tests/smoke_bridge_stickers_runtime.py && \
    python -B /opt/can-tests/smoke_bridge_transcription_runtime.py && \
    python -B /opt/can-tests/smoke_bridge_no_auto_join_runtime.py && \
    python -B /opt/can-tests/smoke_bridge_linking_runtime.py && \
    python -B /opt/can-tests/test_bridge_linking.py && \
    python -B /opt/can-tests/smoke_voice_integration.py && \
    python -B /opt/can-tests/smoke_voice_activation.py && \
    python -B /opt/can-tests/smoke_bridge_muc_history_runtime.py && \
    python -B /opt/can-tests/smoke_bridge_recovery_runtime.py && \
    python -B /opt/can-tests/smoke_bridge_read_markers_runtime.py && \
    python -B /opt/can-tests/smoke_bridge_group_echo_runtime.py

# Isolated callback probe: inject synthetic events only into this test binary.
FROM native-builder AS inbound-probe-builder
COPY tests/inbound/bridge_inbound_probe.go /src/slidge_whatsapp/
WORKDIR /src/slidge_whatsapp
RUN python3 -c 'from pathlib import Path; p=Path("session.go"); s=p.read_text(); anchor="s.eventHandler = h"; assert s.count(anchor)==1; p.write_text(s.replace(anchor,anchor+"\n s.canInboundOfflineProbe()"))' && \
    cd generated && go build -buildmode=c-shared -tags "mupdf extlib can_inbound_probe" \
    -ldflags="-s -w -extldflags=-Wl,--version-script=/tmp/gopy_whatsapp_exports.map" \
    -o _whatsapp.cpython-313-x86_64-linux-gnu.so .

FROM runtime AS inbound-probe-tests
USER root
COPY --from=inbound-probe-builder /src/slidge_whatsapp/generated/_whatsapp.cpython-313-x86_64-linux-gnu.so /venv/lib/python3.13/site-packages/slidge_whatsapp/generated/
COPY tests/inbound/smoke_bridge_inbound_runtime.py /opt/can-tests/
USER slidge
RUN --network=none python -B /opt/can-tests/smoke_bridge_inbound_runtime.py

FROM runtime AS release
