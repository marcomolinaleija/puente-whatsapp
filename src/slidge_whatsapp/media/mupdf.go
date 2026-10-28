//go:build mupdf

package media

import (
	// Standard library.
	"context"
	"fmt"

	// Third-party packages.
	"github.com/gen2brain/go-fitz"
)

// InternalConvertDocument converts the given data buffer, which is assumed to be a valid PDF document,
// into the target spec with MuPDF.
func internalConvertDocument(_ context.Context, data []byte, spec *Spec) ([]byte, error) {
	doc, err := fitz.NewFromMemory(data)
	if err != nil {
		return nil, fmt.Errorf("failed reading document: %s", err)
	}

	defer doc.Close()

	var buf []byte
	if n := doc.NumPage(); n <= spec.DocumentPage {
		return nil, fmt.Errorf("cannot read page %d in document with %d pages", spec.DocumentPage+1, n+1)
	} else if img, err := doc.Image(spec.DocumentPage); err != nil {
		if buf, err = processImage(img, spec); err != nil {
			return nil, err
		}
	}

	return buf, nil
}

// InternalGetDocumentSpec fetches as much metadata as possible from the given data buffer with MuPDF.
func internalGetDocumentSpec(_ context.Context, data []byte) (*Spec, error) {
	doc, err := fitz.NewFromMemory(data)
	if err != nil {
		return nil, fmt.Errorf("failed to read document: %s", err)
	}

	defer doc.Close()
	return &Spec{
		DocumentPage: doc.NumPage(),
	}, nil
}
