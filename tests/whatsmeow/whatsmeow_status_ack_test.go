package whatsmeow

import (
	"context"
	"testing"

	waBinary "go.mau.fi/whatsmeow/binary"
	"go.mau.fi/whatsmeow/socket"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
	waLog "go.mau.fi/whatsmeow/util/log"
)

// Observe the actual serialized send boundary. Cancellation makes NoiseSocket
// stop before encryption/IO: no real account, network or production test hook.
type canStatusSendLog struct {
	waLog.Logger
	nodes []waBinary.Node
}

func (l *canStatusSendLog) Debugf(format string, args ...any) {
	if len(args) == 1 {
		if node, ok := args[0].(*waBinary.Node); ok {
			l.nodes = append(l.nodes, *node)
		}
	}
}

func TestCANStatusNodesAcknowledged(t *testing.T) {
	for _, tc := range []struct {
		name      string
		mediaType string
		offline   bool
		malformed bool
	}{
		{name: "live media", mediaType: "media"},
		{name: "live text", mediaType: "text"},
		{name: "pending offline status", mediaType: "media", offline: true},
		{name: "malformed status is nacked", mediaType: "media", malformed: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			own := types.NewJID("15550000000", types.DefaultUserServer)
			participant := types.NewJID("15550000001", types.DefaultUserServer)
			cli := NewClient(&store.Device{ID: &own}, waLog.Noop)
			handler := cli.nodeHandlers["status"]
			if handler == nil {
				t.Fatal("status stanza has no handler; WhatsApp will retain it unacked")
			}
			log := &canStatusSendLog{Logger: waLog.Noop}
			cli.sendLog = log
			cli.socket = &socket.NoiseSocket{}
			node := &waBinary.Node{Tag: "status", Attrs: waBinary.Attrs{
				"id": "SYNTHETIC-STATUS-001", "from": types.StatusBroadcastJID,
				"participant": participant, "type": tc.mediaType, "t": "1700000000",
			}}
			if tc.offline {
				node.Attrs["offline"] = "true"
			}
			if tc.malformed {
				delete(node.Attrs, "participant")
			}
			ctx, cancel := context.WithCancel(context.Background())
			cancel()
			handler(ctx, node)
			if len(log.nodes) != 1 {
				t.Fatalf("sent %d nodes, want one ACK", len(log.nodes))
			}
			ack := log.nodes[0]
			if ack.Tag != "ack" || ack.Attrs["class"] != "status" ||
				ack.Attrs["id"] != node.Attrs["id"] || ack.Attrs["to"] != types.StatusBroadcastJID ||
				ack.Attrs["type"] != tc.mediaType {
				t.Fatalf("wrong status ACK: %#v", ack)
			}
			if !tc.malformed && ack.Attrs["participant"] != participant {
				t.Fatal("status ACK lost participant")
			}
			if tc.malformed {
				if ack.Attrs["error"] != NackParsingError {
					t.Fatal("malformed status was acknowledged as valid")
				}
			} else if _, exists := ack.Attrs["error"]; exists {
				t.Fatal("valid status must receive a plain ACK, not a NACK")
			}
		})
	}
}
