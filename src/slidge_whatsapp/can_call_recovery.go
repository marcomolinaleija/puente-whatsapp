package whatsapp

import (
    "context"
    "crypto/sha256"
    "encoding/json"
    "fmt"
    "strings"
    "time"

    "go.mau.fi/whatsmeow/appstate"
    waBinary "go.mau.fi/whatsmeow/binary"
    "go.mau.fi/whatsmeow/types/events"
    walog "go.mau.fi/whatsmeow/util/log"
)

type canCallRecoveryEntry struct {
    cancel context.CancelFunc
    expires time.Time
    active bool
}

// Video offers also contain audio. Only explicit media nodes are evidence of kind.
func canCallOfferKind(node *waBinary.Node) string {
    if node == nil { return callKindUnknown }
    audio := false
    for _, child := range node.GetChildren() {
        if child.Tag == "video" { return callKindVideo }
        if child.Tag == "audio" { audio = true }
    }
    if audio { return callKindVoice }
    return callKindUnknown
}

func canCallEnum(value string, allowed ...string) string {
    for _, candidate := range allowed { if value == candidate { return value } }
    return "unknown"
}

func canCallRef(id string) string { return fmt.Sprintf("%x", sha256.Sum256([]byte(id)))[:12] }

// No JID, caller name, native ID, body, raw node, token or arbitrary reason in diagnostics.
func canCallDiagnostic(c *CallContract) string {
    return fmt.Sprintf("CAN_CALL stage=emit ref=%s seq=%d state=%s direction=%s kind=%s source=%s outcome=%s duration_present=%t",
        canCallRef(c.CallID), c.Sequence,
        canCallEnum(c.State, "offered", "accepted", "ended", "missed", "rejected", "failed"),
        canCallEnum(c.Direction, "incoming", "outgoing"),
        canCallEnum(c.Kind, "voice", "video"),
        canCallEnum(c.Source, "app_state", "history_sync", "message"),
        canCallEnum(c.Outcome, "connected", "accepted_elsewhere", "ongoing", "upcoming", "missed", "rejected", "cancelled", "failed", "invalid", "unavailable", "abandoned"), c.DurationSeconds != nil)
}

// Dedicated safe audit stream remains available even when the global logger is WARNING.
// Do not raise the global level: its existing debug events include raw private payloads.
var canCallAuditLogger = walog.Stdout("can-call", "INFO", false)

func (s *Session) canCallLog(format string, args ...any) {
    if s.gateway != nil { canCallAuditLogger.Infof(format, args...) }
}

// These signals can be ignored by the adapter, but must remain visible in the private test.
func (s *Session) canTraceCallSignal(evt any) {
    id, stage, reason := "", "", ""
    switch e := evt.(type) {
    case *events.CallPreAccept: id, stage = e.CallID, "preaccept"
    case *events.CallTransport: id, stage = e.CallID, "transport"
    case *events.CallTerminate:
        id, stage = e.CallID, "terminate"
        reason = canCallEnum(e.Reason, "accepted_elsewhere", "rejected_elsewhere", "timeout", "hangup", "ended", "cancelled")
    case *events.UnknownCallEvent:
        // Unknown raw nodes can contain private metadata; record presence only.
        s.canCallLog("CAN_CALL stage=unknown_signal")
        return
    }
    if id != "" { s.canCallLog("CAN_CALL stage=%s ref=%s reason=%s", stage, canCallRef(id), reason) }
}

func (s *Session) canObserveCall(payload *EventPayload) {
    var c CallContract
    raw := payload.Call.Actor.LID
    if !strings.HasPrefix(raw, callContractTransportPrefix) ||
        json.Unmarshal([]byte(strings.TrimPrefix(raw, callContractTransportPrefix)), &c) != nil || c.CallID == "" { return }
    s.canCallLog("%s", canCallDiagnostic(&c))
    if canCallRecoveryComplete(&c) {
        s.canFinishCallRecovery(c.CallID)
    } else if c.Sequence == 2 && c.State == callStateAccepted && c.Direction == callDirectionIncoming {
        s.canStartCallRecovery(c.CallID)
    }
}

func canCallRecoveryComplete(c *CallContract) bool {
    if c.Sequence < 4 { return false }
    switch c.Outcome {
    case "connected": return c.DurationSeconds != nil
    case "accepted_elsewhere": return c.DurationSeconds != nil && *c.DurationSeconds > 0
    case "missed", "rejected", "cancelled", "failed", "invalid", "unavailable", "abandoned": return true
    default: return false // An ongoing/upcoming/unknown record is not the final record.
    }
}

func (s *Session) canFinishCallRecovery(id string) {
    s.canCallRecoveryMutex.Lock()
    defer s.canCallRecoveryMutex.Unlock()
    if entry := s.canCallRecoveries[id]; entry != nil && entry.active {
        entry.active = false
        entry.cancel()
        s.canCallLog("CAN_CALL stage=recovery_final ref=%s", canCallRef(id))
    }
}

func (s *Session) canCancelCallRecoveries() {
    s.canCallRecoveryMutex.Lock()
    defer s.canCallRecoveryMutex.Unlock()
    for _, entry := range s.canCallRecoveries {
        entry.active = false
        entry.cancel()
    }
}

// One bounded worker per incoming acceptance, no reset on duplicate linked-device signals.
// Authenticated app-state fetches are reads: never mark chats read or alter phone call logs.
func (s *Session) canStartCallRecovery(id string) {
    client := s.currentClient()
    if client == nil || s.ctx == nil || s.ctx.Err() != nil { return }
    s.canCallRecoveryMutex.Lock()
    now := time.Now()
    if s.canCallRecoveries == nil { s.canCallRecoveries = make(map[string]*canCallRecoveryEntry) }
    active := 0
    for key, entry := range s.canCallRecoveries {
        if !entry.active && now.After(entry.expires) { delete(s.canCallRecoveries, key) } else if entry.active { active++ }
    }
    if s.canCallRecoveries[id] != nil { s.canCallRecoveryMutex.Unlock(); return }
    if active >= 4 || len(s.canCallRecoveries) >= 128 {
        s.canCallRecoveryMutex.Unlock()
        s.canCallLog("CAN_CALL stage=recovery_capacity")
        return
    }
    ctx, cancel := context.WithCancel(s.ctx)
    entry := &canCallRecoveryEntry{cancel: cancel, expires: now.Add(35*time.Minute), active: true}
    s.canCallRecoveries[id] = entry
    s.canCallRecoveryMutex.Unlock()
    go func() {
        defer cancel()
        defer func() {
            s.canCallRecoveryMutex.Lock()
            entry.active = false
            s.canCallRecoveryMutex.Unlock()
        }()
        canRunCallRecovery(ctx, []time.Duration{15*time.Second, 60*time.Second, 180*time.Second, 600*time.Second, 1800*time.Second},
            func(ctx context.Context, attempt int) bool {
                if s.currentClient() != client || !client.IsConnected() { return false }
                // First and last attempt recover a lost incremental mutation; middle attempts fetch deltas.
                full := attempt == 0 || attempt == 4
                requestCtx, stop := context.WithTimeout(ctx, 20*time.Second)
                defer stop()
                err := client.FetchAppState(requestCtx, appstate.WAPatchRegular, full, false)
                status := "ok"
                if err != nil { status = "error" } // Raw errors can contain private IDs; do not log them here.
                s.canCallLog("CAN_CALL stage=recovery_fetch ref=%s attempt=%d full=%t status=%s", canCallRef(id), attempt+1, full, status)
                return ctx.Err() == nil
            })
        s.canCallLog("CAN_CALL stage=recovery_stop ref=%s", canCallRef(id))
    }()
}

// Absolute deadlines bound both the number of fetches and worker lifetime. Cancellation is
// checked again after timer delivery, so a record arriving with the timer avoids another fetch.
func canRunCallRecovery(ctx context.Context, deadlines []time.Duration, fetch func(context.Context, int) bool) {
    started := time.Now()
    for attempt, deadline := range deadlines {
        timer := time.NewTimer(time.Until(started.Add(deadline)))
        select {
        case <-ctx.Done(): timer.Stop(); return
        case <-timer.C:
        }
        if ctx.Err() != nil || !fetch(ctx, attempt) { return }
    }
}

