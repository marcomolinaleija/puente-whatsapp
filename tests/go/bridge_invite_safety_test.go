package whatsapp

import (
	"context"
	"testing"

	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types/events"
)

func TestCANInviteLinkIsOnlyMessageData(t *testing.T) {
	for _, link := range []string{
		"https://chat.whatsapp.com/FAKEINVITATIONCODE0000",
		"https://chat.whatsapp.com/FAKEINVITATIONCODE0000?mode=invite",
		"https://example.invalid/ordinary-link",
	} {
		t.Run(link, func(t *testing.T) {
			body := "Invitation: " + link
			title := "Example group"
			raw := &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{
				Text: &body, MatchedText: &link, Title: &title,
			}}
			// A nil client makes any attempt to join fail rather than contacting WhatsApp.
			kind, payload := newMessageEvent(context.Background(), nil, &events.Message{Message: raw})
			if kind != EventMessage || payload == nil {
				t.Fatal("link message was discarded")
			}
			msg := payload.Message
			if msg.Body != body || msg.Preview.URL != link || msg.Preview.Title != title {
				t.Fatal("message text or preview was not preserved")
			}
			if msg.GroupInvite.JID != "" {
				t.Fatal("receiving a link must not join a group")
			}
		})
	}
}
