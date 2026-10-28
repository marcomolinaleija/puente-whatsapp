package whatsapp

import (
    "context"
    "encoding/json"
    "os"
    "path/filepath"
    "testing"
    "time"

    "go.mau.fi/whatsmeow"
    "go.mau.fi/whatsmeow/proto/waCommon"
    "go.mau.fi/whatsmeow/proto/waE2E"
    "go.mau.fi/whatsmeow/proto/waWeb"
    "go.mau.fi/whatsmeow/store/sqlstore"
    "go.mau.fi/whatsmeow/types"
    "google.golang.org/protobuf/proto"
)

func canCallFixtureClient(t *testing.T) *whatsmeow.Client {
    t.Helper()
    ctx := context.Background()
    container, err := sqlstore.New(ctx, "sqlite3", "file:"+filepath.Join(t.TempDir(), "fixture.sqlite3")+"?_foreign_keys=on", nil)
    if err != nil { t.Fatal(err) }
    t.Cleanup(func() { container.Close() })
    device := container.NewDevice()
    owner := types.NewJID("15550009999", types.DefaultUserServer)
    device.ID = &owner
    device.LID = types.NewJID("9999", types.HiddenUserServer)
    // NewDevice does not initialize stores before pairing; use only the fixture LID map.
    // Do not fake a pairing, save identity keys or start a connection.
    device.LIDs = container.LIDMap
    if err := device.LIDs.PutLIDMapping(ctx, types.NewJID("1111", types.HiddenUserServer),
        types.NewJID("15550001111", types.DefaultUserServer)); err != nil { t.Fatal(err) }
    if err := device.LIDs.PutLIDMapping(ctx, device.LID,
        types.NewJID("15550008888", types.DefaultUserServer)); err != nil { t.Fatal(err) }
    return whatsmeow.NewClient(device, nil)
}

func TestCANIncomingCallDirectionAndPeer(t *testing.T) {
    client := canCallFixtureClient(t)
    owner := client.Store.GetJID()
    peer := types.NewJID("15550001111", types.DefaultUserServer)
    lid := types.NewJID("1111", types.HiddenUserServer)
    meta := types.BasicCallMeta{From: owner, CallCreator: lid, CallCreatorAlt: peer,
        CallID: "fixture-incoming", Timestamp: time.Unix(1788179696, 0)}
    if callDirectionFromMeta(client, meta) != callDirectionIncoming { t.Fatal("incoming direction lost") }
    normalized := canCallPeerMeta(client, meta, callDirectionIncoming)
    if normalized.From != peer { t.Fatal("incoming call routed to our own device") }
    kind, payload := newCallEvent(context.Background(), client, callStateAccepted,
        callDirectionFromMeta(client, meta), callKindVoice, 2, "", meta)
    if kind != EventCall || payload == nil { t.Fatal("incoming acceptance dropped") }
    contract := canDecodeCall(t, payload)
    if contract.Direction != callDirectionIncoming || contract.PeerJID != peer.String() ||
        contract.AnsweredAt == "" || contract.EndedAt != "" || contract.DurationSeconds != nil {
        t.Fatal("incoming facts or unresolved duration changed")
    }
    meta.CallCreator, meta.CallCreatorAlt = owner, client.Store.GetLID()
    if callDirectionFromMeta(client, meta) != callDirectionOutgoing { t.Fatal("outgoing regressed") }
    meta.CallCreator, meta.CallCreatorAlt = types.NewJID("15550008888", types.DefaultUserServer), types.JID{}
    if callDirectionFromMeta(client, meta) != callDirectionOutgoing { t.Fatal("own stored alias mistaken for incoming") }
    meta.CallCreator, meta.CallCreatorAlt = types.JID{}, types.JID{}
    if callDirectionFromMeta(client, meta) != callDirectionUnknown { t.Fatal("unknown creator guessed") }
    if callStateFromTerminateReason("accepted_elsewhere") != callStateAccepted {
        t.Fatal("acceptance on phone interpreted as end")
    }
}

func canDecodeCall(t *testing.T, payload *EventPayload) *CallContract {
    t.Helper()
    raw := payload.Call.Actor.LID
    if len(raw) <= len(callContractTransportPrefix) || raw[:len(callContractTransportPrefix)] != callContractTransportPrefix {
        t.Fatal("missing call contract transport")
    }
    var contract CallContract
    if err := json.Unmarshal([]byte(raw[len(callContractTransportPrefix):]), &contract); err != nil { t.Fatal(err) }
    return &contract
}

func TestCANHistoricalCallLogsSurviveDMFilterAndWrappers(t *testing.T) {
    client := canCallFixtureClient(t)
    for _, name := range []string{"incoming", "outgoing", "group", "wrapped"} {
        t.Run(name, func(t *testing.T) {
            chat := "15550001111@s.whatsapp.net"
            if name == "group" { chat = "100-200@g.us" }
            message := &waE2E.Message{CallLogMesssage: &waE2E.CallLogMessage{
                DurationSecs: proto.Int64(73), CallOutcome: waE2E.CallLogMessage_CONNECTED.Enum(), IsVideo: proto.Bool(false),
            }}
            if name == "wrapped" { message = &waE2E.Message{DeviceSentMessage: &waE2E.DeviceSentMessage{Message: message}} }
            info := &waWeb.WebMessageInfo{
                Key: &waCommon.MessageKey{RemoteJID: proto.String(chat), FromMe: proto.Bool(name == "outgoing"),
                    ID: proto.String("incoming-history-fixture"), Participant: proto.String("15550001111@s.whatsapp.net")},
                Message: message, MessageTimestamp: proto.Uint64(1788179696),
            }
            kind, payload := newEventFromHistory(context.Background(), client, info)
            if kind != EventCall || payload == nil { t.Fatal("historical call dropped") }
            contract := canDecodeCall(t, payload)
            expected := callDirectionIncoming
            if name == "outgoing" { expected = callDirectionOutgoing }
            if contract.Direction != expected || contract.Source != "history_sync" ||
                contract.DurationSeconds == nil || *contract.DurationSeconds != 73 || contract.Outcome != "connected" {
                t.Fatal("history call metadata changed")
            }
            if name == "incoming" && os.Getenv("CAN_CALL_FIXTURE") != "" {
                data, err := json.Marshal(contract)
                if err != nil { t.Fatal(err) }
                if err := os.WriteFile(os.Getenv("CAN_CALL_FIXTURE"), data, 0600); err != nil { t.Fatal(err) }
            }
        })
    }
}

func TestCANHistoricalCallInvalidMetadataAndRegularDMRemainUnknown(t *testing.T) {
    client := canCallFixtureClient(t)
    for _, name := range []string{"ordinary", "empty-id", "invalid-chat", "missing-time", "nil"} {
        t.Run(name, func(t *testing.T) {
            info := &waWeb.WebMessageInfo{
                Key: &waCommon.MessageKey{RemoteJID: proto.String("15550001111@s.whatsapp.net"), ID: proto.String("fixture")},
                Message: &waE2E.Message{CallLogMesssage: &waE2E.CallLogMessage{DurationSecs: proto.Int64(19)}},
                MessageTimestamp: proto.Uint64(1788179696),
            }
            switch name {
            case "ordinary": info.Message = &waE2E.Message{Conversation: proto.String("Fixture text")}
            case "empty-id": info.Key.ID = proto.String("")
            case "invalid-chat": info.Key.RemoteJID = proto.String("invalid")
            case "missing-time": info.MessageTimestamp = nil
            case "nil": info = nil
            }
            kind, payload := newEventFromHistory(context.Background(), client, info)
            if kind != EventUnknown || payload != nil { t.Fatal("invalid call or normal DM changed") }
        })
    }
}
