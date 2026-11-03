package call

import (
	"context"
	"codeberg.org/slidge/slidge-whatsapp/slidge_whatsapp/voicecore/internal/voip/core"
)

// Dispose permanently retires this engine. It never disconnects WhatsMeow.
func (m *CallManager) Dispose(ctx context.Context) {
	m.mu.Lock()
	m.canDisposed = true
	m.mu.Unlock()
	m.canCancel()
	_ = m.EndCall(ctx, core.EndCallReasonCancelled)
	m.cleanupMedia()
}
