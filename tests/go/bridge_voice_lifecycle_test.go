package whatsapp

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"testing"
	"time"

	"codeberg.org/slidge/slidge-whatsapp/slidge_whatsapp/voicecore"

	"github.com/coder/websocket"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

// Exercise the patched v35 Session methods and rebuilt native package. No client
// connects to WhatsApp; the sole listener is a private temporary Unix socket.
func TestCANVoiceExistingSessionLifecycle(t *testing.T) {
	directory := t.TempDir()
	if err := os.Chmod(directory, 0700); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(directory, "native.sock")
	t.Setenv("CAN_VOICE_SOCKET", path)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	jid := types.NewJID("fixture", types.DefaultUserServer)
	client := whatsmeow.NewClient(&store.Device{ID: &jid}, nil)
	s := &Session{ctx: ctx}
	s.setClient(client)
	defer s.clearClient(client)
	if s.currentClient() != client || client.IsConnected() {
		t.Fatal("voice changed shared client lifecycle")
	}
	transport := &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "unix", path)
	}}
	defer transport.CloseIdleConnections()
	options := &websocket.DialOptions{HTTPClient: &http.Client{Transport: transport},
		Subprotocols: []string{"can-voice-1"},
		HTTPHeader: http.Header{"X-Can-Device": []string{jid.String()},
			"X-Can-Resource": []string{"fixture@example.invalid/windows"}}}
	conn, _, err := websocket.Dial(ctx, "ws://localhost/session", options)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.CloseNow()
	_, raw, err := conn.Read(ctx)
	if err != nil {
		t.Fatal(err)
	}
	var initial map[string]any
	if json.Unmarshal(raw, &initial) != nil || initial["state"] != "idle" {
		t.Fatal("missing native idle state")
	}
	conn.Write(ctx, websocket.MessageText, []byte(`{"id":"fixture-command","action":"hangup","call_id":"foreign-call"}`))
	_, raw, err = conn.Read(ctx)
	if err != nil {
		t.Fatal(err)
	}
	var result map[string]any
	if json.Unmarshal(raw, &result) != nil || result["error"] != "item-not-found" {
		t.Fatal("foreign call command was not rejected")
	}
	// Clearing an obsolete client cannot stop the current session.
	s.clearClient(whatsmeow.NewClient(&store.Device{}, nil))
	if s.currentClient() != client {
		t.Fatal("obsolete cleanup cleared new client")
	}
	voicecore.Dispatch(client, &events.Disconnected{})
	if _, _, err = conn.Read(ctx); err == nil {
		t.Fatal("voice channel survived WhatsMeow disconnect")
	}
	if s.currentClient() != client {
		t.Fatal("voice took ownership of the message connection")
	}
	s.clearClient(client)
	if s.currentClient() != nil {
		t.Fatal("native client was not cleared")
	}
	if client.IsConnected() {
		t.Fatal("test connected an account")
	}
}
