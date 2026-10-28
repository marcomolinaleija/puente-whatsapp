package whatsapp

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"os"
	"strings"
	"time"
	"unicode/utf8"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
)

const nativePackMIME = "application/x-can-sticker-pack"
const maxNativePackBytes = 32 * 1024 * 1024

type nativePackEntry struct {
	File        string `json:"file"`
	Description string `json:"description"`
	Animated    bool   `json:"animated"`
}
type nativePackManifest struct {
	Format   string            `json:"format"`
	Version  int               `json:"version"`
	Name     string            `json:"name"`
	Author   string            `json:"author"`
	Stickers []nativePackEntry `json:"stickers"`
}
type nativePackUploader interface {
	Upload(context.Context, []byte, whatsmeow.MediaType) (whatsmeow.UploadResponse, error)
	UploadStickerPackThumbnail(context.Context, []byte, []byte) (whatsmeow.UploadResponse, error)
}

func validPackText(s string, limit int, required bool) bool {
	return utf8.ValidString(s) && !strings.ContainsRune(s, 0) && utf8.RuneCountInString(s) <= limit &&
		(!required || strings.TrimSpace(s) != "")
}

// Only the Python-validated internal envelope reaches this boundary. Validate again before upload.
func prepareNativeStickerPack(data []byte) (*waE2E.StickerPackMessage, []byte, []byte, error) {
	if len(data) == 0 || len(data) > maxNativePackBytes {
		return nil, nil, nil, fmt.Errorf("native pack exceeds limits")
	}
	archive, err := zip.NewReader(bytes.NewReader(data), int64(len(data)))
	if err != nil || len(archive.File) > 63 {
		return nil, nil, nil, fmt.Errorf("invalid native pack archive")
	}
	files := map[string][]byte{}
	total := 0
	for _, file := range archive.File {
		name := file.Name
		if strings.ContainsAny(name, "/\\:\x00") || file.Mode()&os.ModeSymlink != 0 || file.Flags&1 != 0 || file.UncompressedSize64 > 2*1024*1024 {
			return nil, nil, nil, fmt.Errorf("unsafe native pack asset")
		}
		if _, ok := files[strings.ToLower(name)]; ok {
			return nil, nil, nil, fmt.Errorf("duplicate native pack asset")
		}
		reader, err := file.Open()
		if err != nil {
			return nil, nil, nil, err
		}
		payload, readErr := io.ReadAll(io.LimitReader(reader, 2*1024*1024+1))
		reader.Close()
		total += len(payload)
		if readErr != nil || len(payload) > 2*1024*1024 || total > maxNativePackBytes {
			return nil, nil, nil, fmt.Errorf("corrupt native pack asset")
		}
		files[strings.ToLower(name)] = payload
	}
	var manifest nativePackManifest
	if err = json.Unmarshal(files["manifest.json"], &manifest); err != nil || manifest.Format != "can-native-sticker-pack" || manifest.Version != 1 ||
		!validPackText(manifest.Name, 128, true) || !validPackText(manifest.Author, 128, true) || len(manifest.Stickers) < 1 || len(manifest.Stickers) > 60 {
		return nil, nil, nil, fmt.Errorf("invalid native pack metadata")
	}
	digest := sha256.Sum256(data)
	id := hex.EncodeToString(digest[:16])
	trayName := id + ".webp"
	pack := &waE2E.StickerPackMessage{StickerPackID: ptrTo(id), Name: ptrTo(manifest.Name), Publisher: ptrTo(manifest.Author),
		StickerPackOrigin: waE2E.StickerPackMessage_USER_CREATED.Enum(), TrayIconFileName: ptrTo(trayName)}
	var output bytes.Buffer
	writer := zip.NewWriter(&output)
	for index, entry := range manifest.Stickers {
		expected := fmt.Sprintf("%03d.webp", index)
		payload := files[expected]
		limit := 100 * 1024
		if entry.Animated {
			limit = 500 * 1024
		}
		if entry.File != expected || !validPackText(entry.Description, 8000, false) || len(payload) < 12 || len(payload) > limit || string(payload[:4]) != "RIFF" || string(payload[8:12]) != "WEBP" {
			return nil, nil, nil, fmt.Errorf("invalid native sticker")
		}
		hash := sha256.Sum256(payload)
		name := fmt.Sprintf("%02d_%s.webp", index, url.PathEscape(base64.StdEncoding.EncodeToString(hash[:])))
		file, err := writer.CreateHeader(&zip.FileHeader{Name: name, Method: zip.Store})
		if err != nil {
			return nil, nil, nil, err
		}
		if _, err = file.Write(payload); err != nil {
			return nil, nil, nil, err
		}
		pack.Stickers = append(pack.Stickers, &waE2E.StickerPackMessage_Sticker{FileName: ptrTo(name), IsAnimated: ptrTo(entry.Animated),
			AccessibilityLabel: ptrTo(entry.Description), IsLottie: ptrTo(false), Mimetype: ptrTo("image/webp")})
	}
	tray, thumb := files["tray.webp"], files["thumbnail.jpg"]
	if len(files) != len(manifest.Stickers)+3 || len(tray) < 12 || len(tray) > 100*1024 || string(tray[:4]) != "RIFF" || string(tray[8:12]) != "WEBP" ||
		len(thumb) < 3 || len(thumb) > 100*1024 || !bytes.HasPrefix(thumb, []byte{255, 216, 255}) {
		return nil, nil, nil, fmt.Errorf("invalid native pack cover")
	}
	file, err := writer.CreateHeader(&zip.FileHeader{Name: trayName, Method: zip.Store})
	if err != nil {
		return nil, nil, nil, err
	}
	if _, err = file.Write(tray); err != nil {
		return nil, nil, nil, err
	}
	if err = writer.Close(); err != nil {
		return nil, nil, nil, err
	}
	if output.Len() > maxNativePackBytes {
		return nil, nil, nil, fmt.Errorf("native pack exceeds limits")
	}
	thumbHash := sha256.Sum256(thumb)
	pack.ImageDataHash = ptrTo(base64.StdEncoding.EncodeToString(thumbHash[:]))
	return pack, output.Bytes(), thumb, nil
}

func uploadNativeStickerPack(ctx context.Context, client nativePackUploader, attach *Attachment) (*waE2E.Message, error) {
	pack, archive, thumbnail, err := prepareNativeStickerPack(attach.Data)
	if err != nil {
		return nil, err
	}
	upload, err := client.Upload(ctx, archive, whatsmeow.MediaStickerPack)
	if err != nil {
		return nil, err
	}
	thumb, err := client.UploadStickerPackThumbnail(ctx, thumbnail, upload.MediaKey)
	if err != nil {
		return nil, err
	}
	pack.DirectPath = ptrTo(upload.DirectPath)
	pack.MediaKey = upload.MediaKey
	pack.FileSHA256 = upload.FileSHA256
	pack.FileEncSHA256 = upload.FileEncSHA256
	pack.FileLength = ptrTo(uint64(len(archive)))
	pack.StickerPackSize = ptrTo(uint64(len(archive)))
	pack.MediaKeyTimestamp = ptrTo(time.Now().Unix())
	pack.ThumbnailDirectPath = ptrTo(thumb.DirectPath)
	pack.ThumbnailSHA256 = thumb.FileSHA256
	pack.ThumbnailEncSHA256 = thumb.FileEncSHA256
	pack.ThumbnailWidth = ptrTo(uint32(252))
	pack.ThumbnailHeight = ptrTo(uint32(252))
	return &waE2E.Message{StickerPackMessage: pack}, nil
}
