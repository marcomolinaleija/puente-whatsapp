//go:build !mupdf

package media

import (
	// Standard library.
	"context"
	"errors"
)

// InternalGetDocumentSpec is a stub implementation, as called by [convertDocument].
func internalConvertDocument(_ context.Context, _ []byte, _ *Spec) ([]byte, error) {
	return nil, errors.New("document support not enabled in this build")
}

// InternalGetDocumentSpec is a stub implementation, as called by [getDocumentSpec].
func internalGetDocumentSpec(_ context.Context, _ []byte) (*Spec, error) {
	return nil, errors.New("document support not enabled in this build")
}
