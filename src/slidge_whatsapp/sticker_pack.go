package whatsapp

import (
	"archive/zip"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path"
	"strings"
	"unicode/utf8"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
)

const maxStickerPackBytes = 120 * 1024 * 1024
const maxPackAssetBytes = 5 * 1024 * 1024

type packEntry struct {
	File        string `json:"file"`
	Name        string `json:"name"`
	Description string `json:"description"`
	Lottie      bool   `json:"lottie"`
}

type packManifest struct {
	Format   string      `json:"format"`
	Version  int         `json:"version"`
	Name     string      `json:"name"`
	Author   string      `json:"author"`
	Stickers []packEntry `json:"stickers"`
}

func packText(value, fallback string, limit int) string {
	value = strings.TrimSpace(value)
	if !utf8.ValidString(value) || value == "" {
		value = fallback
	}
	runes := []rune(value)
	if len(runes) > limit {
		value = string(runes[:limit])
	}
	return value
}

// No files are extracted. Names in the authenticated protobuf must match ZIP entries exactly.
func bundleStickerPack(pack *waE2E.StickerPackMessage, data []byte) ([]byte, error) {
	if len(data) == 0 || len(data) > maxStickerPackBytes || len(pack.GetStickers()) < 1 || len(pack.GetStickers()) > 200 {
		return nil, fmt.Errorf("sticker pack exceeds limits")
	}
	archive, err := zip.NewReader(bytes.NewReader(data), int64(len(data)))
	if err != nil {
		return nil, fmt.Errorf("invalid sticker pack archive")
	}
	if len(archive.File) > 404 {
		return nil, fmt.Errorf("too many archive entries")
	}
	files := map[string]*zip.File{}
	var total uint64
	for _, file := range archive.File {
		name := file.Name
		if file.Mode()&os.ModeSymlink != 0 || file.Flags&1 != 0 || strings.ContainsAny(name, "\\:\x00") || strings.HasPrefix(name, "/") || path.Clean(name) != strings.TrimSuffix(name, "/") || strings.HasPrefix(name, "../") {
			return nil, fmt.Errorf("unsafe sticker pack entry")
		}
		if file.FileInfo().IsDir() {
			continue
		}
		if _, exists := files[strings.ToLower(name)]; exists {
			return nil, fmt.Errorf("duplicate sticker pack entry")
		}
		if file.UncompressedSize64 > maxPackAssetBytes {
			return nil, fmt.Errorf("sticker pack asset too large")
		}
		total += file.UncompressedSize64
		if total > maxStickerPackBytes {
			return nil, fmt.Errorf("expanded sticker pack too large")
		}
		files[strings.ToLower(name)] = file
	}
	manifest := packManifest{Format: "whatsapp-sticker-pack", Version: 1,
		Name:   packText(pack.GetName(), "Stickers de WhatsApp", 128),
		Author: packText(pack.GetPublisher(), "WhatsApp", 128)}
	var output bytes.Buffer
	writer := zip.NewWriter(&output)
	seen := map[string]bool{}
	for index, item := range pack.GetStickers() {
		name := item.GetFileName()
		file := files[strings.ToLower(name)]
		if file == nil || file.Name != name || seen[name] {
			return nil, fmt.Errorf("missing or duplicate sticker asset")
		}
		seen[name] = true
		reader, err := file.Open()
		if err != nil {
			return nil, fmt.Errorf("unreadable sticker asset")
		}
		payload, readErr := io.ReadAll(io.LimitReader(reader, maxPackAssetBytes+1))
		closeErr := reader.Close()
		if readErr != nil || closeErr != nil || len(payload) > maxPackAssetBytes {
			return nil, fmt.Errorf("corrupt sticker asset")
		}
		suffix := ".webp"
		if item.GetIsLottie() {
			suffix = ".was"
		} else if strings.EqualFold(item.GetMimetype(), "image/png") {
			suffix = ".png"
		}
		normalName := fmt.Sprintf("%03d%s", index, suffix)
		entry, err := writer.Create(normalName)
		if err != nil {
			return nil, err
		}
		if _, err = entry.Write(payload); err != nil {
			return nil, err
		}
		manifest.Stickers = append(manifest.Stickers, packEntry{File: normalName,
			Name:        fmt.Sprintf("Sticker %d", index+1),
			Description: packText(item.GetAccessibilityLabel(), strings.Join(item.GetEmojis(), " "), 8000), Lottie: item.GetIsLottie()})
	}
	metadata, err := json.Marshal(manifest)
	if err != nil {
		return nil, err
	}
	entry, err := writer.Create("manifest.json")
	if err != nil {
		return nil, err
	}
	if _, err = entry.Write(metadata); err != nil {
		return nil, err
	}
	if err = writer.Close(); err != nil {
		return nil, err
	}
	if output.Len() > maxStickerPackBytes {
		return nil, fmt.Errorf("output pack too large")
	}
	return output.Bytes(), nil
}

func receiveStickerPack(ctx context.Context, download func(context.Context, whatsmeow.DownloadableMessage) ([]byte, error), pack *waE2E.StickerPackMessage) Attachment {
	failure := "No se pudo descargar el paquete de stickers. Pide que lo reenvíen."
	if pack.GetFileLength() > maxStickerPackBytes || len(pack.GetStickers()) < 1 || len(pack.GetStickers()) > 200 {
		return Attachment{MIME: "text/plain", Filename: "paquete-no-disponible.txt", Caption: failure, Data: []byte(failure)}
	}
	data, err := download(ctx, pack)
	if err == nil {
		data, err = bundleStickerPack(pack, data)
	}
	if err != nil {
		return Attachment{MIME: "text/plain", Filename: "paquete-no-disponible.txt", Caption: failure, Data: []byte(failure)}
	}
	return Attachment{MIME: "application/x-whatsapp-sticker-pack", Filename: "stickers.canstickers",
		Caption: "Paquete de stickers: " + packText(pack.GetName(), "WhatsApp", 128), Data: data}
}
