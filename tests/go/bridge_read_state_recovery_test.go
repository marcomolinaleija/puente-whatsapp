package whatsapp

import (
    "context"
    "errors"
    "fmt"
    "testing"

    "go.mau.fi/whatsmeow/appstate"
)

func TestCANReadRecoveryIsBoundedAndPreservesPatch(t *testing.T) {
    receipt := Receipt{Chat: Chat{JID: "100-200@g.us", IsGroup: true},
        OriginActor: Actor{LID: "300@lid"}, MessageIDs: []string{"fixture"}, Timestamp: 1700000000}
    patch, err := canGroupReadPatch(receipt)
    if err != nil { t.Fatal(err) }
    cases := []string{"success", "wrapped-hash", "other-error", "hash-text-only", "recovery-error", "retry-error", "cancelled", "cancel-during-recovery"}
    for _, name := range cases {
        t.Run(name, func(t *testing.T) {
            ctx, cancel := context.WithCancel(context.Background())
            defer cancel()
            if name == "cancelled" { cancel() }
            sends, fetches := 0, 0
            other := errors.New("fixture network failure")
            send := func(ctx context.Context, got appstate.PatchInfo) error {
                sends++
                if got.Type != patch.Type || got.Mutations[0].Value != patch.Mutations[0].Value {
                    t.Fatal("read horizon changed during recovery")
                }
                if name == "success" || (sends == 2 && name != "retry-error") { return nil }
                if name == "other-error" { return other }
                if name == "hash-text-only" { return errors.New("mismatching LTHash") }
                return fmt.Errorf("fixture conflict: %w", appstate.ErrMismatchingLTHash)
            }
            fetch := func(ctx context.Context, collection appstate.WAPatchName, full, onlyNew bool) error {
                fetches++
                if collection != patch.Type || !full || onlyNew { t.Fatal("wrong collection or recovery flags") }
                if name == "recovery-error" { return other }
                if name == "cancel-during-recovery" { cancel() }
                return nil
            }
            err := canSendGroupReadWithRecovery(ctx, patch, send, fetch)
            wantSends, wantFetches := 1, 0
            if name == "wrapped-hash" || name == "retry-error" { wantSends, wantFetches = 2, 1 }
            if name == "recovery-error" || name == "cancel-during-recovery" { wantFetches = 1 }
            if sends != wantSends || fetches != wantFetches { t.Fatalf("unbounded calls: send=%d fetch=%d", sends, fetches) }
            if (err == nil) != (name == "success" || name == "wrapped-hash") { t.Fatalf("wrong result: %v", err) }
            if name == "recovery-error" && (!errors.Is(err, other) || !errors.Is(err, appstate.ErrMismatchingLTHash)) { t.Fatal("failure evidence lost") }
        })
    }
}
