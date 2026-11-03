package call

import (
	"errors"
	"io"
	"log/slog"
	"testing"
	"codeberg.org/slidge/slidge-whatsapp/slidge_whatsapp/voicecore/internal/voip/core"
	"codeberg.org/slidge/slidge-whatsapp/slidge_whatsapp/voicecore/internal/voip/media"
	"codeberg.org/slidge/slidge-whatsapp/slidge_whatsapp/voicecore/internal/voip/transport"
)

type canCodecFixture struct{ calls int }

func (c *canCodecFixture) Encode([]float32) ([]byte, error) {
	c.calls++
	if c.calls == 2 {
		return nil, errors.New("fixture encoder failure")
	}
	return []byte{0x50, 1, 2, 3}, nil
}
func (*canCodecFixture) Decode([]byte) ([]float32, error) { return nil, nil }
func (*canCodecFixture) FrameSize() int                   { return 960 }
func (*canCodecFixture) SampleRate() int                  { return 16000 }
func (*canCodecFixture) Close()                           {}

type canMeasuredRelay struct {
	canRelayFixture
	packets int
}

func (*canMeasuredRelay) HasConnection() bool { return true }
func (r *canMeasuredRelay) BroadcastMeasured(b []byte) transport.SendReport {
	r.packets++
	if len(b) < 16 {
		panic("unprotected frame sent")
	}
	return transport.SendReport{{Slot: 1, Status: "accepted"}, {Slot: 2, Status: "failed"}}
}

func TestCANMediaCountersCoverConsumptionEncodeFailureAndRelayAcceptance(t *testing.T) {
	m := NewCallManager(nil, slog.New(slog.NewTextHandler(io.Discard, nil)))
	var events []MediaEvent
	m.OnMediaEvent = func(e MediaEvent) { events = append(events, e) }
	m.FeedCapturedPCM(make([]float32, 320))
	if len(events) != 1 || events[0].Kind != "capture_not_ready" {
		t.Fatal("unready input not measured")
	}
	key := make([]byte, 32)
	a, _ := media.DerivePerJidSrtpKey(key, "fixture-a:0@lid")
	b, _ := media.DerivePerJidSrtpKey(key, "fixture-b:0@lid")
	var err error
	m.srtpSession, err = media.NewSrtpSession(a, b, core.SRTPSendAuthTagLen, core.SRTPRecvAuthTagLen)
	if err != nil {
		t.Fatal(err)
	}
	r := &canMeasuredRelay{}
	m.relay = r
	codec := &canCodecFixture{}
	m.codec = codec
	m.rtpSession = media.NewWhatsAppOpusSession(1)
	for range 6 {
		m.FeedCapturedPCM(make([]float32, 320))
	}
	counts := map[string]int{}
	samples, failed := 0, 0
	for _, e := range events {
		counts[e.Kind]++
		if e.Kind == "capture" {
			samples += e.Samples
		}
		if e.Kind == "encode" && e.Failed {
			failed++
		}
		if e.Kind == "relay" && (len(e.Relays) != 2 || e.Relays[1].Status != "failed") {
			t.Fatal("relay results hidden")
		}
	}
	if samples != 1920 || counts["encode"] != 2 || failed != 1 || counts["rtp"] != 1 || r.packets != 1 {
		t.Fatalf("wrong media boundaries: %v, samples=%d failures=%d", counts, samples, failed)
	}
}
