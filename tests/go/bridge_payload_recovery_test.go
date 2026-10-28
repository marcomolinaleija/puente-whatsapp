package whatsapp

import (
    "context"
    "errors"
    "fmt"
    "testing"
    "sync"

    "go.mau.fi/whatsmeow"
    "go.mau.fi/whatsmeow/appstate"
    "go.mau.fi/whatsmeow/proto/waE2E"
    "go.mau.fi/whatsmeow/store"
)

func TestCANPayloadComposition(t *testing.T) {
    locations := []Location{{}, {Latitude: 20, Longitude: 30, Accuracy: 7},
        {Latitude: -20, Longitude: -30, Accuracy: 7}, {Latitude: 0, Longitude: -30},
        {Latitude: -20, Longitude: 0}}
    for reply := 0; reply < 3; reply++ {
        for flags := 0; flags < 4; flags++ {
            for index, location := range locations {
                t.Run(fmt.Sprintf("reply%d_flags%d_location%d", reply, flags, index), func(t *testing.T) {
                    defer func() { if p := recover(); p != nil { t.Errorf("payload panic: %v", p) } }()
                    message := Message{Body: "fictional body", Location: location}
                    if reply > 0 { message.ReplyID = "fixture-reply"; message.ReplyBody = "fictional quote" }
                    if reply == 1 { message.OriginActor.JID = "12025550100@s.whatsapp.net" }
                    if flags & 1 != 0 { message.Preview = Preview{URL: "https://example.invalid", Title: "Fixture"} }
                    if flags & 2 != 0 { message.MentionJIDs = []string{"12025550101@s.whatsapp.net"} }
                    payload := (&Session{}).getMessagePayload(context.Background(), message)
                    var info *waE2E.ContextInfo
                    if index > 0 {
                        loc := payload.GetLocationMessage()
                        if loc == nil || loc.GetDegreesLatitude() != location.Latitude || loc.GetDegreesLongitude() != location.Longitude || loc.GetAccuracyInMeters() != uint32(location.Accuracy) {
                            t.Fatal("location lost or changed")
                        }
                        if payload.ExtendedTextMessage != nil || payload.Conversation != nil { t.Fatal("multiple content types") }
                        info = loc.GetContextInfo()
                    } else {
                        if payload.GetConversation() != message.Body && payload.GetExtendedTextMessage().GetText() != message.Body { t.Fatal("body lost") }
                        if flags & 1 != 0 && payload.GetExtendedTextMessage().GetMatchedText() != message.Preview.URL { t.Fatal("preview lost") }
                        info = payload.GetExtendedTextMessage().GetContextInfo()
                    }
                    if reply == 1 && (info.GetStanzaID() != message.ReplyID || info.GetParticipant() != message.OriginActor.JID || info.GetQuotedMessage().GetConversation() != message.ReplyBody) { t.Fatal("reply context changed") }
                    if reply == 2 && info.GetParticipant() != "" { t.Fatal("invented reply participant") }
                    if flags & 2 != 0 && (len(info.GetMentionedJID()) != 1 || info.GetMentionedJID()[0] != message.MentionJIDs[0]) { t.Fatal("mention lost") }
                })
            }
        }
    }
}

func TestCANRosterSnapshotSurvivesConcurrentCleanup(t *testing.T) {
    session := &Session{ctx: context.Background()}
    client := &whatsmeow.Client{Store: &store.Device{}}
    var workers sync.WaitGroup
    workers.Add(2)
    go func() {
        defer workers.Done()
        for i := 0; i < 200; i++ {
            session.clientMutex.Lock()
            if i % 2 == 0 { session.client = client } else { session.client = nil }
            session.clientMutex.Unlock()
        }
    }()
    go func() {
        defer workers.Done()
        for i := 0; i < 200; i++ {
            if _, err := session.GetContacts(false); err == nil { t.Error("unauthenticated contacts accepted") }
            if _, err := session.GetGroups(); err == nil { t.Error("unauthenticated groups accepted") }
            if err := session.SubscribeToPresences(); err != nil { t.Error(err) }
        }
    }()
    workers.Wait()
}

func TestCANLocationKeepsGroupAndPrivateReplyContext(t *testing.T) {
    for _, private := range []bool{false, true} {
        t.Run(fmt.Sprintf("private=%t", private), func(t *testing.T) {
            message := Message{Body: "fictional location", ReplyID: "fixture-reply",
                Location: Location{Latitude: 10, Longitude: -20}, Chat: Chat{IsGroup: !private},
                OriginActor: Actor{LID: "10001@lid"}}
            if private { message.OriginActor.LID += privateReplyGroupSeparator + "100-200@g.us" }
            info := (&Session{}).getMessagePayload(context.Background(), message).GetLocationMessage().GetContextInfo()
            if info.GetParticipant() != "10001@lid" || info.GetStanzaID() != message.ReplyID { t.Fatal("group reply lost") }
            if private && info.GetRemoteJID() != "100-200@g.us" { t.Fatal("private reply lost original room") }
        })
    }
}

func TestCANReadSnapshotRequestsPrimaryOnlyForVerifiedHashFailure(t *testing.T) {
    hashErr := fmt.Errorf("fixture snapshot: %w", appstate.ErrMismatchingLTHash)
    other := errors.New("fixture network failure")
    for _, name := range []string{"snapshot-hash", "request-failed", "network-error", "hash-text-only", "cancelled"} {
        t.Run(name, func(t *testing.T) {
            ctx, cancel := context.WithCancel(context.Background()); defer cancel()
            sends, fetches, requests := 0, 0, 0
            send := func(context.Context, appstate.PatchInfo) error { sends++; return hashErr }
            fetch := func(context.Context, appstate.WAPatchName, bool, bool) error {
                fetches++
                if name == "cancelled" { cancel() }
                if name == "network-error" { return other }
                if name == "hash-text-only" { return errors.New("mismatching LTHash") }
                return hashErr
            }
            request := func(_ context.Context, name appstate.WAPatchName) error {
                requests++
                msg := whatsmeow.BuildAppStateRecoveryRequest(name).GetProtocolMessage()
                op := msg.GetPeerDataOperationRequestMessage()
                if msg.GetType() != waE2E.ProtocolMessage_PEER_DATA_OPERATION_REQUEST_MESSAGE || op.GetSyncdCollectionFatalRecoveryRequest().GetCollectionName() != string(appstate.WAPatchRegularLow) { t.Fatal("wrong primary recovery request") }
                return other
            }
            if name != "request-failed" {
                original := request
                request = func(ctx context.Context, name appstate.WAPatchName) error { original(ctx, name); return nil }
            }
            err := canSendGroupReadWithRecovery(ctx, appstate.PatchInfo{Type: appstate.WAPatchRegularLow}, send, fetch, request)
            want := 0
            if name == "snapshot-hash" || name == "request-failed" { want = 1 }
            if sends != 1 || fetches != 1 || requests != want { t.Fatalf("unbounded/wrong calls %d/%d/%d", sends, fetches, requests) }
            if err == nil || !errors.Is(err, appstate.ErrMismatchingLTHash) { t.Fatal("queued recovery falsely acknowledged read or lost error") }
            if name == "request-failed" && !errors.Is(err, other) { t.Fatal("primary request error lost") }
        })
    }
}
