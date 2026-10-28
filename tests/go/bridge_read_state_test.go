package whatsapp

import (
    "testing"
    "go.mau.fi/whatsmeow/types"
)

func TestCANGroupReadPatchUsesOriginalHorizon(t *testing.T) {
    receipt := Receipt{
        Chat: Chat{JID: "100-200@g.us", IsGroup: true},
        OriginActor: Actor{LID: "300@lid"}, MessageIDs: []string{"fixture-native"},
        Timestamp: 1700000000,
    }
    patch, err := canGroupReadPatch(receipt)
    if err != nil { t.Fatal(err) }
    action := patch.Mutations[0].Value.GetMarkChatAsReadAction()
    r := action.GetMessageRange()
    if !action.GetRead() || r.GetLastMessageTimestamp() != receipt.Timestamp || len(r.Messages) != 1 {
        t.Fatal("missing bounded read horizon")
    }
    m := r.Messages[0]
    k := m.GetKey()
    if m.GetTimestamp() != receipt.Timestamp || k.GetID() != "fixture-native" || k.GetFromMe() ||
        k.GetRemoteJID() != receipt.Chat.JID || k.GetParticipant() != "300@lid" {
        t.Fatal("read key or timestamp changed")
    }
}

func TestCANGroupReadPatchRejectsUnresolvedHorizons(t *testing.T) {
    valid := Receipt{Chat: Chat{JID: "100-200@g.us", IsGroup: true},
        OriginActor: Actor{LID: "300@lid"}, MessageIDs: []string{"fixture"}, Timestamp: 1700000000}
    for _, name := range []string{"timestamp", "empty", "multi", "direct", "sender", "own", "wrong-chat"} {
        t.Run(name, func(t *testing.T) {
            r := valid
            switch name {
            case "timestamp": r.Timestamp = 0
            case "empty": r.MessageIDs = nil
            case "multi": r.MessageIDs = []string{"a", "b"}
            case "direct": r.Chat.IsGroup = false
            case "sender": r.OriginActor.LID = "300@" + types.DefaultUserServer
            case "own": r.OriginActor.IsMe = true
            case "wrong-chat": r.Chat.JID = "300@lid"
            }
            if _, err := canGroupReadPatch(r); err == nil { t.Fatal("accepted invalid horizon") }
        })
    }
}

func TestCANGroupReadRequiresAuthenticatedSession(t *testing.T) {
    s := &Session{}
    if s.MarkChatRead(Receipt{}) == nil { t.Fatal("accepted unauthenticated session") }
}
