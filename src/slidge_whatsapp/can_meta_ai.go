package whatsapp

// CAN Meta AI v1: WhatsApp assistants (Meta AI, Muse and other personas) use
// "@bot" identities. XMPP addresses them through the phone alias documented in
// types.BotJIDMap when there is one, otherwise through the "@bot" identity
// itself, so the existing contact, chat, archive and correction paths apply.
// Outgoing messages go to the "@bot" identity so whatsmeow enables bot mode.
// Streamed answers ("first", "inner", "last", "full") collapse into one message
// plus a final correction; partial "inner" updates are never forwarded.

import (
	"encoding/json"
	"fmt"
	"slices"
	"strings"
	"sync"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waAICommonDeprecated"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

const canBotRichTextMaxBytes = 64 * 1024

// canBotByPhone maps a bot phone alias back to its "@bot" identity.
var canBotByPhone = func() map[types.JID]types.JID {
	reverse := make(map[types.JID]types.JID, len(types.BotJIDMap))
	for bot, phone := range types.BotJIDMap {
		reverse[phone.ToNonAD()] = bot.ToNonAD()
	}
	return reverse
}()

// canBotPhoneJID returns the addressable phone alias for a "@bot" identity.
func canBotPhoneJID(jid types.JID) (types.JID, bool) {
	if jid.Server != types.BotServer {
		return types.EmptyJID, false
	}
	phone, ok := types.BotJIDMap[jid.ToNonAD()]
	return phone.ToNonAD(), ok
}

// canBotChatJID returns the XMPP-facing identity of a bot: its phone alias when
// whatsmeow documents one, otherwise the "@bot" identity itself (e.g. Muse).
func canBotChatJID(jid types.JID) (types.JID, bool) {
	if jid.Server != types.BotServer {
		return types.EmptyJID, false
	}
	if phone, ok := canBotPhoneJID(jid); ok {
		return phone, true
	}
	if jid.User == "" {
		return types.EmptyJID, false
	}
	return jid.ToNonAD(), true
}

// canKnownBotNames names assistants whose profile may be unavailable.
var canKnownBotNames = map[types.JID]string{
	types.NewMetaAIJID:   "Meta AI",
	whatsmeow.CANMuseJID: "Muse",
}

func canBotName(bot types.JID) string {
	if name := canKnownBotNames[bot.ToNonAD()]; name != "" {
		return name
	}
	return "Asistente de Meta"
}

// canBotSendJID returns the "@bot" identity for a known bot phone alias.
func canBotSendJID(jid types.JID) types.JID {
	if jid.Server == types.DefaultUserServer && jid.Device == 0 {
		if bot, ok := canBotByPhone[jid.ToNonAD()]; ok {
			return bot
		}
	}
	return jid
}

func canIsBotMessage(evt *events.Message) bool {
	return evt != nil && (evt.Info.Sender.IsBot() || evt.Info.Chat.IsBot() || evt.Info.MsgBotInfo.EditType != "")
}

// canBotRosterContacts returns assistants the account really uses: those with
// a WASA root secret (e.g. Muse). Others appear once they write.
func (s *Session) canBotRosterContacts(client *whatsmeow.Client) []Contact {
	if client == nil || client.Store == nil || client.Store.ID == nil {
		return nil
	}
	var contacts []Contact
	for _, bot := range []types.JID{whatsmeow.CANMuseJID} {
		if client.CANWASARootSecretID(bot) != "" {
			contacts = append(contacts, Contact{Actor: Actor{JID: bot.String()}, Name: canBotName(bot), IsFriend: true})
		}
	}
	return contacts
}

// canPrepareBotSend routes a phone alias to its "@bot" identity. whatsmeow
// sets the persona and, for Muse, encrypts with its WASA root secret.
func (s *Session) canPrepareBotSend(_ *whatsmeow.Client, jid types.JID, payload *waE2E.Message) types.JID {
	bot := canBotSendJID(jid)
	if !bot.IsBot() || bot.Server != types.BotServer || payload == nil {
		return jid
	}
	return bot
}

// canUnwrapBotMessage returns the content a bot wrapped in a future-proof envelope.
func canUnwrapBotMessage(msg *waE2E.Message) *waE2E.Message {
	for depth := 0; msg != nil && depth < 3; depth++ {
		var inner *waE2E.Message
		for _, envelope := range []*waE2E.FutureProofMessage{
			msg.GetBotInvokeMessage(), msg.GetBotForwardedMessage(), msg.GetBotTaskMessage(),
		} {
			if envelope.GetMessage() != nil {
				inner = envelope.GetMessage()
				break
			}
		}
		if inner == nil {
			return msg
		}
		msg = inner
	}
	return msg
}

// canBotText extracts readable text from plain, extended, edited or rich answers.
func canBotText(msg *waE2E.Message) string {
	msg = canUnwrapBotMessage(msg)
	if msg == nil {
		return ""
	}
	if edited := msg.GetProtocolMessage().GetEditedMessage(); edited != nil {
		return canBotText(edited)
	}
	if text := msg.GetConversation(); text != "" {
		return text
	}
	if text := msg.GetExtendedTextMessage().GetText(); text != "" {
		return text
	}
	return canRichResponseText(msg.GetRichResponseMessage())
}

// canBotRich returns the rich answer of a bot message, also inside an edit.
func canBotRich(msg *waE2E.Message) *waE2E.AIRichResponseMessage {
	msg = canUnwrapBotMessage(msg)
	if edited := msg.GetProtocolMessage().GetEditedMessage(); edited != nil {
		return canBotRich(edited)
	}
	return msg.GetRichResponseMessage()
}

// canUnifiedTextKeys are the JSON keys whose string values are readable text.
var canUnifiedTextKeys = map[string]bool{
	"text": true, "markdown": true, "markdown_text": true, "plain_text": true, "content_text": true,
}

// canBotLink is a link offered with an answer (a cited source or a card).
type canBotLink struct {
	Title string
	URL   string
}

// canBotAnswerParts is the readable content of an assistant answer.
type canBotAnswerParts struct {
	Text    string
	Links   []canBotLink
	Prompts []string // suggested follow-up prompts, shown as buttons on phones
}

const (
	canBotMaxLinks   = 10
	canBotMaxPrompts = 8
	canBotMaxItem    = 300
)

var (
	canPromptContext = []string{"suggest", "prompt", "follow", "quick_repl", "chip", "starter"}
	canSourceContext = []string{"source", "citation", "reference"}
	canURLKeys       = map[string]bool{"url": true, "uri": true, "link": true, "href": true, "source_url": true, "canonical_url": true, "sourceproviderurl": true, "source_provider_url": true}
	canMediaContext  = []string{"thumbnail", "favicon", "image", "icon", "cdn", "avatar", "logo", "media"}
	canTitleKeys     = []string{"title", "source_title", "sourcetitle", "name", "label", "text", "display_text"}
)

func canContextHas(context string, words []string) bool {
	for _, word := range words {
		if strings.Contains(context, word) {
			return true
		}
	}
	return false
}

func canBoundItem(text string) string {
	text = strings.Join(strings.Fields(text), " ")
	if len(text) > canBotMaxItem {
		text = strings.TrimSpace(text[:canBotMaxItem]) + "…"
	}
	return text
}

func (p *canBotAnswerParts) addLink(title, url string) {
	url = strings.TrimSpace(url)
	if !(strings.HasPrefix(url, "https://") || strings.HasPrefix(url, "http://")) || len(url) > 2048 {
		return
	}
	for i, link := range p.Links {
		if link.URL == url {
			if link.Title == "" {
				p.Links[i].Title = canBoundItem(title)
			}
			return
		}
	}
	if len(p.Links) < canBotMaxLinks {
		p.Links = append(p.Links, canBotLink{Title: canBoundItem(title), URL: url})
	}
}

func (p *canBotAnswerParts) addPrompt(prompt string) {
	prompt = canBoundItem(prompt)
	if prompt == "" || slices.Contains(p.Prompts, prompt) || len(p.Prompts) >= canBotMaxPrompts {
		return
	}
	p.Prompts = append(p.Prompts, prompt)
}

// canUnifiedParse extracts the answer text, links and suggestions from a
// unified response JSON payload. Text under suggestion or source containers
// is not mixed into the answer body.
func canUnifiedParse(data []byte) canBotAnswerParts {
	var parts canBotAnswerParts
	var root any
	if len(data) == 0 || !json.Valid(data) || json.Unmarshal(data, &root) != nil {
		return parts
	}
	var body, fallback []string
	size := 0
	var walk func(value any, context string, depth int)
	walk = func(value any, context string, depth int) {
		if depth > 24 || size >= canBotRichTextMaxBytes {
			return
		}
		switch typed := value.(type) {
		case map[string]any:
			keys := make([]string, 0, len(typed))
			for key := range typed {
				keys = append(keys, key)
			}
			slices.Sort(keys)
			isPrompt := canContextHas(context, canPromptContext)
			isSource := canContextHas(context, canSourceContext)
			title := ""
			for _, want := range canTitleKeys {
				if text, ok := typed[want].(string); ok && strings.TrimSpace(text) != "" {
					title = text
					break
				}
			}
			// Text keys first keep a section's own text ahead of nested metadata.
			for _, key := range keys {
				text, ok := typed[key].(string)
				if !ok {
					continue
				}
				lower := strings.ToLower(key)
				if canURLKeys[lower] && !canContextHas(context+"."+lower, canMediaContext) {
					parts.addLink(title, text)
					continue
				}
				if !canUnifiedTextKeys[key] {
					continue
				}
				if trimmed := strings.TrimSpace(text); trimmed != "" && len(fallback) < 256 {
					fallback = append(fallback, trimmed)
				}
				switch {
				case isPrompt:
					parts.addPrompt(text)
				case isSource:
					// Source labels belong to their link, not to the answer body.
				default:
					text = strings.TrimSpace(text)
					if text != "" && (len(body) == 0 || body[len(body)-1] != text) && size+len(text) <= canBotRichTextMaxBytes {
						body = append(body, text)
						size += len(text)
					}
				}
			}
			for _, key := range keys {
				if _, isText := typed[key].(string); !isText {
					walk(typed[key], context+"."+strings.ToLower(key), depth+1)
				}
			}
		case []any:
			for _, item := range typed {
				walk(item, context, depth+1)
			}
		}
	}
	walk(root, "", 0)
	if len(body) == 0 && len(fallback) > 0 {
		// An unexpected layout must never hide the whole answer.
		body = slices.Compact(fallback)
	}
	parts.Text = strings.Join(body, "\n\n")
	if len(parts.Text) > canBotRichTextMaxBytes {
		parts.Text = parts.Text[:canBotRichTextMaxBytes]
	}
	return parts
}

// canUnifiedText extracts readable text from a unified response JSON payload.
func canUnifiedText(data []byte) string {
	return canUnifiedParse(data).Text
}

// addMetadata adds typed sources and suggestions from bot metadata.
func (p *canBotAnswerParts) addMetadata(msgs ...*waE2E.Message) {
	for _, msg := range msgs {
		meta := msg.GetMessageContextInfo().GetBotMetadata()
		if meta == nil {
			continue
		}
		for _, source := range meta.GetRichResponseSourcesMetadata().GetSources() {
			p.addLink(source.GetSourceTitle(), source.GetSourceProviderURL())
		}
		suggested := meta.GetSuggestedPromptMetadata()
		for _, prompt := range suggested.GetSuggestedPrompts() {
			p.addPrompt(prompt)
		}
		for _, suggestion := range suggested.GetPromptSuggestions().GetSuggestions() {
			p.addPrompt(suggestion.GetPrompt())
		}
	}
}

// canBotAnswer collects text, links and suggestions of a bot message.
func canBotAnswer(msg *waE2E.Message) canBotAnswerParts {
	inner := canUnwrapBotMessage(msg)
	edited := inner.GetProtocolMessage().GetEditedMessage()
	parts := canUnifiedParse(canBotRich(msg).GetUnifiedResponse().GetData())
	parts.Text = canBotText(msg)
	parts.addMetadata(msg, inner, edited, canUnwrapBotMessage(edited))
	return parts
}

// canComposeBotAnswer renders the answer with its links and suggestions as
// plain lists, readable by screen readers and copyable in any XMPP client.
func canComposeBotAnswer(parts canBotAnswerParts) string {
	if strings.TrimSpace(parts.Text) == "" {
		return ""
	}
	var b strings.Builder
	b.WriteString(parts.Text)
	if len(parts.Links) > 0 {
		b.WriteString("\n\nEnlaces:")
		for i, link := range parts.Links {
			b.WriteString(fmt.Sprintf("\n%d. ", i+1))
			if link.Title != "" && link.Title != link.URL {
				b.WriteString(link.Title + ": ")
			}
			b.WriteString(link.URL)
		}
	}
	if len(parts.Prompts) > 0 {
		b.WriteString("\n\nSugerencias:")
		for _, prompt := range parts.Prompts {
			b.WriteString("\n• " + prompt)
		}
	}
	return b.String()
}

func canRichResponseText(rich *waE2E.AIRichResponseMessage) string {
	if rich == nil {
		return ""
	}
	if text := canRichSubmessagesText(rich); text != "" {
		return text
	}
	return canUnifiedText(rich.GetUnifiedResponse().GetData())
}

func canRichSubmessagesText(rich *waE2E.AIRichResponseMessage) string {
	var parts []string
	size := 0
	add := func(text string) bool {
		text = strings.TrimSpace(text)
		if text == "" {
			return true
		}
		if size+len(text) > canBotRichTextMaxBytes {
			return false
		}
		parts = append(parts, text)
		size += len(text)
		return true
	}
	for _, sub := range rich.GetSubmessages() {
		ok := true
		switch sub.GetMessageType() {
		case waAICommonDeprecated.AIRichResponseSubMessageType_AI_RICH_RESPONSE_CODE:
			code := sub.GetCodeMetadata()
			var b strings.Builder
			for _, block := range code.GetCodeBlocks() {
				b.WriteString(block.GetCodeContent())
			}
			if b.Len() > 0 {
				ok = add("```" + code.GetCodeLanguage() + "\n" + b.String() + "\n```")
			}
		case waAICommonDeprecated.AIRichResponseSubMessageType_AI_RICH_RESPONSE_TABLE:
			table := sub.GetTableMetadata()
			lines := []string{}
			if title := strings.TrimSpace(table.GetTitle()); title != "" {
				lines = append(lines, title)
			}
			for _, row := range table.GetRows() {
				lines = append(lines, strings.Join(row.GetItems(), " | "))
			}
			ok = add(strings.Join(lines, "\n"))
		default:
			ok = add(sub.GetMessageText())
		}
		if !ok {
			break
		}
	}
	return strings.Join(parts, "\n\n")
}

// canBotMessageEvent applies the streaming contract to a bot message. handled
// reports whether the caller must return (kind, payload) instead of continuing.
// When not handled, evt may be replaced by its unwrapped content.
func canBotMessageEvent(client *whatsmeow.Client, evt *events.Message, message *Message) (*events.Message, EventKind, *EventPayload, bool) {
	stage := evt.Info.MsgBotInfo.EditType
	inner := canUnwrapBotMessage(evt.Message)
	text := canComposeBotAnswer(canBotAnswer(evt.Message))
	if canIsPlainBotPrompt(evt) {
		text = canBotText(evt.Message) // our own prompts are never decorated
	}
	rich := canBotRich(evt.Message)
	edit := inner.GetProtocolMessage()
	if message.Chat.JID == "" {
		return evt, EventUnknown, nil, true
	}
	// Muse approvals travel as hatch metadata sync events; other sync events are internal.
	if hatch := edit.GetAiMetadataOperation().GetHatchMetadataSync(); hatch != nil {
		if !evt.Info.IsFromMe {
			if kind, payload, ok := canHatchApprovalEvent(message, hatch.GetData()); ok {
				return evt, kind, payload, true
			}
		}
		return evt, EventUnknown, nil, true
	}
	// The answer being edited: explicit bot target, else the protocol edit key.
	target := evt.Info.MsgBotInfo.EditTargetID
	if target == "" && edit != nil && edit.GetType() == waE2E.ProtocolMessage_MESSAGE_EDIT {
		target = edit.GetKey().GetID()
	}
	switch {
	case stage == types.EditTypeInner:
		// Partial updates would flood readers; "last"/"full" carry the full answer.
		return evt, EventUnknown, nil, true
	case stage == types.EditTypeLast || stage == types.EditTypeFull ||
		(edit != nil && edit.GetType() == waE2E.ProtocolMessage_MESSAGE_EDIT):
		// Never forward an empty correction: it would erase the visible answer.
		if text == "" || target == "" {
			return evt, EventUnknown, nil, true
		}
		message.Body = text
		if canBotAnswers.seen(target) {
			message.Kind = MessageEdit
			message.ReferenceID = target
		} else {
			// The original never reached XMPP (e.g. an empty "first"): publish the
			// answer under its ID so later edits correct it instead of vanishing.
			message.Kind = MessagePlain
			message.ID = target
			canBotAnswers.add(target)
		}
		return evt, EventMessage, &EventPayload{Message: *message}, true
	}
	if rich != nil && text == "" && message.Body == "" {
		// A rich answer without readable text yet; a later stage will carry it.
		return evt, EventUnknown, nil, true
	}
	canBotAnswers.add(message.ID)
	if inner != evt.Message {
		unwrapped := *evt
		unwrapped.Message = inner
		evt = &unwrapped
	}
	if message.Body == "" && evt.Message.GetExtendedTextMessage() == nil {
		message.Body = text
	}
	return evt, EventUnknown, nil, false
}

// canBotAnswerSet remembers recently forwarded bot answers (bounded FIFO).
type canBotAnswerSet struct {
	mutex sync.Mutex
	ids   map[string]struct{}
	order []string
}

const canBotAnswerMax = 512

var canBotAnswers = &canBotAnswerSet{ids: map[string]struct{}{}}

func (s *canBotAnswerSet) seen(id string) bool {
	s.mutex.Lock()
	defer s.mutex.Unlock()
	_, ok := s.ids[id]
	return ok
}

func (s *canBotAnswerSet) add(id string) {
	if id == "" {
		return
	}
	s.mutex.Lock()
	defer s.mutex.Unlock()
	if _, ok := s.ids[id]; ok {
		return
	}
	s.ids[id] = struct{}{}
	s.order = append(s.order, id)
	if len(s.order) > canBotAnswerMax {
		delete(s.ids, s.order[0])
		s.order = s.order[1:]
	}
}

// canIsPlainBotPrompt reports our own message to a bot, which carries no answer metadata.
func canIsPlainBotPrompt(evt *events.Message) bool {
	return evt.Info.IsFromMe && evt.Info.MsgBotInfo.EditType == ""
}

// canShortError bounds an error for logs.
func canShortError(err error) string {
	text := err.Error()
	if len(text) > 160 {
		text = text[:160]
	}
	return text
}
