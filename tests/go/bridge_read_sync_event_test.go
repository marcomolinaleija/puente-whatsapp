package whatsapp

import (
	"context"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waSyncAction"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

func readSyncPtr[T any](value T) *T {
	return &value
}

func readSyncClient() *whatsmeow.Client {
	jid := types.NewJID("15550000000", types.DefaultUserServer)
	lid := types.NewJID("123456789", types.HiddenUserServer)
	return whatsmeow.NewClient(&store.Device{ID: &jid, LID: lid}, nil)
}

func markChatAsReadEvent(
	chat types.JID,
	read bool,
	messages ...*waSyncAction.SyncActionMessage,
) *events.MarkChatAsRead {
	return &events.MarkChatAsRead{
		JID:       chat,
		Timestamp: time.Unix(1_784_000_000, 0),
		Action: &waSyncAction.MarkChatAsReadAction{
			Read: &read,
			MessageRange: &waSyncAction.SyncActionMessageRange{
				Messages: messages,
			},
		},
	}
}

func readSyncMessage(id string, timestamp int64) *waSyncAction.SyncActionMessage {
	return &waSyncAction.SyncActionMessage{
		Key:       &waCommon.MessageKey{ID: readSyncPtr(id)},
		Timestamp: readSyncPtr(timestamp),
	}
}

func TestNewMarkChatAsReadEvent(t *testing.T) {
	client := readSyncClient()
	contact := types.NewJID("15551111111", types.DefaultUserServer)

	t.Run("valid read emits own read receipt", func(t *testing.T) {
		kind, payload := newMarkChatAsReadEvent(
			context.Background(),
			client,
			markChatAsReadEvent(contact, true, readSyncMessage("first-id", 10)),
		)
		if kind != EventReceipt || payload == nil {
			t.Fatalf("expected EventReceipt, got kind=%v payload=%v", kind, payload)
		}
		receipt := payload.Receipt
		if receipt.Kind != ReceiptRead {
			t.Fatalf("expected ReceiptRead, got %v", receipt.Kind)
		}
		if len(receipt.MessageIDs) != 1 || receipt.MessageIDs[0] != "first-id" {
			t.Fatalf("unexpected message IDs: %v", receipt.MessageIDs)
		}
		if !receipt.Actor.IsMe || receipt.Actor.JID == "" || receipt.Actor.LID == "" {
			t.Fatalf("expected own actor with JID and LID, got %+v", receipt.Actor)
		}
		if receipt.Chat.JID != contact.String() || receipt.Chat.IsGroup {
			t.Fatalf("unexpected chat: %+v", receipt.Chat)
		}
	})

	t.Run("latest timestamp wins", func(t *testing.T) {
		_, payload := newMarkChatAsReadEvent(
			context.Background(),
			client,
			markChatAsReadEvent(
				contact,
				true,
				readSyncMessage("newest-id", 30),
				readSyncMessage("older-id", 20),
				readSyncMessage("middle-id", 25),
			),
		)
		if payload.Receipt.MessageIDs[0] != "newest-id" {
			t.Fatalf("unexpected selected ID: %v", payload.Receipt.MessageIDs)
		}
	})

	t.Run("unread action is ignored", func(t *testing.T) {
		kind, payload := newMarkChatAsReadEvent(
			context.Background(),
			client,
			markChatAsReadEvent(contact, false, readSyncMessage("ignored-id", 10)),
		)
		if kind != EventUnknown || payload != nil {
			t.Fatalf("expected ignored event, got kind=%v payload=%v", kind, payload)
		}
	})

	t.Run("empty message range is ignored", func(t *testing.T) {
		kind, payload := newMarkChatAsReadEvent(
			context.Background(),
			client,
			markChatAsReadEvent(contact, true, readSyncMessage("", 10)),
		)
		if kind != EventUnknown || payload != nil {
			t.Fatalf("expected ignored event, got kind=%v payload=%v", kind, payload)
		}
	})

	t.Run("group JID is classified as group", func(t *testing.T) {
		group := types.NewJID("120363000000000000", types.GroupServer)
		_, payload := newMarkChatAsReadEvent(
			context.Background(),
			client,
			markChatAsReadEvent(group, true, readSyncMessage("group-id", 10)),
		)
		if payload.Receipt.Chat.JID != group.String() || !payload.Receipt.Chat.IsGroup {
			t.Fatalf("unexpected group chat: %+v", payload.Receipt.Chat)
		}
	})
}
