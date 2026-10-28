package transport

import (
	"time"
)

type SendOutcome struct {
	Slot     uint64
	Status   string
	Duration time.Duration
}
type SendReport []SendOutcome

type canSender interface{ Send([]byte) error }

func canMeasureSend(slot uint64, sender canSender, data []byte) SendOutcome {
	started := time.Now()
	err := sender.Send(data)
	status := "accepted"
	if err != nil {
		status = "failed"
	}
	return SendOutcome{Slot: slot, Status: status, Duration: time.Since(started)}
}

// BroadcastMeasured observes DataChannel.Send acceptance, NOT remote delivery.
// Snapshot state under the existing mutex; send outside it, as Broadcast does.
func (m *SctpRelayManager) BroadcastMeasured(data []byte) SendReport {
	type target struct {
		slot   uint64
		sender canSender
	}
	m.mu.Lock()
	targets := make([]target, 0, len(m.connections))
	for _, c := range m.connections {
		var sender canSender
		if c.state == relayStateOpen && c.channel != nil {
			sender = c.channel
		}
		targets = append(targets, target{c.canMetricSlot, sender})
	}
	m.mu.Unlock()
	report := make(SendReport, 0, len(targets))
	for _, c := range targets {
		if c.sender == nil {
			report = append(report, SendOutcome{Slot: c.slot, Status: "not_open"})
			continue
		}
		report = append(report, canMeasureSend(c.slot, c.sender, data))
	}
	return report
}
