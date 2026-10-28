package whatsapp

import (
    "context"
    "testing"
    "time"
    "go.mau.fi/whatsmeow"
    "go.mau.fi/whatsmeow/store"
    "go.mau.fi/whatsmeow/types"
    "go.mau.fi/whatsmeow/types/events"
)

func TestCANHistoryReceipt(t *testing.T) {
    for _, c := range []struct { delivered, read int64; kind ReceiptKind; timestamp int64; known bool }{
        {0, 0, ReceiptDelivered, 0, false},
        {-1, -1, ReceiptDelivered, 0, false},
        {100, 0, ReceiptDelivered, 100, true},
        {0, 200, ReceiptRead, 200, true},
        {100, 200, ReceiptRead, 200, true},
    } {
        kind, timestamp, known := canHistoryReceipt(c.delivered, c.read)
        if kind != c.kind || timestamp != c.timestamp || known != c.known {
            t.Fatalf("per-person classification failed: %+v", c)
        }
    }
}

func TestCANUnsupportedReceiptNeverDelivered(t *testing.T) {
    for _, receiptType := range []types.ReceiptType{
        types.ReceiptTypeRetry, types.ReceiptTypeReadSelf,
        types.ReceiptTypeSender, types.ReceiptType("unknown-fixture"),
    } {
        kind, payload := newReceiptEvent(context.Background(), nil, &events.Receipt{Type: receiptType})
        if kind != EventUnknown || payload != nil { t.Fatal("unsupported receipt became delivery") }
    }
}

func TestCANGroupReceiptKeepsParticipantAndTime(t *testing.T) {
    own := types.NewJID("15550000000", types.DefaultUserServer)
    ownLID := types.NewJID("100", types.HiddenUserServer)
    client := whatsmeow.NewClient(&store.Device{ID: &own, LID: ownLID}, nil)
    for _, receiptType := range []types.ReceiptType{types.ReceiptTypeDelivered, types.ReceiptTypeRead} {
        evt := &events.Receipt{
            MessageSource: types.MessageSource{
                Chat: types.NewJID("100-200", types.GroupServer), IsGroup: true,
                Sender: types.NewJID("15551111111", types.DefaultUserServer),
                SenderAlt: types.NewJID("200", types.HiddenUserServer),
            },
            MessageIDs: []types.MessageID{"fixture-msg"}, Timestamp: time.Unix(1234,0), Type: receiptType,
        }
        kind, payload := newReceiptEvent(context.Background(), client, evt)
        if kind != EventReceipt || payload == nil { t.Fatal("receipt was lost") }
        receipt := payload.Receipt
        if receipt.Actor.LID != "200@lid" || receipt.Actor.IsMe || !receipt.Chat.IsGroup || receipt.Timestamp != 1234 {
            t.Fatalf("group receipt lost identity: %+v", receipt)
        }
    }
}
