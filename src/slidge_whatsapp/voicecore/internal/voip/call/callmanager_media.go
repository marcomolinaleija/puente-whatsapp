package call

import (
	"time"
	"codeberg.org/slidge/slidge-whatsapp/slidge_whatsapp/voicecore/internal/voip/core"
	"codeberg.org/slidge/slidge-whatsapp/slidge_whatsapp/voicecore/internal/voip/media"
	"codeberg.org/slidge/slidge-whatsapp/slidge_whatsapp/voicecore/internal/voip/transport"
)

func (m *CallManager) initCodec() {
	if m.codec != nil {
		return
	}
	codec, err := media.NewMLowCodec(media.DefaultCodecOptions)
	if err != nil {
		m.log.Warn("MLow codec unavailable — call will run signaling-only (no audio)", "err", err)
		return
	}
	m.codec = codec
}

func (m *CallManager) FeedCapturedPCM(data []float32) {
	m.mu.Lock()
	defer m.mu.Unlock()

	if m.codec == nil || m.rtpSession == nil || m.srtpSession == nil || !m.relay.HasConnection() {
		m.canObserve(MediaEvent{Kind: "capture_not_ready", Samples: len(data)})
		return
	}
	m.canObserve(MediaEvent{Kind: "capture", Samples: len(data)})
	m.lastCaptureAt = time.Now()
	frameSize := m.codec.FrameSize()
	if m.encodeBuf == nil {
		m.encodeBuf = make([]float32, frameSize)
		m.encodeBufPos = 0
	}

	offset := 0
	for offset < len(data) {
		toCopy := min(len(data)-offset, frameSize-m.encodeBufPos)
		copy(m.encodeBuf[m.encodeBufPos:], data[offset:offset+toCopy])
		m.encodeBufPos += toCopy
		offset += toCopy
		if m.encodeBufPos < frameSize {
			break
		}
		frame := make([]float32, frameSize)
		copy(frame, m.encodeBuf)
		m.encodeBufPos = 0

		started := time.Now()
		opus, err := m.codec.Encode(frame)
		m.canObserve(MediaEvent{Kind: "encode", Samples: len(frame), Duration: time.Since(started), Failed: err != nil})
		if err != nil {
			m.log.Debug("encode error", "err", err)
			continue
		}
		m.sendOpusFrameLocked(opus)
	}
}

func (m *CallManager) sendOpusFrameLocked(opus []byte) {
	if m.rtpSession == nil || m.srtpSession == nil {
		return
	}
	marker := !m.firstPacketSent
	pkt := m.rtpSession.CreatePacketWithDuration(opus, m.codec.FrameSize(), marker)
	m.canObserve(MediaEvent{Kind: "rtp", Samples: m.codec.FrameSize(), Sequence: pkt.Header.SequenceNumber, Timestamp: pkt.Header.Timestamp, SSRC: pkt.Header.Ssrc, Marker: marker})
	if m.debeEnabled {
		pkt.Header.Extension = true
		pkt.Header.ExtensionProfile = 0xbede
		pkt.Header.ExtensionData = nil
	}
	m.firstPacketSent = true

	srtp, err := m.srtpSession.Protect(pkt)
	if err != nil {
		m.canObserve(MediaEvent{Kind: "protect_error"})
		m.log.Debug("srtp protect error", "err", err)
		return
	}
	if measured, ok := m.relay.(interface {
		BroadcastMeasured([]byte) transport.SendReport
	}); ok && m.OnMediaEvent != nil {
		m.canObserve(MediaEvent{Kind: "relay", Relays: measured.BroadcastMeasured(srtp)})
	} else {
		m.relay.Broadcast(srtp)
		m.canObserve(MediaEvent{Kind: "relay_unmeasured"})
	}
}

func (m *CallManager) startSilenceKeepaliveLocked() {
	if m.keepaliveStop != nil || m.codec == nil {
		return
	}
	stop := make(chan struct{})
	m.keepaliveStop = stop
	frameSize := m.codec.FrameSize()
	go func() {
		ticker := time.NewTicker(60 * time.Millisecond)
		defer ticker.Stop()
		silence := make([]float32, frameSize)
		for {
			select {
			case <-stop:
				return
			case <-ticker.C:
				m.mu.Lock()
				ready := m.codec != nil && m.rtpSession != nil && m.srtpSession != nil && m.relay.HasConnection()
				idle := time.Since(m.lastCaptureAt) > 120*time.Millisecond
				if ready && idle {
					started := time.Now()
					opus, err := m.codec.Encode(silence)
					m.canObserve(MediaEvent{Kind: "keepalive_encode", Samples: len(silence), Duration: time.Since(started), Failed: err != nil})
					if err == nil {
						m.sendOpusFrameLocked(opus)
					}
				}
				m.mu.Unlock()
			}
		}
	}()
}

func (m *CallManager) onRelayData(data []byte) {
	if transport.IsStunPacket(data) {
		return
	}
	if !transport.IsRtpPacket(data) {
		return
	}
	if len(data) < 12 {
		return
	}
	pt := data[1] & 0x7f
	if pt != core.PayloadTypeWhatsAppOpus {
		return
	}

	m.mu.Lock()
	if m.srtpSession == nil || m.codec == nil {
		m.mu.Unlock()
		return
	}
	ssrc := uint32(data[8])<<24 | uint32(data[9])<<16 | uint32(data[10])<<8 | uint32(data[11])
	if ssrc == m.selfSsrc {
		m.mu.Unlock()
		return
	}
	if !m.actualPeerSet {
		m.actualPeerSet = true
		if !containsSsrc(m.peerSsrcs, ssrc) {
			m.peerSsrcs = []uint32{ssrc}
			m.relay.SetSubscriptionSsrc(ssrc)
			go m.relay.ResendSubscriptions()
		}
	}
	srtp := m.srtpSession
	codec := m.codec
	m.mu.Unlock()

	pkt, err := srtp.Unprotect(data)
	if err != nil {
		m.log.Debug("srtp unprotect error", "err", err)
		return
	}
	if len(pkt.Payload) == 0 {
		return
	}
	pcm, err := codec.Decode(pkt.Payload)
	if err != nil {
		return
	}
	pcm = media.NormalizeFrame(pcm, codec.FrameSize())
	if m.OnPeerAudio != nil {
		m.OnPeerAudio(pcm)
	}
}
