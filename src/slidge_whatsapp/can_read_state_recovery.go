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
) error {
    err := send(ctx, patch)
    if !errors.Is(err, appstate.ErrMismatchingLTHash) || ctx.Err() != nil {
        return err
    }
    if recoveryErr := fetch(ctx, patch.Type, true, false); recoveryErr != nil {
        return fmt.Errorf("group read app-state recovery failed: %w", errors.Join(err, recoveryErr))
    }
    if err = ctx.Err(); err != nil {
        return err
    }
    return send(ctx, patch)
}
