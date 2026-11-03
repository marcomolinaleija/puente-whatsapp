# La base aporta Python y recursos; todas las fuentes activas salen de este repo.
ARG BRIDGE_BASE=ghcr.io/marcomolinaleija/cliente-xmpp-bridge:v35@sha256:5cc36c7f2d1cfc08632f6d6e88cd38c009138915069fc0de282c3a43c60de86e
ARG GO_BUILDER=golang:1.26.4-trixie@sha256:68b7145ec43d1820b9a56704554b53d1520aa2a15cb5233e374188a31b2a1bce
FROM ${BRIDGE_BASE} AS baseline

FROM ${GO_BUILDER} AS native-builder
RUN apt-get update && apt-get install -y --no-install-recommends libmupdf-dev && rm -rf /var/lib/apt/lists/*
COPY --from=baseline /usr/local/include/python3.13 /usr/local/include/python3.13
COPY --from=baseline /usr/local/lib/libpython3.13.so.1.0 /usr/local/lib/libpython3.13.so.1.0
RUN ln -s libpython3.13.so.1.0 /usr/local/lib/libpython3.13.so
COPY src/slidge_whatsapp/ /src/slidge_whatsapp/
COPY build/gopy_whatsapp_exports.map build/check_go_report.py /opt/can-build/
WORKDIR /src/slidge_whatsapp
ENV GOTOOLCHAIN=local GOMAXPROCS=2 GOMEMLIMIT=2GiB GOFLAGS=-mod=mod
RUN go mod vendor
COPY src-overrides/whatsmeow/ /src/slidge_whatsapp/vendor/go.mau.fi/whatsmeow/
COPY build/patch_whatsmeow_status.py /opt/can-build/
RUN python3 -S -B /opt/can-build/patch_whatsmeow_status.py /src/slidge_whatsapp/vendor/go.mau.fi/whatsmeow
ENV GOFLAGS=-mod=vendor
RUN --network=none cd generated && go build -p 2 -buildmode=c-shared -tags "mupdf extlib" \
    -ldflags="-s -w -extldflags=-Wl,--version-script=/opt/can-build/gopy_whatsapp_exports.map" \
    -o _whatsapp.cpython-313-x86_64-linux-gnu.so .

FROM native-builder AS go-tests
COPY tests/go/ /src/slidge_whatsapp/
COPY tests/whatsmeow/ /src/slidge_whatsapp/vendor/go.mau.fi/whatsmeow/
COPY build/fetch_codec_testdata.py build/voice-upstream.json build/voice-fixtures.sha256 /opt/can-build/
RUN python3 -S -B /opt/can-build/fetch_codec_testdata.py
RUN --network=none (go test -p 2 -json go.mau.fi/whatsmeow -run '^TestCAN(StickerPackThumbnailEncryption|StatusNodesAcknowledged)$' > /tmp/sticker-go.jsonl; echo $? > /tmp/sticker-go.status) && \
    python3 /opt/can-build/check_go_report.py /tmp/sticker-go.jsonl "$(cat /tmp/sticker-go.status)" && \
    (go test -p 2 -json -race go.mau.fi/whatsmeow -run '^TestCANStatusNodesAcknowledged$' > /tmp/status-race.jsonl; echo $? > /tmp/status-race.status) && \
    python3 /opt/can-build/check_go_report.py /tmp/status-race.jsonl "$(cat /tmp/status-race.status)" && \
    (CAN_CALL_FIXTURE=/tmp/call-history-fixture.json go test -p 2 -json . ./voicecore/... > /tmp/go.jsonl; echo $? > /tmp/go.status) && \
    python3 /opt/can-build/check_go_report.py /tmp/go.jsonl "$(cat /tmp/go.status)" && \
    (go test -p 2 -json -race . ./voicecore/... > /tmp/race.jsonl; echo $? > /tmp/race.status) && \
    python3 /opt/can-build/check_go_report.py /tmp/race.jsonl "$(cat /tmp/race.status)"

FROM baseline AS runtime
USER root
COPY src/slidge/ /venv/lib/python3.13/site-packages/slidge/
COPY src/slidge_whatsapp/ /venv/lib/python3.13/site-packages/slidge_whatsapp/
COPY src/sitecustomize.py /venv/lib/python3.13/site-packages/sitecustomize.py
COPY src-overrides/whatsmeow/ /venv/lib/python3.13/site-packages/slidge_whatsapp/vendor/go.mau.fi/whatsmeow/
COPY --from=native-builder /src/slidge_whatsapp/generated/_whatsapp.cpython-313-x86_64-linux-gnu.so /venv/lib/python3.13/site-packages/slidge_whatsapp/generated/
COPY runtime/ /opt/can-bridge/
ENV CAN_VOICE_ENABLE=0
USER slidge
ENTRYPOINT ["python", "-B", "/opt/can-bridge/voice_bridge_entrypoint.py"]
HEALTHCHECK --interval=30s --timeout=10s --start-period=120s --retries=3 CMD ["python", "-B", "/opt/can-bridge/bridge_health.py"]

FROM runtime AS runtime-tests
USER root
COPY tests/runtime/ /opt/can-tests/
COPY build/run_runtime_checks.py /opt/can-tests/
COPY deploy/slidge-watchdog /opt/can-tests/slidge-watchdog
COPY --chown=slidge:slidge --from=go-tests /tmp/go.jsonl /tmp/race.jsonl /tmp/sticker-go.jsonl /tmp/status-race.jsonl /tmp/call-history-fixture.json /opt/can-tests/
USER slidge
RUN --network=none python -B /opt/can-tests/run_runtime_checks.py

# Variante exclusiva de prueba: nunca se distribuye el callback sintético.
FROM go-tests AS inbound-probe-builder
COPY tests/inbound/bridge_inbound_probe.go /src/slidge_whatsapp/
RUN --network=none python3 -c 'from pathlib import Path; p=Path("session.go"); s=p.read_text(); anchor="s.eventHandler = h"; assert s.count(anchor)==1; p.write_text(s.replace(anchor,anchor+"\n s.canInboundOfflineProbe()"))' && \
    cd generated && go build -p 2 -buildmode=c-shared -tags "mupdf extlib can_inbound_probe" \
    -ldflags="-s -w -extldflags=-Wl,--version-script=/opt/can-build/gopy_whatsapp_exports.map" \
    -o _whatsapp.cpython-313-x86_64-linux-gnu.so .

FROM runtime AS inbound-probe-tests
USER root
COPY --from=inbound-probe-builder /src/slidge_whatsapp/generated/_whatsapp.cpython-313-x86_64-linux-gnu.so /venv/lib/python3.13/site-packages/slidge_whatsapp/generated/
COPY tests/inbound/smoke_bridge_inbound_runtime.py /opt/can-tests/
USER slidge
RUN --network=none python -B /opt/can-tests/smoke_bridge_inbound_runtime.py > /tmp/inbound-result.json

FROM runtime AS release
# Estos COPY fuerzan las barreras, incluso construyendo sólo el target release.
COPY --from=runtime-tests /tmp/runtime-result.json /usr/share/can-bridge/verification.json
COPY --from=inbound-probe-tests /tmp/inbound-result.json /usr/share/can-bridge/inbound-verification.json
ARG VCS_REF=uncommitted
LABEL org.opencontainers.image.source="https://github.com/marcomolinaleija/puente-whatsapp" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="38" \
      org.can.whatsapp.web-version="2.3000.1052556849"
