package whatsapp

import (
	"context"
	"testing"

	"go.mau.fi/whatsmeow/proto/waHistorySync"
)

func historyConversation(id string, unread uint32, marked bool, ts uint64) *waHistorySync.Conversation {
	return &waHistorySync.Conversation{
		ID:                    readSyncPtr(id),
		UnreadCount:           readSyncPtr(unread),
		MarkedAsUnread:        readSyncPtr(marked),
		ConversationTimestamp: readSyncPtr(ts),
	}
}

func TestCANHistoryUnreadSummaryCarriesPhoneState(t *testing.T) {
	client := readSyncClient()
	for _, tc := range []struct {
		id     string
		unread uint32
		marked bool
		group  bool
		want   string
	}{
		{"15550001111@s.whatsapp.net", 3, false, false, "can-unread:1:3:0"},
		{"15550001111@s.whatsapp.net", 0, true, false, "can-unread:1:0:1"},
		{"120363000000000001@g.us", 0, false, true, "can-unread:1:0:0"},
	} {
		kind, payload := canHistoryUnreadSummary(context.Background(), client,
			historyConversation(tc.id, tc.unread, tc.marked, 1700000000))
		if kind != EventReceipt || payload == nil {
			t.Fatalf("%s: summary not produced", tc.id)
		}
		r := payload.Receipt
		if r.Kind != ReceiptCANUnreadSummary || len(r.MessageIDs) != 1 || r.MessageIDs[0] != tc.want {
			t.Fatalf("%s: unexpected summary %+v", tc.id, r)
		}
		if r.Chat.IsGroup != tc.group || r.Chat.JID == "" || !r.Actor.IsMe || r.Timestamp != 1700000000 {
			t.Fatalf("%s: summary lost chat or actor %+v", tc.id, r)
		}
		// A summary is never a read or delivery receipt.
		if r.Kind == ReceiptRead || r.Kind == ReceiptDelivered {
			t.Fatal("summary must not acknowledge messages")
		}
	}
}

func TestCANHistoryUnreadSummarySkipsUnopenableChats(t *testing.T) {
	client := readSyncClient()
	for _, id := range []string{"", "status@broadcast", "123@newsletter", "not a jid"} {
		kind, payload := canHistoryUnreadSummary(context.Background(), client,
			historyConversation(id, 2, false, 1))
		if kind != EventUnknown || payload != nil {
			t.Fatalf("%q must not produce a summary", id)
		}
	}
	if kind, _ := canHistoryUnreadSummary(context.Background(), client, nil); kind != EventUnknown {
		t.Fatal("nil conversation must not produce a summary")
	}
}

func TestCANHistoryUnreadStateOnlyFromCurrentSyncs(t *testing.T) {
	cases := map[waHistorySync.HistorySync_HistorySyncType]bool{
		waHistorySync.HistorySync_INITIAL_BOOTSTRAP: true,
		waHistorySync.HistorySync_RECENT:            true,
		waHistorySync.HistorySync_ON_DEMAND:         false,
		waHistorySync.HistorySync_PUSH_NAME:         false,
	}
	for kind, want := range cases {
		if canHistoryCarriesUnreadState(kind) != want {
			t.Fatalf("%v: unexpected unread-state policy", kind)
		}
	}
}
