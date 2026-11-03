package voicecore

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
	"time"
	"codeberg.org/slidge/slidge-whatsapp/slidge_whatsapp/voicecore/internal/voip/call"
	"codeberg.org/slidge/slidge-whatsapp/slidge_whatsapp/voicecore/internal/voip/transport"
)

func TestNativeMetricsArePerCallAnonymousAndDistinguishMediaStages(t *testing.T) {
	var output bytes.Buffer
	m := newNativeMetrics(&output, time.Hour)
	m.state("private-call-id", "active")
	m.observe(call.MediaEvent{Kind: "socket_capture"})
	m.observe(call.MediaEvent{Kind: "capture_queue_dropped"})
	m.observe(call.MediaEvent{Kind: "capture", Samples: 320})
	m.observe(call.MediaEvent{Kind: "encode", Failed: true, Duration: 61 * time.Millisecond})
	m.observe(call.MediaEvent{Kind: "private-unknown-event"})
	m.observe(call.MediaEvent{Kind: "relay", Relays: transport.SendReport{{Slot: 2, Status: "accepted"}, {Slot: 3, Status: "failed"}}})
	m.observe(call.MediaEvent{Kind: "rtp", SSRC: 7, Sequence: 65535, Timestamp: 0xffffff00, Samples: 960})
	m.observe(call.MediaEvent{Kind: "rtp", SSRC: 7, Sequence: 0, Timestamp: 704, Samples: 960})
	m.state("private-call-id", "ended")
	m.state("other-private-call", "active")
	m.observe(call.MediaEvent{Kind: "socket_capture"})
	m.close()
	<-m.finished
	text := output.String()
	if strings.Contains(text, "private") {
		t.Fatal("identity or arbitrary label leaked")
	}
	lines := strings.Split(strings.TrimSpace(text), "\n")
	if len(lines) != 2 {
		t.Fatalf("expected final report for each call: %s", text)
	}
	var reports [2]nativeReport
	for i, l := range lines {
		if err := json.Unmarshal([]byte(strings.TrimPrefix(l, "CAN_VOICE_NATIVE ")), &reports[i]); err != nil {
			t.Fatal(err)
		}
		if !reports[i].Final || len(reports[i].Tag) != 16 {
			t.Fatal("invalid final report")
		}
	}
	c := reports[0].Counters
	if c["pcm_consumed_samples"] != 320 || c["encode_errors"] != 1 || c["encode_over_60ms"] != 1 || c["capture_queue_dropped_packets"] != 1 || c["rtp_clock_discontinuities"] != 0 || c["rtp_sequence_discontinuities"] != 0 {
		t.Fatalf("wrong counters: %v", c)
	}
	if len(reports[0].Relays) != 2 || reports[0].Tag == reports[1].Tag || reports[1].Counters["pcm_consumed_samples"] != 0 {
		t.Fatal("call reports mixed")
	}
}

type blockedMetricWriter struct {
	entered chan struct{}
	release chan struct{}
}

func (w *blockedMetricWriter) Write(p []byte) (int, error) {
	select {
	case w.entered <- struct{}{}:
	default:
	}
	<-w.release
	return len(p), nil
}
func TestSlowMetricsOutputCannotBlockMediaAndFinalQueueIsBounded(t *testing.T) {
	w := &blockedMetricWriter{make(chan struct{}, 1), make(chan struct{})}
	m := newNativeMetrics(w, time.Hour)
	m.state("first", "active")
	m.state("first", "ended")
	<-w.entered
	done := make(chan struct{})
	go func() {
		for range 20 {
			m.state("fixture", "active")
			m.observe(call.MediaEvent{Kind: "capture", Samples: 320})
			m.state("fixture", "ended")
		}
		m.close()
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("metrics blocked media")
	}
	if m.dropped.Load() == 0 {
		t.Fatal("unbounded metrics queue")
	}
	close(w.release)
	<-m.finished
}

func TestCaptureQueueAccountsForEveryDiscardWithoutChangingRetainedOrder(t *testing.T) {
	q := make(chan []byte, 3)
	dropped := 0
	for i := range 17 {
		if latest(q, []byte{byte(i)}) {
			dropped++
		}
	}
	if dropped != 14 {
		t.Fatalf("dropped %d blocks", dropped)
	}
	for i := 14; i < 17; i++ {
		if b := <-q; b[0] != byte(i) {
			t.Fatal("queue order changed")
		}
	}
}

func TestNativeMetricsMeasureWallClockPausesEvenWithContinuousRTPTimestamps(t *testing.T) {
	var output bytes.Buffer
	m := newNativeMetrics(&output, time.Hour)
	now := time.Unix(1, 0)
	m.clock = func() time.Time { return now }
	m.state("fixture", "active")
	m.observe(call.MediaEvent{Kind: "capture", Samples: 320})
	m.observe(call.MediaEvent{Kind: "rtp", SSRC: 1, Sequence: 1, Timestamp: 960, Samples: 960})
	now = now.Add(500 * time.Millisecond)
	m.observe(call.MediaEvent{Kind: "capture", Samples: 320})
	m.observe(call.MediaEvent{Kind: "rtp", SSRC: 1, Sequence: 2, Timestamp: 1920, Samples: 960})
	m.close()
	<-m.finished
	var report nativeReport
	if err := json.Unmarshal(bytes.TrimSpace(bytes.TrimPrefix(output.Bytes(), []byte("CAN_VOICE_NATIVE "))), &report); err != nil {
		t.Fatal(err)
	}
	if report.Counters["pcm_gap_max_us"] != 500000 || report.Counters["rtp_gap_max_us"] != 500000 || report.Counters["rtp_clock_discontinuities"] != 0 {
		t.Fatal("sample clock hid a real-time pause")
	}
}
