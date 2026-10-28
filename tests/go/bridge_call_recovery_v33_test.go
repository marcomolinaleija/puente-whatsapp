package whatsapp

import (
    "context"
    "strings"
    "sync"
    "testing"
    "time"

    waBinary "go.mau.fi/whatsmeow/binary"
)

func TestCANCallOfferMediaAndPrivateDiagnostic(t *testing.T) {
    for _, fixture := range []struct{tags []string; want string}{
        {nil, callKindUnknown}, {[]string{"audio"}, callKindVoice},
        {[]string{"audio", "video"}, callKindVideo}, {[]string{"video", "audio"}, callKindVideo},
        {[]string{"enc", "transport"}, callKindUnknown},
    } {
        nodes := []waBinary.Node{}
        for _, tag := range fixture.tags { nodes = append(nodes, waBinary.Node{Tag: tag}) }
        if got := canCallOfferKind(&waBinary.Node{Content: nodes}); got != fixture.want { t.Fatalf("kind: %s", got) }
    }
    if canCallOfferKind(nil) != callKindUnknown { t.Fatal("missing offer guessed") }
    private := "private-caller@example.invalid"
    c := &CallContract{CallID: private, PeerJID: private, State: private, Kind: private, Source: private, Direction: private, TerminalReason: private}
    text := canCallDiagnostic(c)
    if strings.Contains(text, private) || strings.Contains(text, "@") || !strings.Contains(text, "duration_present=false") {
        t.Fatal("diagnostic leaked identity or fabricated duration")
    }
}

func TestCANCallRecoveryBoundsCancellationAndDisconnectedStop(t *testing.T) {
    duration := int64(0)
    for _, fixture := range []struct{outcome string; duration *int64; final bool}{
        {"ongoing", &duration, false}, {"upcoming", nil, false}, {"", nil, false},
        {"accepted_elsewhere", nil, false}, {"accepted_elsewhere", &duration, false},
        {"connected", nil, false}, {"connected", &duration, true}, {"missed", nil, true},
    } {
        if canCallRecoveryComplete(&CallContract{Sequence: 4, Outcome: fixture.outcome, DurationSeconds: fixture.duration}) != fixture.final {
            t.Fatal("incomplete record stopped recovery or final record was ignored")
        }
    }
    count := 0
    canRunCallRecovery(context.Background(), []time.Duration{0, 0, 0, 0, 0}, func(_ context.Context, attempt int) bool {
        if attempt != count { t.Fatal("attempt order changed") }; count++; return true
    })
    if count != 5 { t.Fatal("fetch bound changed") }
    ctx, cancel := context.WithCancel(context.Background())
    cancel()
    canRunCallRecovery(ctx, []time.Duration{0}, func(context.Context, int) bool { t.Fatal("cancelled worker fetched"); return true })
    ctx, cancel = context.WithCancel(context.Background())
    count = 0
    canRunCallRecovery(ctx, []time.Duration{0, time.Hour}, func(context.Context, int) bool { count++; cancel(); return true })
    if count != 1 { t.Fatal("final record did not stop recovery") }
    count = 0
    canRunCallRecovery(context.Background(), []time.Duration{0, 0}, func(context.Context, int) bool { count++; return false })
    if count != 1 { t.Fatal("disconnected client retried") }
}

func TestCANCallRecoveryDuplicateCapacityFinalAndSessionIsolation(t *testing.T) {
    ctx, cancel := context.WithCancel(context.Background())
    defer cancel()
    s := &Session{ctx: ctx, client: canCallFixtureClient(t)}
    var wg sync.WaitGroup
    for i := 0; i < 20; i++ { wg.Add(1); go func() { defer wg.Done(); s.canStartCallRecovery("same") }() }
    wg.Wait()
    s.canCallRecoveryMutex.Lock()
    if len(s.canCallRecoveries) != 1 { t.Fatal("duplicate acceptance spawned workers") }
    first := s.canCallRecoveries["same"]
    s.canCallRecoveryMutex.Unlock()
    for _, id := range []string{"two", "three", "four", "five"} { s.canStartCallRecovery(id) }
    s.canCallRecoveryMutex.Lock()
    if len(s.canCallRecoveries) != 4 { t.Fatal("pending capacity not bounded") }
    s.canCallRecoveryMutex.Unlock()
    s.canFinishCallRecovery("same")
    s.canStartCallRecovery("same")
    s.canCallRecoveryMutex.Lock()
    if first.active || s.canCallRecoveries["same"] != first { t.Fatal("late acceptance restarted final call") }
    s.canCallRecoveryMutex.Unlock()
    other := &Session{ctx: ctx, client: s.client}
    other.canStartCallRecovery("same")
    other.canCallRecoveryMutex.Lock()
    if len(other.canCallRecoveries) != 1 || !other.canCallRecoveries["same"].active { t.Fatal("session isolation lost") }
    other.canCallRecoveryMutex.Unlock()
    cancel()
    // Workers must stop without reaching the first production deadline; race check also covers cleanup.
    until := time.Now().Add(time.Second)
    for {
        active := 0
        for _, session := range []*Session{s, other} {
            session.canCallRecoveryMutex.Lock()
            for _, entry := range session.canCallRecoveries { if entry.active { active++ } }
            session.canCallRecoveryMutex.Unlock()
        }
        if active == 0 { break }
        if time.Now().After(until) { t.Fatal("session cancellation left workers alive") }
        time.Sleep(time.Millisecond)
    }
}
