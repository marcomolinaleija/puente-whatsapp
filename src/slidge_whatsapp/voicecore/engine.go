package voicecore

import (
	"context"
	"io"
	"log/slog"
	"os"
	"strings"
	"time"

	"go.mau.fi/whatsmeow"
	waBinary "go.mau.fi/whatsmeow/binary"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"codeberg.org/slidge/slidge-whatsapp/slidge_whatsapp/voicecore/internal/voip/call"
	"codeberg.org/slidge/slidge-whatsapp/slidge_whatsapp/voicecore/internal/voip/core"
	"codeberg.org/slidge/slidge-whatsapp/slidge_whatsapp/voicecore/internal/voip/signaling"
	"codeberg.org/slidge/slidge-whatsapp/slidge_whatsapp/voicecore/internal/wa"
)

type nativeEngine struct {
	manager *call.CallManager
	metrics *nativeMetrics
}

func newEngine(client *whatsmeow.Client, s *session) engine {
	s.mu.Lock()
	epoch := s.epoch
	s.mu.Unlock()
	m := call.NewCallManager(wa.NewSocket(client), slog.New(slog.NewTextHandler(io.Discard, nil)))
	var metrics *nativeMetrics
	if os.Getenv("CAN_VOICE_METRICS") == "1" {
		metrics = newNativeMetrics(os.Stderr, 10*time.Second)
	}
	s.mu.Lock()
	s.metrics = metrics
	s.mu.Unlock()
	if metrics != nil {
		m.OnMediaEvent = metrics.observe
	}
	m.OnStateChange = func(info *call.CallInfo) {
		// Callback runs under the engine lock: copy values, never re-enter it.
		s.mu.Lock()
		if epoch != s.epoch || s.closing {
			s.mu.Unlock()
			return
		}
		s.state = State{Type: "state", Revision: s.state.Revision + 1,
			CallID: info.CallID, Peer: info.PeerJid, Direction: string(info.Direction),
			State: string(info.StateData.State), Reason: string(info.StateData.EndReason)}
		s.mu.Unlock()
		metrics.state(info.CallID, string(info.StateData.State))
		select {
		case s.changed <- struct{}{}:
		default:
		}
	}
	m.OnPeerAudio = func(pcm []float32) {
		s.mu.Lock()
		defer s.mu.Unlock()
		if epoch == s.epoch && !s.closing {
			s.audio(pcm)
		}
	}
	return &nativeEngine{m, metrics}
}

func validPeer(peer string) bool {
	j, err := types.ParseJID(peer)
	return err == nil && j.User != "" && j.Device == 0 && j.RawAgent == 0 && !strings.ContainsAny(j.User, ":/ \r\n\t") &&
		(j.Server == types.DefaultUserServer || j.Server == types.HiddenUserServer)
}

func (e *nativeEngine) Command(ctx context.Context, c Command) error {
	switch c.Action {
	case "dial":
		peer, _ := types.ParseJID(c.Peer)
		if err := e.manager.StartCall(ctx, signaling.GenerateCallID(), peer, false); err != nil {
			_ = e.manager.EndCall(ctx, core.EndCallReasonFailed)
			return err
		}
	case "answer":
		if err := e.manager.AcceptCall(ctx, c.CallID); err != nil {
			_ = e.manager.EndCall(ctx, core.EndCallReasonFailed)
			return err
		}
	case "reject":
		return e.manager.RejectCall(ctx, c.CallID, core.EndCallReasonDeclined)
	case "hangup":
		return e.manager.EndCall(ctx, core.EndCallReasonUserEnded)
	}
	return nil
}

func (e *nativeEngine) PCM(pcm []float32) { e.manager.FeedCapturedPCM(pcm) }
func (e *nativeEngine) Close(ctx context.Context) {
	e.manager.Dispose(ctx)
	e.metrics.close()
}

func wrap(from types.JID, node *waBinary.Node) *waBinary.Node {
	if node == nil {
		return nil
	}
	return &waBinary.Node{Tag: "call", Attrs: waBinary.Attrs{"from": from}, Content: []waBinary.Node{*node}}
}

func (e *nativeEngine) Signal(ctx context.Context, signal any, state State) {
	var node *waBinary.Node
	var from types.JID
	var kind string
	switch v := signal.(type) {
	case *events.CallOffer:
		if !v.GroupJID.IsEmpty() {
			return
		}
		node, from, kind = v.Data, v.From, "offer"
	case *events.CallAccept:
		node, from, kind = v.Data, v.From, "accept"
	case *events.CallTransport:
		node, from, kind = v.Data, v.From, "transport"
	case *events.CallTerminate:
		node, from, kind = v.Data, v.From, "end"
	case *events.CallReject:
		node, from, kind = v.Data, v.From, "end"
	default:
		return
	}
	wrapped := wrap(from, node)
	if wrapped == nil {
		return
	}
	info := signaling.ExtractNodeInfo(wrapped)
	if info == nil || info.CallID == "" || len(info.CallID) > 128 {
		return
	}
	if kind == "offer" {
		if state.State != "idle" && state.State != "ended" {
			return
		}
		if !validPeer(from.ToNonAD().String()) {
			return
		}
		for _, child := range node.GetChildren() {
			if child.Tag == "video" {
				return
			}
		}
		e.manager.HandleCallOffer(ctx, wrapped, from)
		return
	}
	// Upstream handlers do not correlate IDs. Never route another call into this one.
	if state.CallID != info.CallID || state.State == "ended" {
		return
	}
	peer, err := types.ParseJID(state.Peer)
	if err != nil || peer.ToNonAD() != from.ToNonAD() {
		return
	}
	switch kind {
	case "accept":
		e.manager.HandleCallAccept(ctx, wrapped, from)
	case "transport":
		e.manager.HandleCallTransport(ctx, wrapped, from)
	case "end":
		e.manager.HandleCallTerminate(wrapped)
	}
}
