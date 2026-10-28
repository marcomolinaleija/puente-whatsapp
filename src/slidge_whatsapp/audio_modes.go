package whatsapp

import (
	"context"
	"fmt"
	"mime"
	"strings"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
)

const canAudioModeMIME = "application/x-can-audio-mode"
const maxCANAudioAttachmentBytes = 64 * 1024 * 1024

type canAudioUploader interface {
	Upload(context.Context, []byte, whatsmeow.MediaType) (whatsmeow.UploadResponse, error)
}

// Handle explicit intent before convertAttachment can change the bytes or classify PTT.
// This envelope never leaves the Python/Go boundary: WhatsApp gets the real MIME.
func uploadCANAudioAttachment(ctx context.Context, client canAudioUploader, attach *Attachment) (bool, *waE2E.Message, error) {
	if !strings.HasPrefix(attach.MIME, canAudioModeMIME) {
		return false, nil, nil
	}
	parts := strings.SplitN(attach.MIME, ";", 3)
	if len(parts) != 3 || parts[0] != canAudioModeMIME {
		return true, nil, fmt.Errorf("invalid CAN audio envelope")
	}
	mode, originalMIME := parts[1], parts[2]
	base, _, err := mime.ParseMediaType(originalMIME)
	if err != nil || !strings.HasPrefix(base, "audio/") ||
		(mode != "audio" && mode != "document") || (mode == "audio" && base != "audio/mpeg") ||
		attach.ViewOnce || len(attach.Data) == 0 || len(attach.Data) > maxCANAudioAttachmentBytes || strings.TrimSpace(attach.Filename) == "" {
		return true, nil, fmt.Errorf("invalid CAN audio attachment")
	}
	mediaType := whatsmeow.MediaDocument
	if mode == "audio" {
		mediaType = whatsmeow.MediaAudio
	}
	upload, err := client.Upload(ctx, attach.Data, mediaType)
	if err != nil {
		return true, nil, err
	}
	length := uint64(len(attach.Data))
	if mode == "audio" {
		return true, &waE2E.Message{AudioMessage: &waE2E.AudioMessage{
			URL: ptrTo(upload.URL), DirectPath: ptrTo(upload.DirectPath), MediaKey: upload.MediaKey,
			FileEncSHA256: upload.FileEncSHA256, FileSHA256: upload.FileSHA256,
			FileLength: ptrTo(length), Mimetype: ptrTo(originalMIME), PTT: ptrTo(false),
		}}, nil
	}
	return true, &waE2E.Message{DocumentMessage: &waE2E.DocumentMessage{
		URL: ptrTo(upload.URL), DirectPath: ptrTo(upload.DirectPath), MediaKey: upload.MediaKey,
		FileEncSHA256: upload.FileEncSHA256, FileSHA256: upload.FileSHA256,
		FileLength: ptrTo(length), Mimetype: ptrTo(originalMIME), FileName: ptrTo(attach.Filename),
		Caption: ptrTo(strings.TrimSpace(attach.Caption)),
	}}, nil
}
