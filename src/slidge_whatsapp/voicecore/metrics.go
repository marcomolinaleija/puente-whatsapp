package voicecore

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"io"
	"sync"
	"sync/atomic"
	"time"
	"codeberg.org/slidge/slidge-whatsapp/slidge_whatsapp/voicecore/internal/voip/call"
)

type relayCounters struct {
	Slot      uint64 `json:"slot"`
	Accepted  uint64 `json:"accepted"`
	Failed    uint64 `json:"failed"`
	NotOpen   uint64 `json:"not_open"`
	SendMaxUS uint64 `json:"send_max_us"`
}
type nativeReport struct {
	Tag            string            `json:"tag"`
	Final          bool              `json:"final"`
	ElapsedMS      int64             `json:"elapsed_ms"`
	Counters       map[string]uint64 `json:"counters"`
	Relays         []relayCounters   `json:"relays"`
	ReportsDropped uint64            `json:"reports_dropped"`
}
type nativeMetrics struct {
	mu          sync.Mutex
	callID      string // correlation only; never serialized
	report      *nativeReport
	started     time.Time
	clock       func() time.Time
	lastCapture time.Time
	lastRTP     time.Time
	relays      map[uint64]*relayCounters
	lastSSRC    uint32
	lastSeq     uint16
	lastTS      uint32
	lastSamples int
	haveRTP     bool
	queue       chan nativeReport
	stop        chan struct{}
	finished    chan struct{}
	once        sync.Once
	dropped     atomic.Uint64
}

func newNativeMetrics(writer io.Writer, interval time.Duration) *nativeMetrics {
	m := &nativeMetrics{queue: make(chan nativeReport, 8), stop: make(chan struct{}),
		finished: make(chan struct{}), clock: time.Now}
	go m.run(writer, interval)
	return m
}

func (m *nativeMetrics) state(id, state string) {
	if m == nil {
		return
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if id != "" && id != m.callID && state != "idle" && state != "ended" {
		m.finishLocked()
		var tag [8]byte
		if _, err := rand.Read(tag[:]); err != nil {
			return
		} // disable this report on entropy failure
		m.callID = id
		m.started = m.clock()
		m.lastCapture = time.Time{}
		m.lastRTP = time.Time{}
		m.haveRTP = false
		m.relays = make(map[uint64]*relayCounters)
		m.report = &nativeReport{Tag: hex.EncodeToString(tag[:]), Counters: make(map[string]uint64)}
	}
	if state == "ended" || state == "idle" {
		m.finishLocked()
	}
}

func (m *nativeMetrics) finishLocked() {
	if m.report == nil {
		return
	}
	m.enqueueLocked(true)
	m.report = nil
	m.callID = ""
	m.relays = nil
	m.haveRTP = false
}

// State callbacks and media observers only update memory; logging runs elsewhere.
func (m *nativeMetrics) observe(e call.MediaEvent) {
	if m == nil {
		return
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.report == nil {
		return
	}
	c := m.report.Counters
	switch e.Kind {
	case "socket_capture", "capture_queue_dropped", "capture_inactive":
		c[e.Kind+"_packets"]++
	case "capture":
		now := m.clock()
		if !m.lastCapture.IsZero() {
			c["pcm_gap_max_us"] = max(c["pcm_gap_max_us"], uint64(max(0, now.Sub(m.lastCapture).Microseconds())))
		}
		m.lastCapture = now
		c["pcm_consumed_blocks"]++
		c["pcm_consumed_samples"] += uint64(e.Samples)
	case "capture_not_ready":
		c["pcm_not_ready_blocks"]++
		c["pcm_not_ready_samples"] += uint64(e.Samples)
	case "encode", "keepalive_encode":
		c[e.Kind+"_frames"]++
		if e.Failed {
			c[e.Kind+"_errors"]++
		}
		us := uint64(max(0, e.Duration.Microseconds()))
		c[e.Kind+"_total_us"] += us
		c[e.Kind+"_max_us"] = max(c[e.Kind+"_max_us"], us)
		if e.Duration > 60*time.Millisecond {
			c[e.Kind+"_over_60ms"]++
		}
	case "rtp":
		now := m.clock()
		if !m.lastRTP.IsZero() {
			c["rtp_gap_max_us"] = max(c["rtp_gap_max_us"], uint64(max(0, now.Sub(m.lastRTP).Microseconds())))
		}
		m.lastRTP = now
		c["rtp_packets"]++
		c["rtp_timestamp_samples"] += uint64(e.Samples)
		if m.haveRTP && m.lastSSRC == e.SSRC && !e.Marker {
			if uint16(e.Sequence-m.lastSeq) != 1 {
				c["rtp_sequence_discontinuities"]++
			}
			if uint32(e.Timestamp-m.lastTS) != uint32(m.lastSamples) {
				c["rtp_clock_discontinuities"]++
			}
		}
		m.haveRTP = true
		m.lastSSRC = e.SSRC
		m.lastSeq = e.Sequence
		m.lastTS = e.Timestamp
		m.lastSamples = e.Samples
	case "protect_error", "relay_unmeasured":
		c[e.Kind]++
	case "relay":
		c["relay_broadcast_packets"]++
		if len(e.Relays) == 0 {
			c["relay_no_targets_packets"]++
		}
		for _, out := range e.Relays {
			r := m.relays[out.Slot]
			if r == nil {
				if len(m.relays) >= 32 {
					c["relay_untracked_outcomes"]++
					continue
				}
				r = &relayCounters{Slot: out.Slot}
				m.relays[out.Slot] = r
			}
			switch out.Status {
			case "accepted":
				r.Accepted++
			case "failed":
				r.Failed++
			case "not_open":
				r.NotOpen++
			default:
				c["relay_unknown_outcomes"]++
				continue
			}
			r.SendMaxUS = max(r.SendMaxUS, uint64(max(0, out.Duration.Microseconds())))
		}
	}
}

func (m *nativeMetrics) enqueueLocked(final bool) {
	if m.report == nil {
		return
	}
	out := *m.report
	out.Final = final
	out.ElapsedMS = m.clock().Sub(m.started).Milliseconds()
	out.Counters = make(map[string]uint64, len(m.report.Counters))
	for k, v := range m.report.Counters {
		out.Counters[k] = v
	}
	out.Relays = make([]relayCounters, 0, len(m.relays))
	for _, r := range m.relays {
		out.Relays = append(out.Relays, *r)
	}
	out.ReportsDropped = m.dropped.Load()
	select {
	case m.queue <- out:
	default:
		m.dropped.Add(1)
	}
}

func (m *nativeMetrics) run(writer io.Writer, interval time.Duration) {
	defer close(m.finished)
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	write := func(r nativeReport) {
		body, err := json.Marshal(r)
		if err == nil {
			_, _ = writer.Write(append(append([]byte("CAN_VOICE_NATIVE "), body...), '\n'))
		}
	}
	for {
		select {
		case r := <-m.queue:
			write(r)
		case <-ticker.C:
			m.mu.Lock()
			m.enqueueLocked(false)
			m.mu.Unlock()
		case <-m.stop:
			for {
				select {
				case r := <-m.queue:
					write(r)
				default:
					return
				}
			}
		}
	}
}

func (m *nativeMetrics) close() {
	if m == nil {
		return
	}
	m.once.Do(func() { m.mu.Lock(); m.finishLocked(); m.mu.Unlock(); close(m.stop) })
}

func (s *session) captureMetric(kind string) {
	s.mu.Lock()
	m := s.metrics
	s.mu.Unlock()
	m.observe(call.MediaEvent{Kind: kind})
}
