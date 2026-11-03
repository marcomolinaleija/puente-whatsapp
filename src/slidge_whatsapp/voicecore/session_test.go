package voicecore

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/coder/websocket"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
)

type fixtureLIDStore struct {
	store.LIDStore
	pn       types.JID
	deadline bool
}

func (f *fixtureLIDStore) GetPNForLID(ctx context.Context, lid types.JID) (types.JID, error) {
	_, f.deadline = ctx.Deadline()
	return f.pn, nil
}

func TestPeerRoutingUsesOnlyTheExistingDevicesLocalLIDStore(t *testing.T) {
	local := &fixtureLIDStore{pn: types.NewJID("fixture", types.DefaultUserServer)}
	client := whatsmeow.NewClient(&store.Device{LIDs: local}, nil)
	if pn := resolvePeerPN(context.Background(), client, "opaque@lid"); pn != "fixture@s.whatsapp.net" {
		t.Fatal("LID did not resolve through own device")
	}
	if !local.deadline {
		t.Fatal("local lookup has no deadline")
	}
	if pn := resolvePeerPN(context.Background(), nil, "fixture:4@s.whatsapp.net"); pn != "fixture@s.whatsapp.net" {
		t.Fatal("device suffix escaped into routing")
	}
	for _, peer := range []string{"fixture@g.us", "missing@lid", "bad"} {
		if resolvePeerPN(context.Background(), nil, peer) != "" {
			t.Fatal("unresolved peer was guessed")
		}
	}
	local.pn = types.NewJID("fixture", types.GroupServer)
	if resolvePeerPN(context.Background(), client, "opaque@lid") != "" {
		t.Fatal("group accepted as contact")
	}
}

type fakeEngine struct {
	s       *session
	closed  atomic.Int32
	fed     chan []float32
	signals atomic.Int32
}

func (f *fakeEngine) Command(_ context.Context, c Command) error {
	state := f.s.snapshot()
	if c.Action == "dial" {
		state = State{CallID: "fixture-call", Peer: c.Peer, Direction: "outgoing", State: "active"}
	} else {
		state.State = "ended"
	}
	f.s.update(state)
	return nil
}
func (f *fakeEngine) Signal(context.Context, any, State) { f.signals.Add(1) }
func (f *fakeEngine) PCM(pcm []float32)                  { f.fed <- pcm; f.s.audio(pcm) }
func (f *fakeEngine) Close(context.Context)              { f.closed.Add(1) }

func fixture(t *testing.T) (*session, *fakeEngine) {
	t.Helper()
	var f *fakeEngine
	s := newSession(context.Background(), "fixture-device", func(s *session) engine {
		f = &fakeEngine{s: s, fed: make(chan []float32, 10)}
		return f
	})
	t.Cleanup(func() {
		s.stop()
		select {
		case <-s.done:
		case <-time.After(time.Second):
			t.Error("actor leaked")
		}
	})
	return s, f
}

func TestCommandsAndAudio(t *testing.T) {
	s, f := fixture(t)
	for _, c := range []Command{
		{ID: "a", Action: "dial", Peer: "fixture@g.us"},
		{ID: "a", Action: "dial", Peer: "fixture:1@s.whatsapp.net"},
		{ID: "a", Action: "hangup", CallID: "other"},
		{ID: "a", Action: "inject"},
	} {
		if s.execute(context.Background(), c) == nil {
			t.Fatalf("accepted invalid command: %+v", c)
		}
	}
	r := request{Command{ID: "a", Action: "dial", Peer: "fixture@s.whatsapp.net"}, make(chan Result, 1)}
	s.commands <- r
	if result := <-r.result; result.Error != "" {
		t.Fatal(result)
	}
	if err := s.execute(context.Background(), r.command); err == nil {
		t.Fatal("second call accepted")
	}
	pcm := make([]byte, PCMBytes)
	pcm[0] = 0xff
	pcm[1] = 0x7f
	s.capture <- pcm
	select {
	case samples := <-f.fed:
		if samples[0] < 0.99 {
			t.Fatal("PCM conversion")
		}
	case <-time.After(time.Second):
		t.Fatal("no capture")
	}
	select {
	case audio := <-s.playback:
		if len(audio) != PCMBytes {
			t.Fatal("wrong PCM size")
		}
	case <-time.After(time.Second):
		t.Fatal("no playback")
	}
	if err := s.execute(context.Background(), Command{ID: "b", Action: "hangup", CallID: "fixture-call"}); err != nil {
		t.Fatal(err)
	}
	if s.snapshot().State != "ended" {
		t.Fatal("call did not end")
	}
}

func TestExclusiveLeaseAndCancellation(t *testing.T) {
	s, f := fixture(t)
	if !s.acquire("fixture@example.invalid/one") || s.acquire("fixture@example.invalid/two") {
		t.Fatal("lease isolation")
	}
	s.stop()
	select {
	case <-s.done:
	case <-time.After(time.Second):
		t.Fatal("cancel stalled")
	}
	if f.closed.Load() != 1 || s.acquire("one") {
		t.Fatal("engine or lease survived close")
	}
}

func TestPrivateSocketRefusesFilesLinksAndPublicDirectory(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "native.sock")
	if err := os.Chmod(root, 0700); err != nil {
		t.Fatal(err)
	}
	listener, err := privateListener(path)
	if err != nil {
		t.Fatal(err)
	}
	info, _ := os.Stat(path)
	if info.Mode().Perm() != 0600 {
		t.Fatal("socket is public")
	}
	if _, err = privateListener(path); err == nil {
		t.Fatal("replaced live socket")
	}
	listener.Close()
	if err = os.WriteFile(path, []byte("preserve"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err = privateListener(path); err == nil {
		t.Fatal("replaced ordinary file")
	}
	data, _ := os.ReadFile(path)
	if string(data) != "preserve" {
		t.Fatal("file damaged")
	}
	os.Remove(path)
	if err = os.Symlink(filepath.Join(root, "other"), path); err != nil {
		t.Fatal(err)
	}
	if _, err = privateListener(path); err == nil {
		t.Fatal("followed link")
	}
	os.Remove(path)
	os.Chmod(root, 0755)
	if _, err = privateListener(path); err == nil {
		t.Fatal("accepted public directory")
	}
}

func TestNativeWebsocketScopesDeviceAndMovesSyntheticAudio(t *testing.T) {
	s, _ := fixture(t)
	h := &hub{sessions: map[*whatsmeow.Client]*session{whatsmeow.NewClient(&store.Device{}, nil): s}}
	server := httptest.NewServer(h)
	defer server.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	url := "ws" + strings.TrimPrefix(server.URL, "http") + "/session"
	_, response, err := websocket.Dial(ctx, url, nil)
	if err == nil || response.StatusCode != 403 {
		t.Fatal("missing device authorized")
	}
	response.Body.Close()
	options := &websocket.DialOptions{HTTPHeader: http.Header{"X-Can-Device": []string{"fixture-device"}, "X-Can-Resource": []string{"fixture@example.invalid/one"}}, Subprotocols: []string{"can-voice-1"}}
	options.HTTPHeader.Set("X-CAN-Device", "another-device")
	_, response, err = websocket.Dial(ctx, url, options)
	if err == nil || response.StatusCode != 503 {
		t.Fatal("foreign device received another account session")
	}
	response.Body.Close()
	options.HTTPHeader.Set("X-CAN-Device", "fixture-device")
	conn, _, err := websocket.Dial(ctx, url, options)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.CloseNow()
	_, raw, err := conn.Read(ctx)
	if err != nil {
		t.Fatal(err)
	}
	var state State
	json.Unmarshal(raw, &state)
	if state.State != "idle" {
		t.Fatal("initial state")
	}
	_, response, err = websocket.Dial(ctx, url, options)
	if err == nil || response.StatusCode != 409 {
		t.Fatal("second resource took lease")
	}
	response.Body.Close()
	if err = conn.Write(ctx, websocket.MessageText, []byte(`{"id":"one","action":"dial","peer":"fixture@s.whatsapp.net"}`)); err != nil {
		t.Fatal(err)
	}
	gotResult, gotState := false, false
	for !gotResult || !gotState {
		_, raw, err = conn.Read(ctx)
		if err != nil {
			t.Fatal(err)
		}
		var row map[string]any
		json.Unmarshal(raw, &row)
		gotResult = gotResult || row["type"] == "result"
		gotState = gotState || row["state"] == "active"
	}
	pcm := make([]byte, PCMBytes)
	pcm[4] = 21
	conn.Write(ctx, websocket.MessageBinary, pcm)
	kind, received, err := conn.Read(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if kind != websocket.MessageBinary || string(received) != string(pcm) {
		t.Fatal("audio did not cross IPC")
	}
	conn.CloseNow()
	// The actor can create a fresh engine after lease loss, without relinking WhatsApp.
	deadline := time.Now().Add(time.Second)
	for {
		s.mu.Lock()
		released := s.owner == ""
		s.mu.Unlock()
		if released {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("lease did not release")
		}
		time.Sleep(time.Millisecond)
	}
	if s.snapshot().State != "idle" {
		t.Fatal("stale call survived channel loss")
	}
}
