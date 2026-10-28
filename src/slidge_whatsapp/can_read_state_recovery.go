package whatsapp

import (
    "context"
    "errors"
    "fmt"

    "go.mau.fi/whatsmeow/appstate"
)

// Recover only a stale hash for this collection. Never skip integrity checks,
// reset a linked device, or change the original bounded read horizon.
func canSendGroupReadWithRecovery(
    ctx context.Context, patch appstate.PatchInfo,
    send func(context.Context, appstate.PatchInfo) error,
    fetch func(context.Context, appstate.WAPatchName, bool, bool) error,
    primary ...func(context.Context, appstate.WAPatchName) error,
) error {
    err := send(ctx, patch)
    if !errors.Is(err, appstate.ErrMismatchingLTHash) || ctx.Err() != nil {
        return err
    }
    if recoveryErr := fetch(ctx, patch.Type, true, false); recoveryErr != nil {
        // CAN request primary snapshot v1. A queued request is not an ack.
        if errors.Is(recoveryErr, appstate.ErrMismatchingLTHash) && ctx.Err() == nil && len(primary) == 1 {
            if peerErr := primary[0](ctx, patch.Type); peerErr != nil {
                return fmt.Errorf("group read primary recovery failed: %w", errors.Join(err, recoveryErr, peerErr))
            }
        }
        return fmt.Errorf("group read app-state recovery failed: %w", errors.Join(err, recoveryErr))
    }
    if err = ctx.Err(); err != nil {
        return err
    }
    return send(ctx, patch)
}
