package whatsapp

import (
	"context"
	"slices"
	"strings"
	"testing"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waAICommon"
	"go.mau.fi/whatsmeow/proto/waAICommonDeprecated"
	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waSyncAction"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

func canMetaAITestClient() *whatsmeow.Client {
	return &whatsmeow.Client{Log: waLog.Noop}
}

func canMetaAIBotEvent(stage types.BotEditType, target string, msg *waE2E.Message) *events.Message {
	evt := &events.Message{Message: msg}
	evt.Info.ID = "BOTMSG2"
	evt.Info.Sender = types.NewMetaAIJID
	evt.Info.Chat = types.NewMetaAIJID
	evt.Info.MsgBotInfo = types.MsgBotInfo{EditType: stage, EditTargetID: target}
	return evt
}

func TestCANMetaAIBotIdentityUsesDocumentedPhoneAlias(t *testing.T) {
	phone, ok := canBotPhoneJID(types.NewMetaAIJID)
	if !ok || phone != types.MetaAIJID {
		t.Fatalf("expected Meta AI phone alias, got %v %t", phone, ok)
	}
	if got := canBotSendJID(types.MetaAIJID); got != types.NewMetaAIJID {
		t.Fatalf("expected phone alias to send through @bot, got %v", got)
	}
	unknown := types.NewJID("000000000000001", types.BotServer)
	if _, ok := canBotPhoneJID(unknown); ok {
		t.Fatalf("unmapped bot must not invent a phone alias")
	}
	person := types.NewJID("12025550100", types.DefaultUserServer)
	if got := canBotSendJID(person); got != person {
		t.Fatalf("regular contacts must keep their JID, got %v", got)
	}
	if got := canBotSendJID(types.NewADJID("13135550002", 0, 3)); got.Server == types.BotServer {
		t.Fatalf("device-specific JIDs must not be rerouted")
	}
}

func TestCANMetaAIChatAndActorMapToPhoneAlias(t *testing.T) {
	chat := newChat(context.Background(), nil, types.NewMetaAIJID, false)
	if chat.JID != types.MetaAIJID.String() || chat.IsGroup {
		t.Fatalf("unexpected bot chat %+v", chat)
	}
	muse := newChat(context.Background(), nil, whatsmeow.CANMuseJID, false)
	if muse.JID != "1807055946647697@bot" || muse.IsGroup {
		t.Fatalf("unmapped bots must keep their bot identity, got %+v", muse)
	}
	if got := newChat(context.Background(), nil, whatsmeow.CANMuseJID, true); got.JID != "" {
		t.Fatalf("a bot is never a group chat, got %+v", got)
	}
	actor := newActor(context.Background(), canMetaAITestClient(), whatsmeow.CANMuseJID)
	if actor.JID != "1807055946647697@bot" || actor.LID != "" || actor.IsMe {
		t.Fatalf("unexpected Muse actor %+v", actor)
	}
	if got := canBotSendJID(whatsmeow.CANMuseJID); got != whatsmeow.CANMuseJID {
		t.Fatalf("Muse must be sent to its bot identity, got %v", got)
	}
	if canBotName(whatsmeow.CANMuseJID) != "Muse" || canBotName(types.NewJID("1", types.BotServer)) == "" {
		t.Fatalf("assistant names must never be empty")
	}
}

func TestCANWASARootSecretSelectsLatestValidEpoch(t *testing.T) {
	bot := whatsmeow.CANMuseJID
	own := types.NewJID("100000000000001", types.HiddenUserServer)
	secret := make([]byte, 32)
	entries := []*waSyncAction.WASARootSecretAction_RootSecretEntry{
		{ID: proto.String("ROOT1"), RootSecret: secret, Epoch: proto.Int64(1)},
		{ID: proto.String("ROOT3"), RootSecret: secret, Epoch: proto.Int64(3)},
		{ID: proto.String("BAD"), RootSecret: []byte{1}, Epoch: proto.Int64(9)},
		{ID: proto.String(""), RootSecret: secret, Epoch: proto.Int64(8)},
	}
	inserts, active := canWASARootSecrets(bot, own, entries)
	if active != "ROOT3" || len(inserts) != 2 {
		t.Fatalf("expected latest valid root, got %q with %d inserts", active, len(inserts))
	}
	for _, insert := range inserts {
		if insert.Chat != bot || insert.Sender != own {
			t.Fatalf("root secrets must be keyed by bot chat and own LID, got %+v", insert)
		}
	}
	if _, active := canWASARootSecrets(bot, own, nil); active != "" {
		t.Fatalf("no entries must not invent a root")
	}
}

func TestCANMetaAIStreamingCollapsesToFinalCorrection(t *testing.T) {
	client := canMetaAITestClient()
	base := Message{ID: "BOTMSG2", Chat: Chat{JID: types.MetaAIJID.String()}}

	inner := base
	_, kind, payload, handled := canBotMessageEvent(client,
		canMetaAIBotEvent(types.EditTypeInner, "BOTMSG1", &waE2E.Message{Conversation: proto.String("Respuesta parc")}), &inner)
	if !handled || kind != EventUnknown || payload != nil {
		t.Fatalf("inner updates must be dropped, got handled=%t kind=%v", handled, kind)
	}

	canBotAnswers.add("BOTMSG1") // the "first" stage already reached XMPP
	last := base
	_, kind, payload, handled = canBotMessageEvent(client,
		canMetaAIBotEvent(types.EditTypeLast, "BOTMSG1", &waE2E.Message{Conversation: proto.String("Respuesta final")}), &last)
	if !handled || kind != EventMessage || payload == nil {
		t.Fatalf("last update must become an event, got handled=%t kind=%v", handled, kind)
	}
	if payload.Message.Kind != MessageEdit || payload.Message.ReferenceID != "BOTMSG1" || payload.Message.Body != "Respuesta final" {
		t.Fatalf("unexpected final correction %+v", payload.Message)
	}

	empty := base
	_, kind, _, handled = canBotMessageEvent(client, canMetaAIBotEvent(types.EditTypeLast, "BOTMSG1", &waE2E.Message{}), &empty)
	if !handled || kind != EventUnknown {
		t.Fatalf("empty final update must not erase the answer")
	}

	first := base
	evt, _, _, handled := canBotMessageEvent(client,
		canMetaAIBotEvent(types.EditTypeFirst, "", &waE2E.Message{Conversation: proto.String("Hola")}), &first)
	if handled || evt.Message.GetConversation() != "Hola" {
		t.Fatalf("first stage must continue through the regular message path")
	}

	unmapped := Message{ID: "BOTMSG3"}
	_, kind, _, handled = canBotMessageEvent(client, canMetaAIBotEvent("", "", &waE2E.Message{Conversation: proto.String("x")}), &unmapped)
	if !handled || kind != EventUnknown {
		t.Fatalf("messages without an addressable chat must be dropped")
	}
}

func TestCANMetaAIUnifiedFullAnswerReachesXMPPOnce(t *testing.T) {
	client := canMetaAITestClient()
	unified := []byte(`{"response":{"sections":[{"view_model":{"primitive":{"text":"Hola, soy un asistente de prueba"}}},` +
		`{"view_model":{"primitive":{"markdown":"Segundo párrafo"}}},{"sources":[{"url":"https://example.invalid","id":7}]}]}}`)
	richMessage := func() *waE2E.Message {
		return &waE2E.Message{RichResponseMessage: &waE2E.AIRichResponseMessage{
			UnifiedResponse: &waAICommon.AIRichResponseUnifiedResponse{Data: unified},
		}}
	}
	if got := canBotText(richMessage()); got != "Hola, soy un asistente de prueba\n\nSegundo párrafo" {
		t.Fatalf("unexpected unified text %q", got)
	}
	// A "full" edit whose original never reached XMPP is published under the original ID.
	edit := &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{
		Type:          waE2E.ProtocolMessage_MESSAGE_EDIT.Enum(),
		Key:           &waCommon.MessageKey{ID: proto.String("BOTUNSEEN")},
		EditedMessage: richMessage(),
	}}
	message := Message{ID: "BOTEDIT1", Chat: Chat{JID: "1807055946647697@bot"}}
	_, kind, payload, handled := canBotMessageEvent(client, canMetaAIBotEvent(types.EditTypeFull, "", edit), &message)
	if !handled || kind != EventMessage || payload.Message.Kind != MessagePlain || payload.Message.ID != "BOTUNSEEN" {
		t.Fatalf("unseen full answer must become a plain message, got %+v", payload)
	}
	// The next full edit of the same answer is a correction.
	message = Message{ID: "BOTEDIT2", Chat: Chat{JID: "1807055946647697@bot"}}
	_, _, payload, _ = canBotMessageEvent(client, canMetaAIBotEvent(types.EditTypeFull, "", edit), &message)
	if payload.Message.Kind != MessageEdit || payload.Message.ReferenceID != "BOTUNSEEN" || payload.Message.Body == "" {
		t.Fatalf("seen full answer must be corrected, got %+v", payload.Message)
	}

	// A protocol edit without readable text must never blank the answer.
	blank := &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{
		Type: waE2E.ProtocolMessage_MESSAGE_EDIT.Enum(), Key: &waCommon.MessageKey{ID: proto.String("BOTUNSEEN")},
		EditedMessage: &waE2E.Message{RichResponseMessage: &waE2E.AIRichResponseMessage{}},
	}}
	message = Message{ID: "BOTEDIT3", Chat: Chat{JID: "1807055946647697@bot"}}
	if _, kind, _, handled := canBotMessageEvent(client, canMetaAIBotEvent("", "", blank), &message); !handled || kind != EventUnknown {
		t.Fatalf("empty edit must be dropped")
	}
	// A rich "first" without text yet is held back instead of creating an empty row.
	message = Message{ID: "BOTFIRST", Chat: Chat{JID: "1807055946647697@bot"}}
	empty := &waE2E.Message{RichResponseMessage: &waE2E.AIRichResponseMessage{}}
	if _, kind, _, handled := canBotMessageEvent(client, canMetaAIBotEvent(types.EditTypeFirst, "", empty), &message); !handled || kind != EventUnknown {
		t.Fatalf("textless rich first must be held back")
	}
	if canUnifiedText([]byte("not json")) != "" {
		t.Fatalf("non-JSON unified payloads must be ignored")
	}
}

func TestCANMetaAIAnswerKeepsLinksAndSuggestionsApart(t *testing.T) {
	unified := []byte(`{"response":{"sections":[` +
		`{"view_model":{"primitive":{"text":"Respuesta de prueba"}}},` +
		`{"sources":[{"title":"Fuente ficticia","url":"https://example.invalid/a","thumbnail":{"url":"https://cdn.example.invalid/t.png"}}]},` +
		`{"suggested_prompts":[{"text":"¿Algo más?"},{"text":"Dame un ejemplo"}]},` +
		`{"card":{"title":"Tarjeta","link":"https://example.invalid/b","icon_url":"https://cdn.example.invalid/i.png"}}]}}`)
	parts := canUnifiedParse(unified)
	if parts.Text != "Respuesta de prueba" {
		t.Fatalf("suggestions and sources must stay out of the body, got %q", parts.Text)
	}
	if len(parts.Links) != 2 || parts.Links[0] != (canBotLink{"Fuente ficticia", "https://example.invalid/a"}) ||
		parts.Links[1] != (canBotLink{"Tarjeta", "https://example.invalid/b"}) {
		t.Fatalf("unexpected links (media URLs must be skipped): %+v", parts.Links)
	}
	if !slices.Equal(parts.Prompts, []string{"¿Algo más?", "Dame un ejemplo"}) {
		t.Fatalf("unexpected suggestions %+v", parts.Prompts)
	}

	// Typed bot metadata adds cited sources and suggestions without duplicates.
	msg := &waE2E.Message{
		RichResponseMessage: &waE2E.AIRichResponseMessage{UnifiedResponse: &waAICommon.AIRichResponseUnifiedResponse{Data: unified}},
		MessageContextInfo: &waE2E.MessageContextInfo{BotMetadata: &waAICommon.BotMetadata{
			RichResponseSourcesMetadata: &waAICommon.BotSourcesMetadata{Sources: []*waAICommon.BotSourcesMetadata_BotSourceItem{
				{SourceTitle: proto.String("Fuente ficticia"), SourceProviderURL: proto.String("https://example.invalid/a")},
				{SourceTitle: proto.String("Otra"), SourceProviderURL: proto.String("javascript:alert(1)")},
			}},
			SuggestedPromptMetadata: &waAICommon.BotSuggestedPromptMetadata{
				SuggestedPrompts: []string{"Dame un ejemplo", "Resúmelo"},
			},
		}},
	}
	answer := canBotAnswer(msg)
	if len(answer.Links) != 2 || !slices.Equal(answer.Prompts, []string{"¿Algo más?", "Dame un ejemplo", "Resúmelo"}) {
		t.Fatalf("unexpected merged answer %+v", answer)
	}
	want := "Respuesta de prueba\n\nEnlaces:\n1. Fuente ficticia: https://example.invalid/a\n2. Tarjeta: https://example.invalid/b" +
		"\n\nSugerencias:\n• ¿Algo más?\n• Dame un ejemplo\n• Resúmelo"
	if got := canComposeBotAnswer(answer); got != want {
		t.Fatalf("unexpected composed answer:\n%s", got)
	}
	if canComposeBotAnswer(canBotAnswerParts{Prompts: []string{"x"}}) != "" {
		t.Fatalf("suggestions alone must not create an answer")
	}

	// A layout where every text lives under a suggestion-like container still shows text.
	odd := canUnifiedParse([]byte(`{"prompt_response":{"text":"Texto visible"}}`))
	if odd.Text != "Texto visible" {
		t.Fatalf("fallback must keep the answer visible, got %q", odd.Text)
	}
}

func TestCANMetaAIUnwrapsEnvelopesAndRichAnswers(t *testing.T) {
	rich := &waE2E.Message{RichResponseMessage: &waE2E.AIRichResponseMessage{
		Submessages: []*waAICommonDeprecated.AIRichResponseSubMessage{
			{
				MessageType: waAICommonDeprecated.AIRichResponseSubMessageType_AI_RICH_RESPONSE_TEXT.Enum(),
				MessageText: proto.String("Texto de prueba"),
			},
			{
				MessageType: waAICommonDeprecated.AIRichResponseSubMessageType_AI_RICH_RESPONSE_TABLE.Enum(),
				TableMetadata: &waAICommonDeprecated.AIRichResponseTableMetadata{
					Title: proto.String("Tabla"),
					Rows: []*waAICommonDeprecated.AIRichResponseTableMetadata_AIRichResponseTableRow{
						{Items: []string{"a", "b"}},
					},
				},
			},
			{
				MessageType: waAICommonDeprecated.AIRichResponseSubMessageType_AI_RICH_RESPONSE_CODE.Enum(),
				CodeMetadata: &waAICommonDeprecated.AIRichResponseCodeMetadata{
					CodeLanguage: proto.String("go"),
					CodeBlocks: []*waAICommonDeprecated.AIRichResponseCodeMetadata_AIRichResponseCodeBlock{
						{CodeContent: proto.String("x := 1")},
					},
				},
			},
		},
	}}
	wrapped := &waE2E.Message{BotInvokeMessage: &waE2E.FutureProofMessage{Message: rich}}
	text := canBotText(wrapped)
	for _, part := range []string{"Texto de prueba", "Tabla\na | b", "```go\nx := 1\n```"} {
		if !strings.Contains(text, part) {
			t.Fatalf("rich answer lost %q: %q", part, text)
		}
	}
	if canUnwrapBotMessage(wrapped) != rich {
		t.Fatalf("expected the invoke envelope to unwrap")
	}

	client := canMetaAITestClient()
	message := Message{ID: "BOTMSG2", Chat: Chat{JID: types.MetaAIJID.String()}}
	evt, _, _, handled := canBotMessageEvent(client, canMetaAIBotEvent("", "", wrapped), &message)
	if handled || evt.Message != rich || message.Body != text {
		t.Fatalf("rich answer must continue as plain text, handled=%t body=%q", handled, message.Body)
	}

	edited := &waE2E.Message{ProtocolMessage: &waE2E.ProtocolMessage{
		EditedMessage: &waE2E.Message{ExtendedTextMessage: &waE2E.ExtendedTextMessage{Text: proto.String("Editado")}},
	}}
	if got := canBotText(edited); got != "Editado" {
		t.Fatalf("expected edited extended text, got %q", got)
	}
}

func TestCANMetaAIRichTextIsBounded(t *testing.T) {
	big := strings.Repeat("x", canBotRichTextMaxBytes)
	rich := &waE2E.AIRichResponseMessage{Submessages: []*waAICommonDeprecated.AIRichResponseSubMessage{
		{MessageText: proto.String("inicio")},
		{MessageText: proto.String(big)},
	}}
	if got := canRichResponseText(rich); got != "inicio" {
		t.Fatalf("expected bounded rich text, got %d bytes", len(got))
	}
}
