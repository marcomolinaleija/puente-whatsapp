// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.
package whatsmeow

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"fmt"

	"go.mau.fi/whatsmeow/util/cbcutil"
)

const MediaStickerPackThumbnail MediaType = "WhatsApp Sticker Pack Thumbnail Keys"

func init() {
	mediaTypeToMMSType[MediaStickerPackThumbnail] = "thumbnail-sticker-pack"
	classToThumbnailMediaType["StickerPackMessage"] = MediaStickerPackThumbnail
}

// UploadStickerPackThumbnail uses the pack's key, with a distinct HKDF domain.
// It deliberately cannot upload arbitrary media or choose an unbounded payload.
func encryptStickerPackThumbnail(plaintext, mediaKey []byte) (encrypted []byte, resp UploadResponse, err error) {
	if len(mediaKey) != 32 || len(plaintext) == 0 || len(plaintext) > 100*1024 {
		return nil, resp, fmt.Errorf("invalid sticker pack thumbnail")
	}
	resp.FileLength = uint64(len(plaintext))
	resp.MediaKey = append([]byte(nil), mediaKey...)
	digest := sha256.Sum256(plaintext)
	resp.FileSHA256 = digest[:]
	iv, cipherKey, macKey, _ := getMediaKeys(resp.MediaKey, MediaStickerPackThumbnail)
	ciphertext, err := cbcutil.Encrypt(cipherKey, iv, plaintext)
	if err != nil {
		return nil, resp, err
	}
	mac := hmac.New(sha256.New, macKey)
	mac.Write(iv)
	mac.Write(ciphertext)
	encrypted = append(ciphertext, mac.Sum(nil)[:10]...)
	encryptedDigest := sha256.Sum256(encrypted)
	resp.FileEncSHA256 = encryptedDigest[:]
	return encrypted, resp, nil
}

func (cli *Client) UploadStickerPackThumbnail(ctx context.Context, plaintext, mediaKey []byte) (resp UploadResponse, err error) {
	encrypted, resp, err := encryptStickerPackThumbnail(plaintext, mediaKey)
	if err != nil {
		return resp, err
	}
	err = cli.rawUpload(ctx, bytes.NewReader(encrypted), uint64(len(encrypted)), resp.FileEncSHA256,
		MediaStickerPackThumbnail, false, &resp)
	return resp, err
}
