package call

import (
	"context"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
	"io"
	"log/slog"
	"sync/atomic"
	"testing"
	"time"
	"codeberg.org/slidge/slidge-whatsapp/slidge_whatsapp/voicecore/internal/voip/core"
	"codeberg.org/slidge/slidge-whatsapp/slidge_whatsapp/voicecore/internal/voip/transport"
	"codeberg.org/slidge/slidge-whatsapp/slidge_whatsapp/voicecore/internal/wa"
)

type canRelayFixture struct {
	configured atomic.Int32
	cleaned    atomic.Int32
	entered    chan struct{}
	release    chan struct{}
}

func (r *canRelayFixture) SetSsrc(uint32)                   {}
func (r *canRelayFixture) SetSubscriptionSsrc(uint32)       {}
func (r *canRelayFixture) SetOnConnected(func(string, int)) {}
func (r *canRelayFixture) SetOnReceive(func([]byte))        {}
func (r *canRelayFixture) ResendSubscriptions()             {}
func (r *canRelayFixture) Broadcast([]byte)                 {}
func (r *canRelayFixture) HasConnection() bool              { return false }
func (r *canRelayFixture) ConnectedCount() int              { return 0 }
func (r *canRelayFixture) Cleanup()                         { r.cleaned.Add(1) }
func (r *canRelayFixture) ConfigureRelays([]transport.RelayConfig) {
	r.configured.Add(1)
	if r.entered != nil {
		close(r.entered)
		<-r.release
	}
}

func TestCANDisposedEngineCannotReopenMedia(t *testing.T) {
	m := NewCallManager(nil, slog.New(slog.NewTextHandler(io.Discard, nil)))
	r := &canRelayFixture{}
	m.relay = r
	m.Dispose(context.Background())
	// Simulate a late callback carrying relay endpoints after local teardown.
	m.currentCall = NewOutgoingCall("fixture-call", "fixture@lid", "self@lid", core.CallMediaTypeAudio)
	endpoints := []core.RelayEndpoint{{IP: "127.0.0.1", Key: "fixture-key", RawToken: []byte{1}}}
	m.connectRelays(endpoints)
	if r.configured.Load() != 0 {
		t.Fatal("disposed engine reopened relay")
	}
	if err := m.StartCall(context.Background(), "next", types.NewJID("fixture", types.HiddenUserServer), false); err == nil {
		t.Fatal("disposed engine reused")
	}
	if m.canContext.Err() == nil {
		t.Fatal("ack work survived disposal")
	}
}

func TestCANRelayCleanupWaitsForSetupAndPreventsReopen(t *testing.T) {
	m := NewCallManager(nil, slog.New(slog.NewTextHandler(io.Discard, nil)))
	r := &canRelayFixture{entered: make(chan struct{}), release: make(chan struct{})}
	m.relay = r
	m.currentCall = NewOutgoingCall("fixture-call", "fixture@lid", "self@lid", core.CallMediaTypeAudio)
	endpoints := []core.RelayEndpoint{{IP: "127.0.0.1", Key: "fixture-key", RawToken: []byte{1}}}
	configured := make(chan struct{})
	go func() { m.connectRelays(endpoints); close(configured) }()
	<-r.entered
	m.mu.Lock()
	_ = m.currentCall.ApplyTransition(Transition{Type: TransitionTerminated})
	m.mu.Unlock()
	done := make(chan struct{})
	go func() { m.Dispose(context.Background()); close(done) }()
	close(r.release)
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("dispose stalled")
	}
	<-configured
	m.connectRelays(endpoints)
	if r.configured.Load() != 1 || r.cleaned.Load() == 0 {
		t.Fatal("relay lifecycle was not serialized")
	}
}

func TestCANAcceptWithoutKeyFailsBeforeTransition(t *testing.T) {
	m := NewCallManager(nil, slog.New(slog.NewTextHandler(io.Discard, nil)))
	m.currentCall = NewIncomingCall("fixture-call", "fixture@lid", "fixture@lid", "", core.CallMediaTypeAudio)
	if err := m.AcceptCall(context.Background(), "fixture-call"); err == nil {
		t.Fatal("accepted without key")
	}
	if m.currentCall.StateData.State != core.CallStateIncomingRinging {
		t.Fatal("failed answer advanced state")
	}
}

func TestCANTeardownPropagatesSignalingFailureAfterMediaCleanup(t *testing.T) {
	for _, action := range []string{"hangup", "reject"} {
		t.Run(action, func(t *testing.T) {
			client := whatsmeow.NewClient(&store.Device{}, nil)
			m := NewCallManager(wa.NewSocket(client), slog.New(slog.NewTextHandler(io.Discard, nil)))
			r := &canRelayFixture{}
			m.relay = r
			m.currentCall = NewIncomingCall("fixture-call", "fixture@lid", "fixture@lid", "", core.CallMediaTypeAudio)
			ctx, cancel := context.WithTimeout(context.Background(), time.Second)
			defer cancel()
			var err error
			if action == "hangup" {
				err = m.EndCall(ctx, core.EndCallReasonUserEnded)
			} else {
				err = m.RejectCall(ctx, "fixture-call", core.EndCallReasonDeclined)
			}
			if err == nil {
				t.Fatal("disconnected signaling falsely reported success")
			}
			if !m.currentCall.IsEnded() || r.cleaned.Load() == 0 {
				t.Fatal("media survived failed signaling")
			}
			m.Dispose(ctx)
		})
	}
}
