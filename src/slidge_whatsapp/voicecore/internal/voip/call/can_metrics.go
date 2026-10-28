package call

import (
	"time"
	"codeberg.org/slidge/slidge-whatsapp/slidge_whatsapp/voicecore/internal/voip/transport"
)

// MediaEvent is synchronous and contains no PCM, payload, key, endpoint or JID.
// The observer must only accumulate counters; it must never block or re-enter us.
type MediaEvent struct {
	Kind      string
	Samples   int
	Duration  time.Duration
	Failed    bool
	Sequence  uint16
	Timestamp uint32
	SSRC      uint32
	Marker    bool
	Relays    transport.SendReport
}

func (m *CallManager) canObserve(event MediaEvent) {
	if m.OnMediaEvent != nil {
		m.OnMediaEvent(event)
	}
}
