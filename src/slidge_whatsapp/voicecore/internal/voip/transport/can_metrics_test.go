package transport

import (
	"bytes"
	"errors"
	"testing"
)

type canSendFixture struct {
	data []byte
	err  error
}

func (s *canSendFixture) Send(b []byte) error { s.data = append([]byte(nil), b...); return s.err }
func TestCANSendMeasurementPreservesBytesAndSeparatesErrors(t *testing.T) {
	payload := []byte{1, 2, 3}
	for _, failed := range []bool{false, true} {
		s := &canSendFixture{}
		if failed {
			s.err = errors.New("private endpoint error must not escape")
		}
		out := canMeasureSend(3, s, payload)
		expected := "accepted"
		if failed {
			expected = "failed"
		}
		if out.Status != expected || out.Slot != 3 || out.Duration < 0 || !bytes.Equal(payload, s.data) {
			t.Fatal("incorrect send result")
		}
	}
	m := NewSctpRelayManager(nil)
	m.connections["private-endpoint"] = &relayConnection{canMetricSlot: 4, state: relayStateConnecting}
	out := m.BroadcastMeasured(payload)
	if len(out) != 1 || out[0].Status != "not_open" || out[0].Slot != 4 {
		t.Fatal("closed relay counted as sent")
	}
}
