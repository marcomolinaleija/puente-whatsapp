package whatsapp

import (
    "context"
    "testing"
)

func TestPrivateGroupReplyKeepsOriginalRemoteJID(t *testing.T) {
    message := Message{
        Body: "respuesta privada", ReplyID: "group-message-id", ReplyBody: "mensaje original",
        Chat: Chat{JID: "12025550100@s.whatsapp.net", IsGroup: false},
        OriginActor: Actor{
            JID: "12025550100@s.whatsapp.net", LID: "123456789012345@lid",
        },
    }
    message.OriginActor.LID += privateReplyGroupSeparator + "120363000000000000@g.us"
    payload := (&Session{}).getMessagePayload(context.Background(), message)
    contextInfo := payload.GetExtendedTextMessage().GetContextInfo()
    if contextInfo.GetRemoteJID() != "120363000000000000@g.us" {
        t.Fatalf("expected original group, got %q", contextInfo.GetRemoteJID())
    }
    if contextInfo.GetParticipant() != "123456789012345@lid" {
        t.Fatalf("expected original participant, got %q", contextInfo.GetParticipant())
    }
    if contextInfo.GetStanzaID() != message.ReplyID {
        t.Fatalf("expected quoted ID %q, got %q", message.ReplyID, contextInfo.GetStanzaID())
    }
}

func TestDirectReplyDoesNotInventRemoteJID(t *testing.T) {
    message := Message{
        Body: "respuesta directa", ReplyID: "direct-message-id", ReplyBody: "mensaje directo",
        Chat: Chat{JID: "12025550100@s.whatsapp.net", IsGroup: false},
        OriginActor: Actor{JID: "12025550100@s.whatsapp.net"},
    }
    payload := (&Session{}).getMessagePayload(context.Background(), message)
    contextInfo := payload.GetExtendedTextMessage().GetContextInfo()
    if contextInfo.GetRemoteJID() != "" {
        t.Fatalf("direct reply unexpectedly set RemoteJID to %q", contextInfo.GetRemoteJID())
    }
    if contextInfo.GetParticipant() != message.OriginActor.JID {
        t.Fatalf("expected direct participant %q, got %q", message.OriginActor.JID, contextInfo.GetParticipant())
    }
}
