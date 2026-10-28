package voicecore

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"time"

	"github.com/coder/websocket"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

var defaultHub = &hub{sessions: make(map[*whatsmeow.Client]*session)}
var listenOnce sync.Once

type hub struct {
	mu       sync.Mutex
	sessions map[*whatsmeow.Client]*session
}

// Attach is opt-in and uses only the already owned native client.
func Attach(ctx context.Context, client *whatsmeow.Client) {
	path := os.Getenv("CAN_VOICE_SOCKET")
	if path == "" || client == nil {
		return
	}
	listenOnce.Do(func() {
		// Failure disables voice only; no reconnect or mutation of the message session.
		if listener, err := privateListener(path); err == nil {
			server := &http.Server{Handler: defaultHub, ReadHeaderTimeout: 5 * time.Second, IdleTimeout: 30 * time.Second}
			go func() { _ = server.Serve(listener) }()
		}
	})
	defaultHub.attach(ctx, client)
}

func (h *hub) attach(ctx context.Context, client *whatsmeow.Client) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if old := h.sessions[client]; old != nil {
		old.stop()
	}
	s := newSession(ctx, client.Store.GetJID().String(), func(s *session) engine { return newEngine(client, s) })
	h.sessions[client] = s
	go func() {
		<-s.done
		h.mu.Lock()
		defer h.mu.Unlock()
		if h.sessions[client] == s {
			delete(h.sessions, client)
		}
	}()
}

func Detach(client *whatsmeow.Client) {
	defaultHub.mu.Lock()
	s := defaultHub.sessions[client]
	delete(defaultHub.sessions, client)
	defaultHub.mu.Unlock()
	if s != nil {
		s.stop()
	}
}

// Dispatch performs no network I/O and never waits for voice workers.
func Dispatch(client *whatsmeow.Client, event any) {
	defaultHub.mu.Lock()
	s := defaultHub.sessions[client]
	defaultHub.mu.Unlock()
	if s == nil {
		return
	}
	switch event.(type) {
	case *events.Connected:
		s.mu.Lock()
		s.device = client.Store.GetJID().String()
		s.mu.Unlock()
	case *events.Disconnected:
		s.mu.Lock()
		cancel := s.leaseCancel
		s.mu.Unlock()
		if cancel != nil {
			cancel()
		}
	case *events.CallOffer, *events.CallAccept, *events.CallTransport, *events.CallTerminate, *events.CallReject:
		s.mu.Lock()
		enabled := s.owner != "" && !s.closing
		s.mu.Unlock()
		if enabled {
			s.signal(event)
		}
	}
}

func privateListener(path string) (net.Listener, error) {
	if !filepath.IsAbs(path) {
		return nil, errors.New("absolute socket path required")
	}
	parent := filepath.Dir(path)
	// Python creates a fresh private directory beneath the managed bridge home.
	info, err := os.Lstat(parent)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 || info.Mode().Perm() != 0700 {
		return nil, errors.New("private socket directory required")
	}
	if _, err = os.Lstat(path); !os.IsNotExist(err) {
		// Never replace an existing file, link, or live listener.
		return nil, errors.New("socket path already exists")
	}
	listener, err := net.Listen("unix", path)
	if err != nil {
		return nil, err
	}
	if err = os.Chmod(path, 0600); err != nil {
		listener.Close()
		return nil, err
	}
	return listener, nil
}

func (h *hub) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/session" || r.Method != "GET" {
		http.NotFound(w, r)
		return
	}
	device, owner := r.Header.Get("X-CAN-Device"), r.Header.Get("X-CAN-Resource")
	if device == "" || len(device) > 256 || len(owner) > 1024 {
		http.Error(w, "forbidden", 403)
		return
	}
	h.mu.Lock()
	var selected *session
	var client *whatsmeow.Client
	for candidate, s := range h.sessions {
		s.mu.Lock()
		match := s.device == device
		s.mu.Unlock()
		if match {
			selected = s
			client = candidate
			break
		}
	}
	h.mu.Unlock()
	if selected == nil {
		http.Error(w, "unavailable", 503)
		return
	}
	if !selected.acquire(owner) {
		http.Error(w, "busy", 409)
		return
	}
	conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{Subprotocols: []string{"can-voice-1"}})
	if err != nil {
		selected.mu.Lock()
		selected.owner = ""
		selected.mu.Unlock()
		return
	}
	defer conn.CloseNow()
	// The actor tears down media before granting another resource a lease.
	defer func() {
		select {
		case selected.release <- struct{}{}:
		default:
		}
	}()
	conn.SetReadLimit(2048)
	ctx, cancel := context.WithCancel(selected.ctx)
	defer cancel()
	selected.mu.Lock()
	selected.leaseCancel = cancel
	selected.mu.Unlock()
	defer func() {
		selected.mu.Lock()
		selected.leaseCancel = nil
		selected.mu.Unlock()
	}()
	results := make(chan Result, 16)
	go func() { defer cancel(); selected.read(ctx, conn, results) }()
	write := func(kind websocket.MessageType, data []byte) error {
		deadline, done := context.WithTimeout(ctx, 5*time.Second)
		defer done()
		return conn.Write(deadline, kind, data)
	}
	lastPeer, lastPN := "", ""
	stateJSON := func() []byte {
		snapshot := selected.snapshot()
		if snapshot.Peer != lastPeer {
			lastPeer = snapshot.Peer
			lastPN = resolvePeerPN(ctx, client, lastPeer)
		}
		snapshot.PeerPN = lastPN
		data, _ := json.Marshal(snapshot)
		return data
	}
	state := stateJSON()
	if err := write(websocket.MessageText, state); err != nil {
		return
	}
	for {
		select {
		case <-ctx.Done():
			return
		case <-selected.changed:
			state := stateJSON()
			if write(websocket.MessageText, state) != nil {
				return
			}
		case result := <-results:
			data, _ := json.Marshal(result)
			if write(websocket.MessageText, data) != nil {
				return
			}
		case data := <-selected.playback:
			if write(websocket.MessageBinary, data) != nil {
				return
			}
		}
	}
}

// Lookup is scoped to the existing device store and never queries WhatsApp.
// It runs on the media channel writer, outside the engine's callbacks/lock.
func resolvePeerPN(ctx context.Context, client *whatsmeow.Client, peer string) string {
	jid, err := types.ParseJID(peer)
	if err != nil || jid.User == "" {
		return ""
	}
	jid = jid.ToNonAD()
	if jid.Server == types.DefaultUserServer {
		return jid.String()
	}
	if jid.Server != types.HiddenUserServer || client == nil || client.Store == nil || client.Store.LIDs == nil {
		return ""
	}
	lookup, cancel := context.WithTimeout(ctx, time.Second)
	defer cancel()
	pn, err := client.Store.LIDs.GetPNForLID(lookup, jid)
	if err != nil || pn.User == "" || pn.Server != types.DefaultUserServer {
		return ""
	}
	return pn.ToNonAD().String()
}

func (s *session) read(ctx context.Context, conn *websocket.Conn, results chan Result) {
	for {
		kind, data, err := conn.Read(ctx)
		if err != nil {
			return
		}
		if kind == websocket.MessageBinary {
			if len(data) != PCMBytes {
				return
			}
			s.captureMetric("socket_capture")
			if latest(s.capture, append([]byte(nil), data...)) {
				s.captureMetric("capture_queue_dropped")
			}
			continue
		}
		c, err := decodeCommand(data)
		if err != nil {
			return
		}
		r := request{c, make(chan Result, 1)}
		select {
		case s.commands <- r:
		case <-ctx.Done():
			return
		default:
			return
		}
		select {
		case result := <-r.result:
			select {
			case results <- result:
			case <-ctx.Done():
				return
			}
		case <-ctx.Done():
			return
		}
	}
}
