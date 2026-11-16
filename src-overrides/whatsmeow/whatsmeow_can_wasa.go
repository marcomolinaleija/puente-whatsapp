package whatsmeow

// CAN WASA v1: backport of upstream whatsmeow (2026-11-13, ecc39170bc14) support
// for Muse, a WhatsApp assistant whose messages are encrypted with a per-bot
// "root secret" synchronized through app state (wasa_root_secret). This file
// keeps the upstream wire format; root secret IDs live in memory and are
// registered by the bridge after reading app state, because this pinned
// store has no ChatSettings WASA columns.

import (
	"context"
	"fmt"
	"sync"

	"go.mau.fi/util/random"
	"google.golang.org/protobuf/proto"

	"go.mau.fi/whatsmeow/proto/waAICommon"
	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/util/gcmutil"
	"go.mau.fi/whatsmeow/util/hkdfutil"
)

// CANMuseJID is upstream types.MuseJID ("hatch").
var CANMuseJID = types.NewJID("1807055946647697", types.BotServer)

const canMusePersonaID = "1807055946647697$1"

type canWASAKey struct {
	client *Client
	bot    types.JID
}

var canWASARootIDs sync.Map // canWASAKey -> types.MessageID

// CANSetWASARootSecretID records the active root secret ID for a bot. The
// secret itself must already be stored in MsgSecrets as (bot, own LID, id).
func (cli *Client) CANSetWASARootSecretID(bot types.JID, id types.MessageID) {
	if cli == nil || id == "" {
		return
	}
	canWASARootIDs.Store(canWASAKey{cli, bot.ToNonAD()}, id)
}

// CANWASARootSecretID returns the active root secret ID for a bot, if known.
func (cli *Client) CANWASARootSecretID(bot types.JID) types.MessageID {
	value, ok := canWASARootIDs.Load(canWASAKey{cli, bot.ToNonAD()})
	if !ok {
		return ""
	}
	return value.(types.MessageID)
}

// CANForgetWASA drops the root IDs of a client that is being discarded.
func (cli *Client) CANForgetWASA() {
	canWASARootIDs.Range(func(key, _ any) bool {
		if key.(canWASAKey).client == cli {
			canWASARootIDs.Delete(key)
		}
		return true
	})
}

func canDefaultBotPersona(to types.JID) string {
	if to == CANMuseJID {
		return canMusePersonaID
	}
	return "867051314767696$760019659443059"
}

// canEncryptWASAMessage ports upstream encryptWASAMessage.
func (cli *Client) canEncryptWASAMessage(ctx context.Context, bot types.JID, id types.MessageID, msg *waE2E.Message) ([]byte, error) {
	if msg.GetProtocolMessage().GetType() == waE2E.ProtocolMessage_REQUEST_WELCOME_MESSAGE {
		return proto.Marshal(msg)
	}
	rootID := cli.CANWASARootSecretID(bot)
	if rootID == "" {
		return nil, fmt.Errorf("no active WASA root secret for %s", bot)
	}
	ownLID := cli.getOwnLID().ToNonAD()
	if ownLID.IsEmpty() {
		return nil, fmt.Errorf("own LID not available for WASA message")
	}
	secret, _, err := cli.Store.MsgSecrets.GetMessageSecret(ctx, bot, ownLID, rootID)
	if err != nil {
		return nil, fmt.Errorf("failed to get WASA root secret: %w", err)
	}
	if len(secret) != 32 {
		return nil, fmt.Errorf("invalid WASA root secret for %s", bot)
	}
	msg = proto.Clone(msg).(*waE2E.Message)
	if document := msg.GetDocumentWithCaptionMessage().GetMessage().GetDocumentMessage(); document != nil {
		msg.DocumentMessage = document
		msg.DocumentWithCaptionMessage = nil
	}
	if msg.MessageContextInfo == nil {
		msg.MessageContextInfo = &waE2E.MessageContextInfo{}
	}
	if msg.MessageContextInfo.BotMetadata == nil {
		msg.MessageContextInfo.BotMetadata = &waAICommon.BotMetadata{}
	}
	msg.MessageContextInfo.BotMetadata.CapabilityMetadata = &waAICommon.BotCapabilityMetadata{}
	plaintext, err := proto.Marshal(msg)
	if err != nil {
		return nil, err
	}
	key := hkdfutil.SHA256(applyBotMessageHKDF(secret), nil, []byte(string(id)+ownLID.String()+bot.String()), 32)
	iv := random.Bytes(12)
	additionalData := append(append([]byte(id), 0), []byte(ownLID.String())...)
	ciphertext, err := gcmutil.Encrypt(key, iv, plaintext, additionalData)
	if err != nil {
		return nil, err
	}
	return proto.Marshal(&waE2E.Message{SecretEncryptedMessage: &waE2E.SecretEncryptedMessage{
		TargetMessageKey: &waCommon.MessageKey{
			RemoteJID: proto.String(bot.String()),
			FromMe:    proto.Bool(true),
			ID:        proto.String(string(rootID)),
		},
		EncPayload: ciphertext,
		EncIV:      iv,
	}})
}
