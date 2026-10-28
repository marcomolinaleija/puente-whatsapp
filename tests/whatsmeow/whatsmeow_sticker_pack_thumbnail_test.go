package whatsmeow

import (
	"bytes"
	"crypto/sha256"
	"testing"

	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/util/cbcutil"
)

func TestCANStickerPackThumbnailEncryption(t *testing.T) {
	msg := &waE2E.Message{StickerPackMessage: &waE2E.StickerPackMessage{}}
	if getTypeFromMessage(msg) != "media" || getMediaTypeFromMessage(msg) != "sticker_pack" {
		t.Fatal("native pack transport classifier missing")
	}
	key := bytes.Repeat([]byte{42}, 32)
	payload := []byte("synthetic JPEG payload")
	encrypted, upload, err := encryptStickerPackThumbnail(payload, key)
	if err != nil {
		t.Fatal(err)
	}
	iv, cipherKey, macKey, _ := getMediaKeys(key, MediaStickerPackThumbnail)
	ciphertext, mac := encrypted[:len(encrypted)-10], encrypted[len(encrypted)-10:]
	if err = validateMedia(iv, ciphertext, macKey, mac); err != nil {
		t.Fatal(err)
	}
	plain, err := cbcutil.Decrypt(cipherKey, iv, append([]byte(nil), ciphertext...))
	if err != nil || !bytes.Equal(plain, payload) {
		t.Fatal("thumbnail cannot be decrypted")
	}
	sha, encsha := sha256.Sum256(payload), sha256.Sum256(encrypted)
	if !bytes.Equal(upload.FileSHA256, sha[:]) || !bytes.Equal(upload.FileEncSHA256, encsha[:]) || !bytes.Equal(upload.MediaKey, key) {
		t.Fatal("upload metadata lost")
	}
	_, packKey, _, _ := getMediaKeys(key, MediaStickerPack)
	if bytes.Equal(packKey, cipherKey) {
		t.Fatal("thumbnail and pack used same HKDF domain")
	}
	if mediaTypeToMMSType[MediaStickerPackThumbnail] != "thumbnail-sticker-pack" || classToThumbnailMediaType["StickerPackMessage"] != MediaStickerPackThumbnail {
		t.Fatal("thumbnail route missing")
	}
	for _, bad := range [][]byte{nil, key[:31]} {
		if _, _, err = encryptStickerPackThumbnail(payload, bad); err == nil {
			t.Fatal("bad media key accepted")
		}
	}
	key[0] = 0
	if upload.MediaKey[0] == 0 {
		t.Fatal("key is aliased")
	}
}
