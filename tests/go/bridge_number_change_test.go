package whatsapp

import (
	"context"
	"testing"

	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waWeb"
	"go.mau.fi/whatsmeow/types"
	"google.golang.org/protobuf/proto"
)

func TestCANNumberChangeOwnActorUsesAuthenticatedIdentity(t *testing.T) {
	client := canCallFixtureClient(t)
	ctx := context.Background()
	owner, lid := client.Store.GetJID(), client.Store.GetLID()
	oldPN := types.NewJID("15550008888", types.DefaultUserServer)
	oldLID := types.NewJID("8888", types.HiddenUserServer)
	for _, pair := range [][2]types.JID{{owner, oldLID}, {oldPN, lid}, {owner, lid}} {
		actor := newActor(ctx, client, pair[0], pair[1])
		if !actor.IsMe || actor.JID != owner.String() || actor.LID != lid.String() {
			t.Fatalf("old alias replaced authenticated identity: %+v", actor)
		}
	}
	peer := newActor(ctx, client, types.NewJID("15550001111", types.DefaultUserServer),
		types.NewJID("1111", types.HiddenUserServer))
	if peer.IsMe || peer.LID != "1111@lid" { t.Fatal("remote became local") }
}

func TestCANNumberChangeHistoricalGroupsPreserveRoomAndSender(t *testing.T) {
	client := canCallFixtureClient(t)
	room := "120363000000000001@g.us"
	for _, tc := range []struct{name, participant, keyParticipant string; own, known bool}{
		{"old-own-participant", "15550008888@s.whatsapp.net", "", true, true},
		{"own-with-empty-participant", "", "", true, true},
		{"remote-participant", "15550001111@s.whatsapp.net", "", false, true},
		{"remote-key-fallback", "", "1111@lid", false, true},
		{"unknown-remote", "", "", false, false},
		{"room-is-not-a-sender", room, "", false, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			info := &waWeb.WebMessageInfo{
				Key: &waCommon.MessageKey{RemoteJID: proto.String(room), FromMe: proto.Bool(tc.own),
					ID: proto.String("fixture"), Participant: proto.String(tc.keyParticipant)},
				Participant: proto.String(tc.participant), MessageTimestamp: proto.Uint64(1234),
				Message: &waE2E.Message{Conversation: proto.String("fixture body")},
			}
			kind, payload := newEventFromHistory(context.Background(), client, info)
			if !tc.known {
				if kind != EventUnknown || payload != nil { t.Fatal("fabricated sender") }; return
			}
			if kind != EventMessage || payload == nil { t.Fatal("history disappeared") }
			m := payload.Message
			if m.Chat.JID != room || !m.Chat.IsGroup || !m.IsHistory || m.Actor.IsMe != tc.own {
				t.Fatalf("history crossed identities: %+v", m)
			}
			if tc.own && m.Actor.LID != client.Store.GetLID().String() { t.Fatal("stale self LID") }
		})
	}
}

func TestCANNumberChangeHistoricalReactionsDoNotLoseParent(t *testing.T) {
	client := canCallFixtureClient(t)
	info := &waWeb.WebMessageInfo{
		Key: &waCommon.MessageKey{RemoteJID: proto.String("120363000000000001@g.us"),
			FromMe: proto.Bool(true), ID: proto.String("fixture-parent")},
		Message: &waE2E.Message{Conversation: proto.String("parent survives")},
		Reactions: []*waWeb.Reaction{
			{Key: &waCommon.MessageKey{FromMe: proto.Bool(true)}, Text: proto.String("👍")},
			{Key: &waCommon.MessageKey{}, Text: proto.String("👎")},
			{Key: &waCommon.MessageKey{Participant: proto.String("1111@lid")}, Text: proto.String("👍")},
		},
	}
	kind, payload := newEventFromHistory(context.Background(), client, info)
	if kind != EventMessage || payload == nil || payload.Message.Body != "parent survives" { t.Fatal("lost parent") }
	reactions := payload.Message.Reactions
	if len(reactions) != 2 || !reactions[0].Actor.IsMe || reactions[1].Actor.LID != "1111@lid" {
		t.Fatalf("reaction attribution failed: %+v", reactions)
	}
}
