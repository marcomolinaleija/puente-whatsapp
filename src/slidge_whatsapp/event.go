package whatsapp

import (
	// Standard library.
	"archive/zip"
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"mime"
	"slices"
	"strings"
	"time"

	// Internal packages.
	"codeberg.org/slidge/slidge-whatsapp/slidge_whatsapp/media"

	// Third-party libraries.
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/proto/waSyncAction"
	"go.mau.fi/whatsmeow/proto/waWeb"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

// EventKind represents all event types recognized by the Python session adapter, as emitted by the
// Go session adapter.
type EventKind int

// The event types handled by the overarching session adapter handler.
const (
	EventUnknown EventKind = iota
	EventQRCode
	EventPairDeviceID
	EventConnect
	EventLoggedOut
	EventContact
	EventPresence
	EventMessage
	EventChatState
	EventReceipt
	EventGroup
	EventCall
	EventAvatar
)

// EventPayload represents the collected payloads for all event types handled by the overarching
// session adapter handler. Only specific fields will be populated in events emitted by internal
// handlers, see documentation for specific types for more information.
type EventPayload struct {
	QRCode       string
	PairDeviceID string
	Connect      Connect
	LoggedOut    LoggedOut
	Contact      Contact
	Presence     Presence
	Message      Message
	ChatState    ChatState
	Receipt      Receipt
	Group        Group
	Call         Call
	Avatar       Avatar
}

// HandleEventFunc represents a handler for incoming events sent to the Python adapter, accepting an
// event type and payload.
type HandleEventFunc func(EventKind, *EventPayload)

// Connect represents event data related to a connection to WhatsApp being established, or failing
// to do so (based on the [Connect.Error] result).
type Connect struct {
	JID   string // The device JID given for this connection.
	Error string // The connection error, if any.
}

// LoggedOut repreents event data related to an explicit or implicit log-out event.
type LoggedOut struct {
	Reason string // The human-readable reason for logging out, if any.
}

// A Avatar represents a small image set for a Contact or Group.
type Avatar struct {
	ID  string // The unique ID for this avatar, used for persistent caching.
	URL string // The HTTP URL over which this avatar might be retrieved. Can change for the same ID.

	ResourceID string // JID of the group or contact this avatar concerns
	IsGroup    bool   // Whether this JID is a group or a contact
}

// A Contact represents any entity that be communicated with directly in WhatsApp. This typically
// represents people, but may represent a business or bot as well, but not a group-chat.
type Contact struct {
	Actor    Actor
	Name     string // The user-set, human-readable name for this contact.
	IsFriend bool   // Whether this contact is in the user's contact list.
}

// NewContactEvent returns event data meant for [Session.propagateEvent] for a "live contact" event
func storedContactInfo(
	ctx context.Context,
	client *whatsmeow.Client,
	actor Actor,
	jids ...types.JID,
) types.ContactInfo {
	candidates := make([]types.JID, 0, len(jids)+1)
	if actor.JID != "" {
		if phoneJID, err := types.ParseJID(actor.JID); err == nil {
			candidates = append(candidates, phoneJID)
		}
	}
	candidates = append(candidates, jids...)

	seen := make(map[string]struct{}, len(candidates))
	var fallback types.ContactInfo
	for _, jid := range candidates {
		jid = jid.ToNonAD()
		key := jid.String()
		if jid.IsEmpty() || key == "" {
			continue
		}
		if _, ok := seen[key]; ok {
			continue
		}
		seen[key] = struct{}{}

		info, err := client.Store.Contacts.GetContact(ctx, jid)
		if err != nil {
			client.Log.Warnf("Could not load stored contact info for %s: %v", jid, err)
			continue
		}
		if !info.Found {
			continue
		}
		if !fallback.Found {
			fallback = info
		}
		if info.FullName != "" || info.FirstName != "" {
			return info
		}
	}
	return fallback
}

func newContactEvent(
	ctx context.Context,
	client *whatsmeow.Client,
	evt *events.Contact,
) (EventKind, *EventPayload) {
	lid, errlid := types.ParseJID(evt.Action.GetLidJID())
	jid, errjid := types.ParseJID(evt.Action.GetPnJID())

	if errlid != nil && errjid != nil {
		client.Log.Warnf("Ignoring contact event: %s (LID) %s (JID)", errlid, errjid)
		return EventUnknown, nil
	}

	actor := newActor(ctx, client, evt.JID, lid, jid)
	contactInfo := storedContactInfo(ctx, client, actor, evt.JID, jid, lid)
	if fullName := evt.Action.GetFullName(); fullName != "" {
		contactInfo.FullName = fullName
	}
	if firstName := evt.Action.GetFirstName(); firstName != "" {
		contactInfo.FirstName = firstName
	}
	if pushName := evt.Action.GetUsername(); pushName != "" {
		contactInfo.PushName = pushName
	}
	contact := newContact(client, actor, contactInfo)
	return EventContact, &EventPayload{Contact: contact}
}

// NewContactEventFromHistory returns event data meant for [Session.propagateEvent] for a "history push name sync" event
func newContactEventFromHistory(
	ctx context.Context,
	client *whatsmeow.Client,
	evt *waHistorySync.Pushname,
) (EventKind, *EventPayload) {
	jid, _ := types.ParseJID(evt.GetID())
	actor := newActor(ctx, client, jid)
	contactInfo := storedContactInfo(ctx, client, actor, jid)
	contactInfo.PushName = evt.GetPushname()
	contact := newContact(client, actor, contactInfo)
	return EventContact, &EventPayload{Contact: contact}
}

// NewContactEvent returns event data meant for [Session.propagateEvent] for a "live pushname" event
func newContactEventFromPushName(ctx context.Context, client *whatsmeow.Client, evt *events.PushName) (EventKind, *EventPayload) {
	contactInfo, err := client.Store.Contacts.GetContact(ctx, evt.JID)
	if err != nil {
		contactInfo, _ = client.Store.Contacts.GetContact(ctx, evt.JIDAlt)
	}
	contactInfo.PushName = evt.NewPushName
	actor := newActor(ctx, client, evt.JID, evt.JIDAlt)
	return EventContact, &EventPayload{Contact: newContact(client, actor, contactInfo)}
}

// NewContact returns a concrete [Contact] instance for the JID and additional information given.
// In cases where a valid contact can't be returned, [Contact.JID] will be left empty.
func newContact(client *whatsmeow.Client, actor Actor, info types.ContactInfo) Contact {
	var contact = Contact{
		Actor:    actor,
		IsFriend: info.FullName != "", // Only trusted contacts have full names attached on WhatsApp.
	}

	// Find valid contact name from list of alternatives, or return empty contact if none could be found.
	for _, n := range []string{info.FullName, info.FirstName, info.BusinessName, info.PushName, info.RedactedPhone} {
		if n != "" {
			contact.Name = n
			break
		}
	}

	if contact.Name == "" {
		client.Log.Warnf("Could not find a name for this contact: %+v", info)
		return Contact{}
	}

	return contact
}

// Chat identifies a contact or a group, in other words, the "conversation" where an event takes
// place.
type Chat struct {
	JID     string
	IsGroup bool
}

// Actor identifies who has triggered the event. It is either a contact identified by a proper JID
// (in 1:1) or a participant identified by a LID (in groups).
const privateReplyGroupSeparator = "\x1f"

type Actor struct {
	JID  string
	LID  string
	IsMe bool
}

// PresenceKind represents the different kinds of activity states possible in WhatsApp.
type PresenceKind int

// The presences handled by the overarching session event handler.
const (
	PresenceUnknown PresenceKind = iota
	PresenceAvailable
	PresenceUnavailable
)

// Precence represents a contact's general state of activity, and is periodically updated as
// contacts start or stop paying attention to their client of choice.
type Presence struct {
	Actor    Actor
	Kind     PresenceKind
	LastSeen int64
}

// NewPresenceEvent returns event data meant for [Session.propagateEvent] for the primitive presence
// event given.
func newPresenceActor(client *whatsmeow.Client, jid types.JID) Actor {
	var actor Actor
	switch jid.Server {
	case types.DefaultUserServer:
		actor.JID = jid.ToNonAD().String()
		if client != nil && client.Store != nil {
			actor.IsMe = client.Store.GetJID().ToNonAD() == jid.ToNonAD()
		}
	case types.HiddenUserServer:
		actor.LID = jid.ToNonAD().String()
		if client != nil && client.Store != nil {
			actor.IsMe = client.Store.GetLID().ToNonAD() == jid.ToNonAD()
		}
	}
	return actor
}

func newPresenceEvent(ctx context.Context, client *whatsmeow.Client, evt *events.Presence) (EventKind, *EventPayload) {
	var presence = Presence{
		Actor:    newPresenceActor(client, evt.From),
		Kind:     PresenceAvailable,
		LastSeen: evt.LastSeen.Unix(),
	}

	if evt.Unavailable {
		presence.Kind = PresenceUnavailable
	}

	return EventPresence, &EventPayload{Presence: presence}
}

// MessageKind represents all concrete message types (plain-text messages, edit messages, reactions)
// recognized by the Python session adapter.
type MessageKind int

// The message types handled by the overarching session event handler.
const (
	MessagePlain MessageKind = iota
	MessageEdit
	MessageRevoke
	MessageReaction
	MessageAttachment
	MessagePoll
)

// A Message represents one of many kinds of bidirectional communication payloads, for example, a
// text message, a file (image, video) attachment, an emoji reaction, etc. Messages of different
// kinds are denoted as such, and re-use fields where the semantics overlap.
type Message struct {
	Kind        MessageKind  // The concrete message kind being sent or received.
	ID          string       // The unique message ID, used for referring to a specific Message instance.
	Actor       Actor        // Identifies who sent this message
	Chat        Chat         // The JID of the group or contact.
	OriginActor Actor        // Identifies who sent a message being referred to for reaction, replies and moderation
	Body        string       // The plain-text message body. For attachment messages, this can be a caption.
	Timestamp   int64        // The Unix timestamp denoting when this message was created.
	IsForwarded bool         // Whether or not the message was forwarded from another source.
	ReplyID     string       // The unique message ID this message is in reply to, if any.
	ReplyBody   string       // The full body of the message this message is in reply to, if any.
	Attachments []Attachment // The list of file (image, video, etc.) attachments contained in this message.
	Preview     Preview      // A short description for the URL provided in the message body, if any.
	Location    Location     // The location metadata for messages, if any.
	Poll        Poll         // The multiple-choice poll contained in the message, if any.
	Album       Album        // The image album message, if any.
	GroupInvite Group        // Group information for the invite group included in this message, if any.
	MentionJIDs []string     // A list of JIDs mentioned in this message, if any.
	Receipts    []Receipt    // The receipt statuses for the message, typically provided alongside historical messages.
	Reactions   []Message    // Reactions attached to message, typically provided alongside historical messages.
	IsHistory   bool         // Whether or not the message is derived from message history.
	ReferenceID string       // A message referenced in this message, for edits.
}

// A Attachment represents additional binary data (e.g. images, videos, documents) provided alongside
// a message, for display or storage on the recepient client.
type Attachment struct {
	MIME     string // The MIME type for attachment.
	Filename string // The recommended file name for this attachment. May be an auto-generated name.
	Caption  string // The user-provided caption, provided alongside this attachment.
	Data     []byte // Data for the attachment.
	ViewOnce bool   // Whether an audio attachment must be playable only once.

	// Internal fields.
	spec *media.Spec // Metadata specific to audio/video files, used in processing.
}

// GetSpec returns metadata for this attachment, as derived from the underlying attachment data.
func (a *Attachment) GetSpec(ctx context.Context) (*media.Spec, error) {
	if a.spec != nil {
		return a.spec, nil
	}

	spec, err := media.GetSpec(ctx, a.Data)
	if err != nil {
		return nil, err
	}

	a.spec = spec
	return a.spec, nil
}

// PreviewKind represents different ways of previewingadditional data inline with messages.
type PreviewKind int

const (
	PreviewPlain PreviewKind = iota
	PreviewVideo
)

// A Preview represents a short description for a URL provided in a message body, as usually derived
// from the content of the page pointed at.
type Preview struct {
	Kind        PreviewKind // The kind of preview to show, defaults to plain URL preview.
	URL         string      // The original (or canonical) URL this preview was generated for.
	Title       string      // The short title for the URL preview.
	Description string      // The (optional) long-form description for the URL preview.
	Thumbnail   []byte      // The (optional) thumbnail image data.
}

// A Location represents additional metadata given to location messages.
type Location struct {
	Latitude  float64
	Longitude float64
	Accuracy  int
	IsLive    bool

	// Optional fields given for named locations.
	Name    string
	Address string
	URL     string
}

// A Poll represents a multiple-choice question, on which each choice might be voted for one or more
// times.
type Poll struct {
	Title   string       // The human-readable name of this poll.
	Options []PollOption // The list of choices to vote on in the poll.
}

// A PollOption represents an individual choice within a broader poll.
type PollOption struct {
	Title string // The human-readable name for the poll option.
}

// A Album message represents a collection of media files, typically images and videos.
type Album struct {
	IsAlbum    bool // Whether or not the message is an album, regardless of calculated media counts.
	ImageCount int  // The calculated amount of images in the album, might not be accurate.
	VideoCount int  // The calculated amount of videos in the album, might not be accurate.
}

func pollSelectableCount(poll *waE2E.PollCreationMessage, zeroValueFallback int) int {
	optionCount := len(poll.GetOptions())
	selectableCount := int(poll.GetSelectableOptionsCount())
	if selectableCount <= 0 || selectableCount > optionCount {
		selectableCount = zeroValueFallback
	}
	if selectableCount <= 0 || selectableCount > optionCount {
		selectableCount = optionCount
	}
	return selectableCount
}

func pollSelectionMode(poll *waE2E.PollCreationMessage, zeroValueFallback int) string {
	selectableCount := int(poll.GetSelectableOptionsCount())
	if selectableCount > 1 || (selectableCount == 0 && zeroValueFallback == 0) {
		return "multiple"
	}
	return "single"
}

func setPollUpdateMessage(message *Message, update *waE2E.PollUpdateMessage, vote *waE2E.PollVoteMessage) bool {
	if update == nil || vote == nil {
		return false
	}
	referenceID := update.GetPollCreationMessageKey().GetID()
	if referenceID == "" {
		return false
	}

	message.Kind = MessagePoll
	message.ReferenceID = referenceID
	message.Poll = Poll{}
	for _, optionHash := range vote.GetSelectedOptions() {
		message.Poll.Options = append(message.Poll.Options, PollOption{
			Title: hex.EncodeToString(optionHash),
		})
	}
	return true
}

// NewMessageEvent returns event data meant for [Session.propagateEvent] for the primive message
// event given. Unknown or invalid messages will return an [EventUnknown] event with nil data.
func newMessageEvent(ctx context.Context, client *whatsmeow.Client, evt *events.Message) (EventKind, *EventPayload) {
	// Set basic data for message, to be potentially amended depending on the concrete version of
	// the underlying message.
	var message = Message{
		Kind:      MessagePlain,
		ID:        evt.Info.ID,
		Actor:     newActor(ctx, client, evt.Info.Sender, evt.Info.SenderAlt),
		Body:      evt.Message.GetConversation(),
		Timestamp: evt.Info.Timestamp.Unix(),
	}

	message.Chat = newChat(ctx, client, evt.Info.Chat, evt.Info.IsGroup)
	message.Actor.IsMe = evt.Info.IsFromMe

	if evt.Info.Chat.Server == types.BroadcastServer {
		// Handle non-carbon, non-status broadcast messages as plain messages; support for other
		// types is lacking in the XMPP world.
		if evt.Info.Chat.User == types.StatusBroadcastJID.User || message.Actor.IsMe {
			return EventUnknown, nil
		}
	}

	// Handle handle protocol messages (such as message deletion or editing).
	if p := evt.Message.GetProtocolMessage(); p != nil {
		switch p.GetType() {
		case waE2E.ProtocolMessage_MESSAGE_EDIT:
			if m := p.GetEditedMessage(); m != nil {
				message.Kind = MessageEdit
				message.Body = m.GetConversation()
				message.ReferenceID = p.Key.GetID()
			} else {
				return EventUnknown, nil
			}
		case waE2E.ProtocolMessage_REVOKE:
			originJID, err := types.ParseJID(p.Key.GetParticipant())
			if err != nil {
				return EventUnknown, nil
			}
			message.Kind = MessageRevoke
			message.ID = p.Key.GetID()
			message.OriginActor = newActor(ctx, client, originJID)
			return EventMessage, &EventPayload{Message: message}
		}
	}

	// Handle emoji reaction to existing message.
	if r := evt.Message.GetReactionMessage(); r != nil {
		message.Kind = MessageReaction
		message.ID = r.Key.GetID()
		message.Body = r.GetText()
		return EventMessage, &EventPayload{Message: message}
	}

	// Handle location (static and live) message.
	if l := evt.Message.GetLocationMessage(); l != nil {
		message.Location = Location{
			Latitude:  l.GetDegreesLatitude(),
			Longitude: l.GetDegreesLongitude(),
			Accuracy:  int(l.GetAccuracyInMeters()),
			IsLive:    l.GetIsLive(),
			Name:      l.GetName(),
			Address:   l.GetAddress(),
			URL:       l.GetURL(),
		}
		return EventMessage, &EventPayload{Message: message}
	}

	if l := evt.Message.GetLiveLocationMessage(); l != nil {
		message.Body = l.GetCaption()
		message.Location = Location{
			Latitude:  l.GetDegreesLatitude(),
			Longitude: l.GetDegreesLongitude(),
			Accuracy:  int(l.GetAccuracyInMeters()),
			IsLive:    true,
		}
		return EventMessage, &EventPayload{Message: message}
	}

	// Handle poll vote updates before poll creation messages.
	if update := evt.Message.GetPollUpdateMessage(); update != nil {
		client.Log.Infof(
			"Received WhatsApp poll update event=%s reference=%s chat=%s sender=%s from_me=%t",
			message.ID,
			update.GetPollCreationMessageKey().GetID(),
			message.Chat.JID,
			message.Actor.JID,
			message.Actor.IsMe,
		)
		vote, err := client.DecryptPollVote(ctx, evt)
		if err != nil {
			client.Log.Warnf("Failed decrypting poll vote '%s': %s", message.ID, err)
			return EventUnknown, nil
		}
		if !setPollUpdateMessage(&message, update, vote) {
			return EventUnknown, nil
		}
		return EventMessage, &EventPayload{Message: message}
	}

	// Handle poll messages.
	pollVersions := []struct {
		poll              *waE2E.PollCreationMessage
		zeroValueFallback int
	}{
		{evt.Message.GetPollCreationMessageV3(), 1},
		{evt.Message.GetPollCreationMessageV2(), 1},
		{evt.Message.GetPollCreationMessage(), 0},
	}
	for _, version := range pollVersions {
		p := version.poll
		if p == nil {
			continue
		}
		message.Kind = MessagePoll
		selectableCount := pollSelectableCount(p, version.zeroValueFallback)
		selectionMode := pollSelectionMode(p, version.zeroValueFallback)
		message.Body = fmt.Sprintf("%s:%d", selectionMode, selectableCount)
		message.Poll = Poll{Title: p.GetName()}
		for _, o := range p.GetOptions() {
			message.Poll.Options = append(message.Poll.Options, PollOption{
				Title: o.GetOptionName(),
			})
		}
		return EventMessage, &EventPayload{Message: message}
	}

	// Handle "album" messages, denoting a grouping of media messages to follow.
	if a := evt.Message.GetAlbumMessage(); a != nil {
		message.Album = Album{
			IsAlbum:    true,
			ImageCount: int(a.GetExpectedImageCount()),
			VideoCount: int(a.GetExpectedVideoCount()),
		}
		return EventMessage, &EventPayload{Message: message}
	}

	// Handle message attachments, if any.
	if attach, context, err := getMessageAttachments(ctx, client, evt.Message); err != nil {
        client.Log.Warnf("CAN_INBOUND attachment_unavailable class=%s", canAttachmentFailureClass(err))
        message = canUnavailableAttachment(message, evt.Message)
        if context != nil { message = getMessageWithContext(ctx,client,message,context) }
	} else if len(attach) > 0 {
		message.Attachments = append(message.Attachments, attach...)
		message.Kind = MessageAttachment
		if context != nil {
			message = getMessageWithContext(ctx, client, message, context)
		}
	}

	// Get extended information from message, if available. Extended messages typically represent
	// messages with additional context, such as replies, forwards, etc.
	if e := evt.Message.GetExtendedTextMessage(); e != nil {
		if message.Body == "" {
			message.Body = e.GetText()
		}

		message.Preview = Preview{
			URL:         e.GetMatchedText(),
			Description: e.GetDescription(),
			Title:       e.GetTitle(),
			Thumbnail:   e.GetJPEGThumbnail(),
		}

		message = getMessageWithContext(ctx, client, message, e.GetContextInfo())
	}

	// Ignore obviously invalid messages.
	if message.Kind == MessagePlain && message.Body == "" {
		return EventUnknown, nil
	}

	return EventMessage, &EventPayload{Message: message}
}

// GetMessageWithContext processes the given [Message] and applies any context metadata might be
// useful; examples of context include messages being quoted. If no context is found, the original
// message is returned unchanged.
func getMessageWithContext(ctx context.Context, client *whatsmeow.Client, message Message, info *waE2E.ContextInfo) Message {
	if info == nil {
		return message
	}

	message.IsForwarded = info.GetIsForwarded()
	// Preserve the native reference before resolving the quoted participant.
	// Self-sent WhatsApp messages can carry a stanza ID without a participant.
	message.ReplyID = info.GetStanzaID()
	if q := info.GetQuotedMessage(); q != nil {
		if qe := q.GetExtendedTextMessage(); qe != nil {
			message.ReplyBody = qe.GetText()
		} else {
			message.ReplyBody = q.GetConversation()
		}
	}

	remoteJID, _ := types.ParseJID(info.GetRemoteJID())
	originJID, err := types.ParseJID(info.GetParticipant())
	if err != nil {
		return message
	}

	message.OriginActor = newActor(ctx, client, originJID, remoteJID)
	if remoteJID.Server == types.GroupServer && !message.Chat.IsGroup {
		message.OriginActor.LID += privateReplyGroupSeparator + remoteJID.ToNonAD().String()
	}

	return message
}

const (
	stickerCaptionMarker          = "\x1eWHATSAPP_STICKER\x1f"
	lottieStickerMetadataPath     = "animation/animation.json.overridden_metadata"
	maxLottieStickerPackageBytes  = 5 * 1024 * 1024
	maxLottieStickerMetadataBytes = 64 * 1024
	maxLottieStickerArchiveFiles  = 32
)

type lottieStickerMetadata struct {
	AccessibilityText string   `json:"accessibility-text"`
	Emojis            []string `json:"emojis"`
}

func stickerAccessibilityCaption(label, emojis string) string {
	if label = strings.TrimSpace(label); label != "" {
		return label
	}
	return strings.TrimSpace(emojis)
}

func lottieStickerAccessibilityCaption(data []byte) string {
	if len(data) == 0 || len(data) > maxLottieStickerPackageBytes {
		return ""
	}

	archive, err := zip.NewReader(bytes.NewReader(data), int64(len(data)))
	if err != nil || len(archive.File) > maxLottieStickerArchiveFiles {
		return ""
	}

	for _, file := range archive.File {
		if file.Name != lottieStickerMetadataPath {
			continue
		}
		if file.UncompressedSize64 == 0 || file.UncompressedSize64 > maxLottieStickerMetadataBytes {
			return ""
		}

		reader, err := file.Open()
		if err != nil {
			return ""
		}
		payload, readErr := io.ReadAll(io.LimitReader(reader, maxLottieStickerMetadataBytes+1))
		closeErr := reader.Close()
		if readErr != nil || closeErr != nil || len(payload) > maxLottieStickerMetadataBytes {
			return ""
		}

		var metadata lottieStickerMetadata
		if json.Unmarshal(payload, &metadata) != nil {
			return ""
		}
		if label := strings.TrimSpace(metadata.AccessibilityText); label != "" {
			return label
		}

		emojis := make([]string, 0, len(metadata.Emojis))
		for _, emoji := range metadata.Emojis {
			if emoji = strings.TrimSpace(emoji); emoji != "" {
				emojis = append(emojis, emoji)
			}
		}
		return strings.Join(emojis, " ")
	}
	return ""
}

// GetMessageAttachments fetches and decrypts attachments (images, audio, video, or documents) sent
// via WhatsApp. Any failures in retrieving any attachment will return an error immediately.
// media attachment context is propagated
func getMessageAttachments(ctx context.Context, client *whatsmeow.Client, message *waE2E.Message) ([]Attachment, *waE2E.ContextInfo, error) {
    // CAN inbound isolation v1
    ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
    defer cancel()
	if pack := message.GetStickerPackMessage(); pack != nil {
		attachment := receiveStickerPack(ctx, client.Download, pack)
		return []Attachment{attachment}, pack.GetContextInfo(), nil
	}
	var result []Attachment
	var info *waE2E.ContextInfo
	var kinds = []whatsmeow.DownloadableMessage{
		message.GetImageMessage(),
		message.GetAudioMessage(),
		message.GetVideoMessage(),
		message.GetDocumentMessage(),
		message.GetStickerMessage(),
		message.GetPtvMessage(),
	}

	for _, msg := range kinds {
		// Handle data for specific attachment type.
		var a Attachment
		switch msg := msg.(type) {
		case *waE2E.ImageMessage:
			a.MIME, a.Caption = msg.GetMimetype(), msg.GetCaption()
			info = msg.GetContextInfo()
		case *waE2E.AudioMessage:
			// Preserve incoming WhatsApp voice notes as their original Ogg/Opus payload.
			// The XMPP client supports Opus directly, so transcoding to AAC only adds loss.
			a.MIME = msg.GetMimetype()
			info = msg.GetContextInfo()
		case *waE2E.VideoMessage:
			a.MIME, a.Caption = msg.GetMimetype(), msg.GetCaption()
			info = msg.GetContextInfo()
		case *waE2E.DocumentMessage:
			a.MIME, a.Caption, a.Filename = msg.GetMimetype(), msg.GetCaption(), msg.GetFileName()
			info = msg.GetContextInfo()
		case *waE2E.StickerMessage:
			a.MIME = msg.GetMimetype()
			a.Caption = stickerCaptionMarker + stickerAccessibilityCaption(
				msg.GetAccessibilityLabel(),
				msg.GetEmojis(),
			)
			info = msg.GetContextInfo()
		}

		// Ignore attachments with empty or unknown MIME types.
		if a.MIME == "" {
			continue
		}

		// Attempt to download and decrypt raw attachment data, if any.
		data, err := client.Download(ctx, msg)
		if err != nil {
			return nil, info, err
		}

		a.Data = data
		if strings.HasPrefix(a.Caption, stickerCaptionMarker) &&
			strings.TrimSpace(strings.TrimPrefix(a.Caption, stickerCaptionMarker)) == "" {
			a.Caption = stickerCaptionMarker + lottieStickerAccessibilityCaption(data)
		}

		// Set filename from SHA256 checksum and MIME type, if none is already set.
		if a.Filename == "" {
			a.Filename = fmt.Sprintf("%x%s", msg.GetFileSHA256(), extensionByType(a.MIME))
		}

		result = append(result, a)
	}

	// Handle any contact vCard as attachment.
	if c := message.GetContactMessage(); c != nil {
		result = append(result, Attachment{
			MIME:     "text/vcard",
			Filename: c.GetDisplayName() + ".vcf",
			Data:     []byte(c.GetVcard()),
		})
		info = c.GetContextInfo()
	}

	return result, info, nil
}

const (
	// The MIME type used by voice messages on WhatsApp.
	voiceMessageMIME = string(media.TypeOgg) + "; codecs=opus"
	// The MIME type used by animated images on WhatsApp.
	animatedImageMIME = "image/gif"

	// The maximum image attachment size we'll attempt to process in any way, in bytes.
	maxConvertImageSize = 1024 * 1024 * 10 // 10MiB
	// The maximum audio/video attachment size we'll attempt to process in any way, in bytes.
	maxConvertAudioVideoSize = 1024 * 1024 * 20 // 20MiB

	// The maximum number of samples to return in media waveforms.
	maxWaveformSamples = 64
)

var (
	// Default target specification for voice messages.
	voiceMessageSpec = media.Spec{
		MIME:            media.MIMEType(voiceMessageMIME),
		AudioBitRate:    64,
		AudioChannels:   1,
		AudioSampleRate: 48000,
		StripMetadata:   true,
	}

	// Default target specification for generic audio messages.
	audioMessageSpec = media.Spec{
		MIME:            media.TypeM4A,
		AudioBitRate:    160,
		AudioSampleRate: 44100,
	}

	// Default target specification for video messages with inline preview.
	videoMessageSpec = media.Spec{
		MIME:             media.TypeMP4,
		AudioBitRate:     160,
		AudioSampleRate:  44100,
		VideoFilter:      "pad=ceil(iw/2)*2:ceil(ih/2)*2",
		VideoFrameRate:   25,
		VideoPixelFormat: "yuv420p",
		StripMetadata:    true,
	}

	// Default target specification for image messages with inline preview.
	imageMessageSpec = media.Spec{
		MIME:         media.TypeJPEG,
		ImageQuality: 85,
	}

	// Default target specifications for default and preview-size thumbnails.
	defaultThumbnailSpec = media.Spec{
		MIME:          media.TypeJPEG,
		ImageWidth:    100,
		StripMetadata: true,
	}
	previewThumbnailSpec = media.Spec{
		MIME:          media.TypeJPEG,
		ImageWidth:    250,
		StripMetadata: true,
	}
)

// ConvertAttachment attempts to process a given attachment from a less-supported type to a
// canonically supported one; for example, from `image/png` to `image/jpeg`.
//
// Decisions about which MIME types to convert to are based on the concrete MIME type inferred from
// the file itself, and care is taken to conform to WhatsApp semantics for the given input MIME
// type.
//
// If the input MIME type is unknown, or conversion is impossible, the given attachment is not
// changed.
func convertAttachment(ctx context.Context, attach *Attachment) error {
	var detectedMIME media.MIMEType
	if t := media.DetectMIMEType(attach.Data); t != media.TypeUnknown {
		detectedMIME = t
		if attach.MIME == "" || attach.MIME == "application/octet-stream" {
			attach.MIME = string(detectedMIME)
		}
	}

	var spec media.Spec
	switch detectedMIME {
	case media.TypePNG, media.TypeWebP:
		// Convert common image formats to JPEG for inline preview.
		if len(attach.Data) > maxConvertImageSize {
			return fmt.Errorf("attachment size %d exceeds maximum of %d", len(attach.Data), maxConvertImageSize)
		}

		spec = imageMessageSpec
	case media.TypeGIF:
		// Convert GIFs to JPEG or MP4, if animated, as required by WhatsApp.
		if len(attach.Data) > maxConvertImageSize {
			return fmt.Errorf("attachment size %d exceeds maximum of %d", len(attach.Data), maxConvertImageSize)
		}

		spec = imageMessageSpec
		if s, err := attach.GetSpec(ctx); err == nil && s.ImageFrameRate > 0 {
			spec = videoMessageSpec
			spec.ImageFrameRate = s.ImageFrameRate
		}
	case media.TypeM4A:
		if len(attach.Data) > maxConvertAudioVideoSize {
			return fmt.Errorf("attachment size %d exceeds maximum of %d", len(attach.Data), maxConvertAudioVideoSize)
		}

		spec = voiceMessageSpec

		if s, err := attach.GetSpec(ctx); err == nil {
			if s.AudioCodec == "alac" {
				// Don't attempt to process lossless files at all, as it's assumed that the sender
				// wants to retain these characteristics. Since WhatsApp will try (and likely fail)
				// to process this as an audio message anyways, set a unique MIME type.
				attach.MIME = "application/octet-stream"
				return nil
			}
		}
	case media.TypeOgg:
		if len(attach.Data) > maxConvertAudioVideoSize {
			return fmt.Errorf("attachment size %d exceeds maximum of %d", len(attach.Data), maxConvertAudioVideoSize)
		}

		spec = audioMessageSpec
		if s, err := attach.GetSpec(ctx); err == nil {
			if s.AudioCodec == "opus" {
				// Assume that Opus-encoded Ogg files are meant to be voice messages, and re-encode
				// them as such for WhatsApp.
				spec = voiceMessageSpec
			}
		}
	case media.TypeMP4, media.TypeWebM:
		if len(attach.Data) > maxConvertAudioVideoSize {
			return fmt.Errorf("attachment size %d exceeds maximum of %d", len(attach.Data), maxConvertAudioVideoSize)
		}

		spec = videoMessageSpec

		if s, err := attach.GetSpec(ctx); err == nil {
			// Try to see if there's a video stream for ostensibly video-related MIME types, as
			// these are some times misdetected as such.
			if s.VideoWidth == 0 && s.VideoHeight == 0 && s.AudioSampleRate > 0 && s.Duration > 0 {
				spec = voiceMessageSpec
				spec.SourceMIME = media.TypeM4A
			}
		}
	default:
		// Detected source MIME not in list we're willing to convert, move on without error.
		return nil
	}

	// Convert attachment between file-types, if source MIME matches the known list of convertable types.
	if spec.SourceMIME == "" {
		spec.SourceMIME = detectedMIME
	}

	data, err := media.Convert(ctx, attach.Data, &spec)
	if err != nil {
		return fmt.Errorf("failed converting attachment: %w", err)
	}

	attach.Data, attach.MIME = data, string(spec.MIME)
	if i := strings.LastIndexByte(attach.Filename, '.'); i != -1 {
		attach.Filename = attach.Filename[:i] + extensionByType(attach.MIME)
	}

	return nil
}

// KnownMediaTypes represents MIME type to WhatsApp media types known to be handled by WhatsApp in a
// special way (that is, not as generic file uploads).
const nativeStickerMIME = "application/x-whatsapp-can-sticker"

var knownMediaTypes = map[string]whatsmeow.MediaType{
	"image/jpeg": whatsmeow.MediaImage,
	"audio/mpeg": whatsmeow.MediaAudio,
	"audio/mp4":  whatsmeow.MediaAudio,
	"audio/aac":  whatsmeow.MediaAudio,
	"audio/ogg":  whatsmeow.MediaAudio,
	"video/mp4":  whatsmeow.MediaVideo,
}

func uploadStickerAttachment(ctx context.Context, client *whatsmeow.Client, attach *Attachment) (*waE2E.Message, error) {
	if media.DetectMIMEType(attach.Data) != media.TypeWebP {
		return nil, fmt.Errorf("native WhatsApp stickers must be WebP")
	}
	spec, err := attach.GetSpec(ctx)
	if err != nil {
		return nil, fmt.Errorf("failed reading sticker metadata: %w", err)
	}
	if spec.ImageWidth < 1 || spec.ImageHeight < 1 {
		return nil, fmt.Errorf("sticker has no image dimensions")
	}
	attach.MIME = string(media.TypeWebP)
	upload, err := client.Upload(ctx, attach.Data, whatsmeow.MediaImage)
	if err != nil {
		return nil, err
	}
	return buildStickerMessage(attach, upload, spec), nil
}

func buildStickerMessage(attach *Attachment, upload whatsmeow.UploadResponse, spec *media.Spec) *waE2E.Message {
	mime := string(media.TypeWebP)
	animated := spec.ImageFrameRate > 0
	return &waE2E.Message{
		StickerMessage: &waE2E.StickerMessage{
			URL:                &upload.URL,
			DirectPath:         &upload.DirectPath,
			MediaKey:           upload.MediaKey,
			FileEncSHA256:      upload.FileEncSHA256,
			FileSHA256:         upload.FileSHA256,
			FileLength:         ptrTo(uint64(len(attach.Data))),
			Mimetype:           &mime,
			Width:              ptrTo(uint32(spec.ImageWidth)),
			Height:             ptrTo(uint32(spec.ImageHeight)),
			IsAnimated:         ptrTo(animated),
			AccessibilityLabel: ptrTo(strings.TrimSpace(attach.Caption)),
		},
	}
}

// UploadAttachment attempts to push the given attachment data to WhatsApp according to the MIME
// type specified within. Attachments are handled as generic file uploads unless they're of a
// specific format; in addition, certain MIME types may be automatically converted to a
// well-supported type via FFmpeg (if available).
func uploadAttachment(ctx context.Context, client *whatsmeow.Client, attach *Attachment) (*waE2E.Message, error) {
	if handled, payload, err := uploadCANAudioAttachment(ctx, client, attach); handled {
		return payload, err
	}
	if attach.MIME == nativePackMIME {
		return uploadNativeStickerPack(ctx, client, attach)
	}
	if attach.MIME == nativeStickerMIME {
		return uploadStickerAttachment(ctx, client, attach)
	}

	var originalMIME = attach.MIME
	if err := convertAttachment(ctx, attach); err != nil {
		client.Log.Warnf("failed to convert outgoing attachment: %s", err)
	}

	mediaType := knownMediaTypes[getBaseMediaType(attach.MIME)]
	if mediaType == "" {
		mediaType = whatsmeow.MediaDocument
	}

	if len(attach.Data) == 0 {
		return nil, fmt.Errorf("attachment file contains no data")
	}

	upload, err := client.Upload(ctx, attach.Data, mediaType)
	if err != nil {
		return nil, err
	}

	var message *waE2E.Message
	switch mediaType {
	case whatsmeow.MediaImage:
		message = &waE2E.Message{
			ImageMessage: &waE2E.ImageMessage{
				URL:           &upload.URL,
				DirectPath:    &upload.DirectPath,
				MediaKey:      upload.MediaKey,
				Mimetype:      &attach.MIME,
				FileEncSHA256: upload.FileEncSHA256,
				FileSHA256:    upload.FileSHA256,
				FileLength:    ptrTo(uint64(len(attach.Data))),
			},
		}
		t, err := media.Convert(ctx, attach.Data, &defaultThumbnailSpec)
		if err != nil {
			client.Log.Warnf("failed generating attachment thumbnail: %s", err)
		} else {
			message.ImageMessage.JPEGThumbnail = t
		}
	case whatsmeow.MediaAudio:
		spec, err := attach.GetSpec(ctx)
		if err != nil {
			client.Log.Warnf("failed fetching attachment metadata: %s", err)
			spec = &media.Spec{}
		}
		message = &waE2E.Message{
			AudioMessage: &waE2E.AudioMessage{
				URL:           &upload.URL,
				DirectPath:    &upload.DirectPath,
				MediaKey:      upload.MediaKey,
				Mimetype:      &attach.MIME,
				FileEncSHA256: upload.FileEncSHA256,
				FileSHA256:    upload.FileSHA256,
				FileLength:    ptrTo(uint64(len(attach.Data))),
				Seconds:       ptrTo(uint32(spec.Duration.Seconds())),
			},
		}
		if attach.MIME == voiceMessageMIME {
			message.AudioMessage.PTT = ptrTo(true)
			if attach.ViewOnce {
				message.AudioMessage.ViewOnce = ptrTo(true)
			}
			if spec != nil {
				w, err := media.GetWaveform(ctx, attach.Data, spec, maxWaveformSamples)
				if err != nil {
					client.Log.Warnf("failed generating attachment waveform: %s", err)
				} else {
					message.AudioMessage.Waveform = w
				}
			}
		}
	case whatsmeow.MediaVideo:
		spec, err := attach.GetSpec(ctx)
		if err != nil {
			client.Log.Warnf("failed fetching attachment metadata: %s", err)
			spec = &media.Spec{}
		}
		message = &waE2E.Message{
			VideoMessage: &waE2E.VideoMessage{
				URL:           &upload.URL,
				DirectPath:    &upload.DirectPath,
				MediaKey:      upload.MediaKey,
				Mimetype:      &attach.MIME,
				FileEncSHA256: upload.FileEncSHA256,
				FileSHA256:    upload.FileSHA256,
				FileLength:    ptrTo(uint64(len(attach.Data))),
				Seconds:       ptrTo(uint32(spec.Duration.Seconds())),
				Width:         ptrTo(uint32(spec.VideoWidth)),
				Height:        ptrTo(uint32(spec.VideoHeight)),
			},
		}
		t, err := media.Convert(ctx, attach.Data, &defaultThumbnailSpec)
		if err != nil {
			client.Log.Warnf("failed generating attachment thumbnail: %s", err)
		} else {
			message.VideoMessage.JPEGThumbnail = t
		}
		if originalMIME == animatedImageMIME {
			message.VideoMessage.GifPlayback = ptrTo(true)
		}
	case whatsmeow.MediaDocument:
		message = &waE2E.Message{
			DocumentMessage: &waE2E.DocumentMessage{
				URL:           &upload.URL,
				DirectPath:    &upload.DirectPath,
				MediaKey:      upload.MediaKey,
				Mimetype:      &attach.MIME,
				FileEncSHA256: upload.FileEncSHA256,
				FileSHA256:    upload.FileSHA256,
				FileLength:    ptrTo(uint64(len(attach.Data))),
				FileName:      &attach.Filename,
			},
		}
		switch media.MIMEType(attach.MIME) {
		case media.TypePDF:
			if spec, err := attach.GetSpec(ctx); err != nil {
				client.Log.Warnf("failed fetching attachment metadata: %s", err)
			} else {
				message.DocumentMessage.PageCount = ptrTo(uint32(spec.DocumentPage))
			}
			t, err := media.Convert(ctx, attach.Data, &previewThumbnailSpec)
			if err != nil {
				client.Log.Warnf("failed generating attachment thumbnail: %s", err)
			} else {
				message.DocumentMessage.JPEGThumbnail = t
			}
		}
	}

	return message, nil
}

// KnownExtensions represents MIME type to file-extension mappings for basic, known media types.
var knownExtensions = map[string]string{
	"image/jpeg": ".jpg",
	"audio/ogg":  ".oga",
	"audio/mp4":  ".m4a",
	"video/mp4":  ".mp4",
}

// ExtensionByType returns the file extension for the given MIME type, or a generic extension if the
// MIME type is unknown.
func extensionByType(typ string) string {
	// Handle common, known MIME types first.
	if ext := knownExtensions[typ]; ext != "" {
		return ext
	}
	if ext, _ := mime.ExtensionsByType(typ); len(ext) > 0 {
		return ext[0]
	}
	return ".bin"
}

// GetBaseMediaType returns the media type without any additional parameters.
func getBaseMediaType(typ string) string {
	return strings.SplitN(typ, ";", 2)[0]
}

// NewEventFromHistory returns event data meant for [Session.propagateEvent] for the primive history
// message given. Currently, only events related to group-chats will be handled, due to uncertain
// support for history back-fills on 1:1 chats.
//
// Otherwise, the implementation largely follows that of [newMessageEvent], however the base types
// used by these two functions differ in many small ways which prevent unifying the approach.
//
// Typically, this will return [EventMessage] events with appropriate [Message] payloads; unknown or
// invalid messages will return an [EventUnknown] event with nil data.
func newEventFromHistory(ctx context.Context, client *whatsmeow.Client, info *waWeb.WebMessageInfo) (EventKind, *EventPayload) {
	if info == nil {
		return EventUnknown, nil
	}
	if kind, payload, handled := canHistoricalCallEvent(ctx, client, info); handled {
		return kind, payload
	}
	// Handle message as group message is remote JID is a group JID in the absence of any other,
	// specific signal, or don't handle at all if no group JID is found.
	var jid = info.GetKey().GetRemoteJID()
	if j, _ := types.ParseJID(jid); j.Server != types.GroupServer {
		return EventUnknown, nil
	}

	// Reuse the live parser for poll messages so archived group polls and vote updates keep
	// the same metadata and decryption behavior after a bridge restart.
	rawMessage := info.GetMessage()
	if rawMessage.GetPollUpdateMessage() != nil ||
		rawMessage.GetPollCreationMessageV3() != nil ||
		rawMessage.GetPollCreationMessageV2() != nil ||
		rawMessage.GetPollCreationMessage() != nil {
		chatJID, err := types.ParseJID(jid)
		if err != nil {
			return EventUnknown, nil
		}
		evt, err := client.ParseWebMessage(chatJID, info)
		if err != nil {
			client.Log.Warnf("Failed parsing historical poll message '%s': %s", info.GetKey().GetID(), err)
			return EventUnknown, nil
		}
		kind, payload := newMessageEvent(ctx, client, evt)
		if payload != nil {
			payload.Message.IsHistory = true
		}
		return kind, payload
	}

	// Set basic data for message, to be potentially amended depending on the concrete version of
	// the underlying message.
	var message = Message{
		Kind:      MessagePlain,
		ID:        info.GetKey().GetID(),
		Body:      info.GetMessage().GetConversation(),
		Timestamp: int64(info.GetMessageTimestamp()),
		IsHistory: true,
	}

	// CAN: keep the room identity separate from the sender, including old-number history.
	chatJID, err := types.ParseJID(info.GetKey().GetRemoteJID())
	if err != nil {
		return EventUnknown, nil
	}
	message.Chat = newChat(ctx, client, chatJID, true)
	var ok bool
	message.Actor, ok = canHistoricalActor(ctx, client, info.GetKey(), info.GetParticipant())
	if !ok {
		return EventUnknown, nil
	}

	// Handle handle protocol messages (such as message deletion or editing), while ignoring known
	// unhandled types.
	switch info.GetMessageStubType() {
	case waWeb.WebMessageInfo_CIPHERTEXT:
		return EventUnknown, nil
	case waWeb.WebMessageInfo_CALL_MISSED_VOICE, waWeb.WebMessageInfo_CALL_MISSED_VIDEO:
		jid, err := types.ParseJID(info.GetKey().GetParticipant())
		if err != nil {
			return EventUnknown, nil
		}
		return EventCall, &EventPayload{Call: Call{
			State:     CallMissed,
			Actor:     newActor(ctx, client, jid),
			Timestamp: int64(info.GetMessageTimestamp()),
		}}
	case waWeb.WebMessageInfo_REVOKE:
		if p := info.GetMessageStubParameters(); len(p) > 0 {
			message.Kind = MessageRevoke
			message.ID = p[0]
			return EventMessage, &EventPayload{Message: message}
		} else {
			return EventUnknown, nil
		}
	}

	// Handle emoji reaction to existing message.
	for _, r := range info.GetReactions() {
		if r.GetText() != "" {
			reactor, known := canHistoricalActor(ctx, client, r.GetKey(), "")
			if !known {
				// No sender evidence: do not fabricate an author or fail the parent message.
				continue
			}
			message.Reactions = append(message.Reactions, Message{
				Chat:      message.Chat,
				Kind:      MessageReaction,
				ID:        r.GetKey().GetID(),
				Actor:     reactor,
				Body:      r.GetText(),
				Timestamp: r.GetSenderTimestampMS() / 1000,
			})
		}
	}

	// Handle message attachments, if any.
	if attach, context, err := getMessageAttachments(ctx, client, info.GetMessage()); err != nil {
        client.Log.Warnf("CAN_INBOUND attachment_unavailable class=%s", canAttachmentFailureClass(err))
        message = canUnavailableAttachment(message, info.GetMessage())
        if context != nil { message = getMessageWithContext(ctx,client,message,context) }
	} else if len(attach) > 0 {
		message.Attachments = append(message.Attachments, attach...)
		message.Kind = MessageAttachment
		if context != nil {
			message = getMessageWithContext(ctx, client, message, context)
		}
	}

	// Handle pre-set receipt status, if any.
	for _, r := range info.GetUserReceipt() {
		// Ignore self-receipts for the moment, as these cannot be handled correctly by the adapter.
		if client.Store.ID.ToNonAD().String() == r.GetUserJID() {
			continue // why? they're handled fine for live events
		}
		jid, err := types.ParseJID(r.GetUserJID())
		if err != nil {
			continue
		}
		var receipt = Receipt{
			Actor:      newActor(ctx, client, jid),
			Chat:       message.Chat,
			MessageIDs: []string{message.ID},
		}
		// CAN: history status is per participant, not the whole message.
		kind, timestamp, known := canHistoryReceipt(r.GetReceiptTimestamp(), r.GetReadTimestamp())
		if !known {
			continue
		}
		receipt.Kind = kind
		receipt.Timestamp = timestamp
		if kind == ReceiptRead && r.GetReceiptTimestamp() > 0 {
			delivered := receipt
			delivered.Kind = ReceiptDelivered
			delivered.Timestamp = r.GetReceiptTimestamp()
			message.Receipts = append(message.Receipts, delivered)
		}
		message.Receipts = append(message.Receipts, receipt)
	}

	// Get extended information from message, if available. Extended messages typically represent
	// messages with additional context, such as replies, forwards, etc.
	if e := info.GetMessage().GetExtendedTextMessage(); e != nil {
		if message.Body == "" {
			message.Body = e.GetText()
		}
		message = getMessageWithContext(ctx, client, message, e.GetContextInfo())
	}

	// Ignore obviously invalid messages.
	if message.Kind == MessagePlain && message.Body == "" {
		return EventUnknown, nil
	}

	return EventMessage, &EventPayload{Message: message}
}

// ChatStateKind represents the different kinds of chat-states possible in WhatsApp.
type ChatStateKind int

// The chat states handled by the overarching session event handler.
const (
	ChatStateUnknown ChatStateKind = iota
	ChatStateComposing
	ChatStatePaused
)

// A ChatState represents the activity of a contact within a certain discussion, for instance,
// whether the contact is currently composing a message. This is separate to the concept of a
// Presence, which is the contact's general state across all discussions.
type ChatState struct {
	Kind  ChatStateKind
	Chat  Chat
	Actor Actor
	Media string
}

// NewChatStateEvent returns event data meant for [Session.propagateEvent] for the primitive
// chat-state event given.
func newChatStateEvent(ctx context.Context, client *whatsmeow.Client, evt *events.ChatPresence) (EventKind, *EventPayload) {
	var state = ChatState{
		Actor: newActor(ctx, client, evt.Sender, evt.SenderAlt),
	}
	state.Chat = newChat(ctx, client, evt.Chat, evt.IsGroup)
	switch evt.State {
	case types.ChatPresenceComposing:
		state.Kind = ChatStateComposing
	case types.ChatPresencePaused:
		state.Kind = ChatStatePaused
	}
	state.Media = string(evt.Media)
	return EventChatState, &EventPayload{ChatState: state}
}

// ReceiptKind represents the different types of delivery receipts possible in WhatsApp.
type ReceiptKind int

// The delivery receipts handled by the overarching session event handler.
const (
	ReceiptUnknown ReceiptKind = iota
	ReceiptDelivered
	ReceiptRead
)

// A Receipt represents a notice of delivery or presentation for [Message] instances sent or
// received. Receipts can be delivered for many messages at once, but are generally all delivered
// under one specific state at a time.
type Receipt struct {
	Kind        ReceiptKind // The distinct kind of receipt presented.
	MessageIDs  []string    // The list of message IDs to mark for receipt.
	Actor       Actor
	OriginActor Actor
	Chat        Chat
	Timestamp   int64
}

// NewReceiptEvent returns event data meant for [Session.propagateEvent] for the primive receipt
// event given. Unknown or invalid receipts will return an [EventUnknown] event with nil data.
func newReceiptEvent(ctx context.Context, client *whatsmeow.Client, evt *events.Receipt) (EventKind, *EventPayload) {
	// CAN: unknown/retry/self-sync receipts are not delivery confirmations.
	if evt.Type != types.ReceiptTypeDelivered && evt.Type != types.ReceiptTypeRead {
		return EventUnknown, nil
	}
	var receipt = Receipt{
		MessageIDs: slices.Clone(evt.MessageIDs),
		Actor:      newActor(ctx, client, evt.Sender, evt.SenderAlt),
		Timestamp:  evt.Timestamp.Unix(),
	}
	receipt.Chat = newChat(ctx, client, evt.Chat, evt.IsGroup)
	receipt.Actor.IsMe = evt.IsFromMe

	if len(receipt.MessageIDs) == 0 {
		return EventUnknown, nil
	}

	if evt.Chat.Server == types.BroadcastServer {
		receipt.Actor.JID = evt.BroadcastListOwner.ToNonAD().String()
	}

	switch evt.Type {
	case types.ReceiptTypeDelivered:
		receipt.Kind = ReceiptDelivered
	case types.ReceiptTypeRead:
		receipt.Kind = ReceiptRead
	}

	return EventReceipt, &EventPayload{Receipt: receipt}
}

// NewMarkChatAsReadEvent converts a read action made on another
// WhatsApp device into a normal read receipt. Unread actions are ignored because
// XMPP displayed markers are monotonic.
func newMarkChatAsReadEvent(
	ctx context.Context,
	client *whatsmeow.Client,
	evt *events.MarkChatAsRead,
) (EventKind, *EventPayload) {
	action := evt.Action
	if action == nil || !action.GetRead() {
		return EventUnknown, nil
	}

	var messageID string
	var latestTimestamp int64
	if messageRange := action.GetMessageRange(); messageRange != nil {
		for _, message := range messageRange.GetMessages() {
			key := message.GetKey()
			if key == nil || key.GetID() == "" {
				continue
			}
			if messageID == "" || message.GetTimestamp() >= latestTimestamp {
				messageID = key.GetID()
				latestTimestamp = message.GetTimestamp()
			}
		}
	}
	if messageID == "" {
		client.Log.Warnf("Ignoring MarkChatAsRead without a message ID for %s", evt.JID)
		return EventUnknown, nil
	}

	chat := newChat(ctx, client, evt.JID, evt.JID.Server == types.GroupServer)
	if chat.JID == "" {
		client.Log.Warnf("Ignoring MarkChatAsRead for unknown chat %s", evt.JID)
		return EventUnknown, nil
	}

	actor := newActor(ctx, client, client.Store.GetJID(), client.Store.GetLID())
	actor.IsMe = true
	receipt := Receipt{
		Kind:       ReceiptRead,
		MessageIDs: []string{messageID},
		Actor:      actor,
		Chat:       chat,
		Timestamp:  evt.Timestamp.Unix(),
	}
	return EventReceipt, &EventPayload{Receipt: receipt}
}

// GroupAffiliation represents the set of privilidges given to a specific participant in a group.
type GroupAffiliation int

const (
	GroupAffiliationNone  GroupAffiliation = iota // None, or normal member group affiliation.
	GroupAffiliationAdmin                         // Can perform some management operations.
	GroupAffiliationOwner                         // Can manage group fully, including destroying the group.
)

// A Group represents a named, many-to-many chat space which may be joined or left at will. All
// fields apart from the group JID are considered to be optional, and may not be set in cases where
// group information is being updated against previous assumed state. Groups in WhatsApp are
// generally invited to out-of-band with respect to overarching adaptor; see the documentation for
// [Session.GetGroups] for more information.
type Group struct {
	JID          string             // The WhatsApp JID for this group.
	Name         string             // The user-defined, human-readable name for this group.
	Subject      GroupSubject       // The longer-form, user-defined description for this group.
	Nickname     string             // Our own nickname in this group-chat.
	Participants []GroupParticipant // The list of participant contacts for this group, including ourselves.
	InviteCode   string             // The code for inviting members to this group-chat.
}

// A GroupSubject represents the user-defined group description and attached metadata thereof, for a
// given [Group].
type GroupSubject struct {
	Subject string // The user-defined group description.
	SetAt   int64  // The exact time this group description was set at, as a timestamp.
	SetBy   Actor  // The name of the user that set the subject.
}

// GroupParticipantAction represents the distinct set of actions that can be taken when encountering
// a group participant, typically to add or remove.
type GroupParticipantAction int

const (
	GroupParticipantActionAdd     GroupParticipantAction = iota // Default action; add participant to list.
	GroupParticipantActionRemove                                // Remove participant from list, if existing.
	GroupParticipantActionPromote                               // Make group member into administrator.
	GroupParticipantActionDemote                                // Make group administrator into member.
)

// ToParticipantChange converts our public [GroupParticipantAction] to the internal [ParticipantChange]
// representation.
func (a GroupParticipantAction) toParticipantChange() whatsmeow.ParticipantChange {
	switch a {
	case GroupParticipantActionRemove:
		return whatsmeow.ParticipantChangeRemove
	case GroupParticipantActionPromote:
		return whatsmeow.ParticipantChangePromote
	case GroupParticipantActionDemote:
		return whatsmeow.ParticipantChangeDemote
	default:
		return whatsmeow.ParticipantChangeAdd
	}
}

// A GroupParticipant represents a contact who is currently joined in a given group. Participants in
// WhatsApp can generally be derived back to their individual [Contact]; there are no anonymous groups
// in WhatsApp.
type GroupParticipant struct {
	Actor       Actor
	Nickname    string                 // The user-set name for this participant, typically only set for anonymous participants.
	Affiliation GroupAffiliation       // The set of priviledges given to this specific participant.
	Action      GroupParticipantAction // The specific action to take for this participant; typically to add.
}

// NewGroupParticipant returns a [GroupParticipant], filling fields from the internal participant
// type. This is a no-op if [types.GroupParticipant.Error] is non-zero, and other fields may only
// be set optionally.
func newGroupParticipant(ctx context.Context, client *whatsmeow.Client, participant types.GroupParticipant) GroupParticipant {
	if participant.Error > 0 {
		return GroupParticipant{}
	}

	var p = GroupParticipant{
		Actor:    newActor(ctx, client, participant.PhoneNumber, participant.LID),
		Nickname: participant.DisplayName,
	}

	if p.Actor.JID != "" {
		if c, err := client.Store.Contacts.GetContact(ctx, participant.JID); err == nil {
			p.Nickname = c.PushName
		}
	}

	if participant.IsSuperAdmin {
		p.Affiliation = GroupAffiliationOwner
	} else if participant.IsAdmin {
		p.Affiliation = GroupAffiliationAdmin
	}
	return p
}

// NewGroupEvent returns event data meant for [Session.propagateEvent] for the primive group event
// given. Group data returned by this function can be partial, and callers should take care to only
// handle non-empty values.
func newGroupEvent(ctx context.Context, client *whatsmeow.Client, evt *events.GroupInfo) (EventKind, *EventPayload) {
	var group = Group{JID: evt.JID.ToNonAD().String()}
	if evt.Name != nil {
		group.Name = evt.Name.Name
	}
	if evt.Topic != nil {
		topicActor := newActor(ctx, client, evt.Topic.TopicSetBy, evt.Topic.TopicSetByPN)
		group.Subject = GroupSubject{
			Subject: evt.Topic.Topic,
			SetAt:   evt.Topic.TopicSetAt.Unix(),
		}
		group.Subject.SetBy = topicActor
	}
	for _, p := range evt.Join {
		group.Participants = append(group.Participants, GroupParticipant{
			Actor:  newActor(ctx, client, p),
			Action: GroupParticipantActionAdd,
		})
	}
	for _, p := range evt.Leave {
		group.Participants = append(group.Participants, GroupParticipant{
			Actor:  newActor(ctx, client, p),
			Action: GroupParticipantActionRemove,
		})
	}
	for _, p := range evt.Promote {
		group.Participants = append(group.Participants, GroupParticipant{
			Actor:       newActor(ctx, client, p),
			Action:      GroupParticipantActionPromote,
			Affiliation: GroupAffiliationAdmin,
		})
	}
	for _, p := range evt.Demote {
		group.Participants = append(group.Participants, GroupParticipant{
			Actor:       newActor(ctx, client, p),
			Action:      GroupParticipantActionDemote,
			Affiliation: GroupAffiliationNone,
		})
	}
	return EventGroup, &EventPayload{Group: group}
}

// NewGroup returns a concrete [Group] for the primitive data given. This function will generally
// populate fields with as much data as is available from the remote, and is therefore should not
// be called when partial data is to be returned.
func newGroup(ctx context.Context, client *whatsmeow.Client, info *types.GroupInfo) Group {
	var participants []GroupParticipant
	for i := range info.Participants {
		p := newGroupParticipant(ctx, client, info.Participants[i])
		participants = append(participants, p)
	}

	var group = Group{
		JID:  info.JID.ToNonAD().String(),
		Name: info.Name,
		Subject: GroupSubject{
			Subject: info.Topic,
			SetAt:   info.TopicSetAt.Unix(),
			SetBy:   newActor(ctx, client, info.TopicSetBy, info.TopicSetByPN),
		},
		Nickname:     client.Store.PushName,
		Participants: participants,
	}

	return group
}

// CallState represents the legacy fallback state used by the Python adapter.
type CallState int

const (
	CallUnknown CallState = iota
	CallIncoming
	CallMissed
)

const (
	callContractVersion         = 1
	callContractTransportPrefix = "call-contract-v1:"
	callDirectionIncoming       = "incoming"
	callDirectionOutgoing       = "outgoing"
	callDirectionUnknown        = "unknown"
	callKindUnknown             = "unknown"
	callKindVoice               = "voice"
	callKindVideo               = "video"
	callStateOffered            = "offered"
	callStateAccepted           = "accepted"
	callStateMissed             = "missed"
	callStateRejected           = "rejected"
	callStateEnded              = "ended"
)

// CallContract is the versioned, lossless-as-available call metadata transported to Python.
// Empty optional fields mean that WhatsApp did not provide that fact.
type CallContract struct {
	Version         int    `json:"version"`
	CallID          string `json:"call_id"`
	PeerJID         string `json:"peer_jid"`
	GroupJID        string `json:"group_jid,omitempty"`
	Direction       string `json:"direction"`
	Kind            string `json:"kind"`
	State           string `json:"state"`
	EventTimestamp  string `json:"event_timestamp,omitempty"`
	AnsweredAt      string `json:"answered_at,omitempty"`
	EndedAt         string `json:"ended_at,omitempty"`
	TerminalReason  string `json:"terminal_reason,omitempty"`
	Sequence        int    `json:"sequence"`
	DurationSeconds *int64 `json:"duration_seconds,omitempty"`
	Outcome         string `json:"outcome,omitempty"`
	Source          string `json:"source,omitempty"`
}

// A Call represents a WhatsApp call notification. The accessible text fallback remains
// available when the upstream notification has no CallID.
type Call struct {
	State     CallState
	Actor     Actor
	Timestamp int64
}

func callJIDString(jid types.JID) string {
	if jid.IsEmpty() {
		return ""
	}
	return jid.String()
}

func callTimestamp(timestamp time.Time) string {
	if timestamp.IsZero() {
		return ""
	}
	return timestamp.UTC().Format(time.RFC3339Nano)
}

func callStateFromTerminateReason(reason string) string {
	if reason == "accepted_elsewhere" {
		// Another linked device answered; WhatsApp's terminate signal is not terminal.
		return callStateAccepted
	}
	if reason == "" || reason == "timeout" {
		return callStateMissed
	}
	// WhatsMeow has already classified this as a termination; preserve its reason separately.
	return callStateEnded
}

func callSequenceFromTerminateReason(reason string) int {
	if reason == "accepted_elsewhere" {
		return 2
	}
	return 3
}

func callKindFromMedia(media string) string {
	switch media {
	case "audio":
		return callKindVoice
	case "video":
		return callKindVideo
	default:
		return callKindUnknown
	}
}

func callDirectionFromMeta(client *whatsmeow.Client, meta types.BasicCallMeta) string {
	if client == nil || client.Store == nil || meta.CallCreator.IsEmpty() {
		return callDirectionUnknown
	}
	creator := meta.CallCreator.ToNonAD()
	if client.Store.ID != nil && creator == client.Store.ID.ToNonAD() {
		return callDirectionOutgoing
	}
	if !client.Store.LID.IsEmpty() && creator == client.Store.LID.ToNonAD() {
		return callDirectionOutgoing
	}
	if isOwnCallJID(client, meta.CallCreatorAlt) {
		return callDirectionOutgoing
	}
	if canOwnCallAlias(client, creator) {
		return callDirectionOutgoing
	}
	if client.Store.ID != nil && creator.User != "" &&
		(creator.Server == types.DefaultUserServer || creator.Server == types.HiddenUserServer) &&
		(isOwnCallJID(client, meta.From) || meta.From.ToNonAD() == creator ||
			(!meta.CallCreatorAlt.IsEmpty() && meta.From.ToNonAD() == meta.CallCreatorAlt.ToNonAD())) {
		return callDirectionIncoming
	}
	return callDirectionUnknown
}

func newCallContract(
	state, direction, kind string,
	sequence int,
	terminalReason string,
	meta types.BasicCallMeta,
) *CallContract {
	callID := strings.TrimSpace(meta.CallID)
	if callID == "" {
		return nil
	}
	contract := &CallContract{
		Version:        callContractVersion,
		CallID:         callID,
		PeerJID:        callJIDString(meta.From),
		GroupJID:       callJIDString(meta.GroupJID),
		Direction:      direction,
		Kind:           kind,
		State:          state,
		EventTimestamp: callTimestamp(meta.Timestamp),
		Sequence:       sequence,
	}
	switch state {
	case callStateAccepted:
		contract.AnsweredAt = contract.EventTimestamp
	case callStateMissed, callStateRejected, callStateEnded:
		contract.EndedAt = contract.EventTimestamp
		contract.TerminalReason = strings.TrimSpace(terminalReason)
	}
	return contract
}

func callContractTransport(contract *CallContract) string {
	if contract == nil {
		return ""
	}
	payload, err := json.Marshal(contract)
	if err != nil {
		return ""
	}
	return callContractTransportPrefix + string(payload)
}

func legacyCallState(state string) CallState {
	switch state {
	case callStateOffered:
		return CallIncoming
	case callStateMissed:
		return CallMissed
	default:
		return CallUnknown
	}
}

// newCallEvent transports only facts provided by the WhatsApp event. Sequence is the stable
// contract phase (offer=1, accept=2, terminal=3), so consumers deduplicate by call_id+sequence.
func newCallEvent(
	ctx context.Context,
	client *whatsmeow.Client,
	state, direction, kind string,
	sequence int,
	terminalReason string,
	meta types.BasicCallMeta,
) (EventKind, *EventPayload) {
	meta = canCallPeerMeta(client, meta, direction)
	actor := newActor(ctx, client, meta.From, meta.CallCreator, meta.CallCreatorAlt)
	if transport := callContractTransport(newCallContract(state, direction, kind, sequence, terminalReason, meta)); transport != "" {
		// Actor.LID already crosses the generated v25 gopy ABI as an owned string.
		// Call events do not consume it in Python; retain contract metadata there rather
		// than adding a manually maintained C getter for a new Go field.
		actor.LID = transport
	}
	return EventCall, &EventPayload{Call: Call{
		State:     legacyCallState(state),
		Actor:     actor,
		Timestamp: meta.Timestamp.Unix(),
	}}
}

const callRecordSequence = 4

func callLogJID(raw string) types.JID {
	jid, err := types.ParseJID(strings.TrimSpace(raw))
	if err != nil {
		return types.JID{}
	}
	return jid.ToNonAD()
}

func isOwnCallJID(client *whatsmeow.Client, jid types.JID) bool {
	if client == nil || client.Store == nil || jid.IsEmpty() {
		return false
	}
	jid = jid.ToNonAD()
	return jid == client.Store.GetJID().ToNonAD() || jid == client.Store.GetLID().ToNonAD()
}

func callLogPeer(client *whatsmeow.Client, record *waSyncAction.CallLogRecord) types.JID {
	if record == nil {
		return types.JID{}
	}
	creator := callLogJID(record.GetCallCreatorJID())
	if record.IsIncoming != nil && record.GetIsIncoming() &&
		!creator.IsEmpty() && !isOwnCallJID(client, creator) {
		return creator
	}
	for _, participant := range record.GetParticipants() {
		jid := callLogJID(participant.GetUserJID())
		if !jid.IsEmpty() && !isOwnCallJID(client, jid) {
			return jid
		}
	}
	if !creator.IsEmpty() && !isOwnCallJID(client, creator) {
		return creator
	}
	return types.JID{}
}

func callLogTimestamp(raw int64) time.Time {
	if raw <= 0 {
		return time.Time{}
	}
	// WhatsApp schemas do not declare a unit. Current call records use milliseconds,
	// while older fixtures exist in seconds; accept both without altering the value.
	if raw >= 100_000_000_000 {
		return time.UnixMilli(raw)
	}
	return time.Unix(raw, 0)
}

func callLogOutcome(result waSyncAction.CallLogRecord_CallResult) string {
	switch result {
	case waSyncAction.CallLogRecord_CONNECTED:
		return "connected"
	case waSyncAction.CallLogRecord_REJECTED:
		return "rejected"
	case waSyncAction.CallLogRecord_CANCELLED:
		return "cancelled"
	case waSyncAction.CallLogRecord_ACCEPTEDELSEWHERE:
		return "accepted_elsewhere"
	case waSyncAction.CallLogRecord_MISSED:
		return "missed"
	case waSyncAction.CallLogRecord_INVALID:
		return "invalid"
	case waSyncAction.CallLogRecord_UNAVAILABLE:
		return "unavailable"
	case waSyncAction.CallLogRecord_UPCOMING:
		return "upcoming"
	case waSyncAction.CallLogRecord_FAILED:
		return "failed"
	case waSyncAction.CallLogRecord_ABANDONED:
		return "abandoned"
	case waSyncAction.CallLogRecord_ONGOING:
		return "ongoing"
	default:
		return ""
	}
}

func callLogState(outcome string) string {
	switch outcome {
	case "missed":
		return callStateMissed
	case "rejected":
		return callStateRejected
	case "ongoing", "upcoming", "":
		return "unknown"
	default:
		return callStateEnded
	}
}

func newCallLogContract(
	client *whatsmeow.Client,
	record *waSyncAction.CallLogRecord,
	source string,
) (*CallContract, types.JID, time.Time) {
	if record == nil {
		return nil, types.JID{}, time.Time{}
	}
	callID := strings.TrimSpace(record.GetCallID())
	if callID == "" {
		callID = strings.TrimSpace(record.GetScheduledCallID())
	}
	if callID == "" {
		return nil, types.JID{}, time.Time{}
	}

	peer := callLogPeer(client, record)
	group := callLogJID(record.GetGroupJID())
	if peer.IsEmpty() && !group.IsEmpty() {
		peer = group
	}
	timestamp := callLogTimestamp(record.GetStartTime())
	direction := callDirectionUnknown
	if record.IsIncoming != nil {
		if record.GetIsIncoming() {
			direction = callDirectionIncoming
		} else {
			direction = callDirectionOutgoing
		}
	}
	kind := callKindUnknown
	if record.IsVideo != nil {
		if record.GetIsVideo() {
			kind = callKindVideo
		} else {
			kind = callKindVoice
		}
	}
	outcome := ""
	if record.CallResult != nil {
		outcome = callLogOutcome(record.GetCallResult())
	}
	contract := &CallContract{
		Version:        callContractVersion,
		CallID:         callID,
		PeerJID:        callJIDString(peer),
		GroupJID:       callJIDString(group),
		Direction:      direction,
		Kind:           kind,
		State:          callLogState(outcome),
		EventTimestamp: callTimestamp(timestamp),
		TerminalReason: outcome,
		Sequence:       callRecordSequence,
		Outcome:        outcome,
		Source:         source,
	}
	if record.Duration != nil && record.GetDuration() >= 0 {
		duration := record.GetDuration()
		contract.DurationSeconds = &duration
	}
	return contract, peer, timestamp
}

func newCallLogRecordEvent(
	ctx context.Context,
	client *whatsmeow.Client,
	record *waSyncAction.CallLogRecord,
	source string,
) (EventKind, *EventPayload) {
	contract, peer, timestamp := newCallLogContract(client, record, source)
	if contract == nil {
		return EventUnknown, nil
	}
	actor := newActor(ctx, client, peer)
	if transport := callContractTransport(contract); transport != "" {
		actor.LID = transport
	}
	return EventCall, &EventPayload{Call: Call{
		State:     legacyCallState(contract.State),
		Actor:     actor,
		Timestamp: timestamp.Unix(),
	}}
}

func callLogMessageOutcome(result waE2E.CallLogMessage_CallOutcome) string {
	switch result {
	case waE2E.CallLogMessage_CONNECTED:
		return "connected"
	case waE2E.CallLogMessage_MISSED:
		return "missed"
	case waE2E.CallLogMessage_FAILED:
		return "failed"
	case waE2E.CallLogMessage_REJECTED:
		return "rejected"
	case waE2E.CallLogMessage_ACCEPTED_ELSEWHERE:
		return "accepted_elsewhere"
	case waE2E.CallLogMessage_ONGOING:
		return "ongoing"
	case waE2E.CallLogMessage_SILENCED_BY_DND:
		return "silenced_by_dnd"
	case waE2E.CallLogMessage_SILENCED_UNKNOWN_CALLER:
		return "silenced_unknown_caller"
	default:
		return ""
	}
}

func newCallLogMessageEvent(
	ctx context.Context,
	client *whatsmeow.Client,
	evt *events.Message,
) (EventKind, *EventPayload) {
	if evt == nil || evt.Message == nil {
		return EventUnknown, nil
	}
	message := evt.Message.GetCallLogMesssage()
	if message == nil || strings.TrimSpace(string(evt.Info.ID)) == "" {
		return EventUnknown, nil
	}
	peer := evt.Info.Chat.ToNonAD()
	group := types.JID{}
	if evt.Info.IsGroup {
		group = evt.Info.Chat.ToNonAD()
		peer = evt.Info.Sender.ToNonAD()
		if isOwnCallJID(client, peer) {
			peer = types.JID{}
			for _, participant := range message.GetParticipants() {
				candidate := callLogJID(participant.GetJID())
				if !candidate.IsEmpty() && !isOwnCallJID(client, candidate) {
					peer = candidate
					break
				}
			}
		}
		if peer.IsEmpty() {
			peer = group
		}
	}
	direction := callDirectionIncoming
	if evt.Info.IsFromMe {
		direction = callDirectionOutgoing
	}
	kind := callKindUnknown
	if message.IsVideo != nil {
		if message.GetIsVideo() {
			kind = callKindVideo
		} else {
			kind = callKindVoice
		}
	}
	outcome := ""
	if message.CallOutcome != nil {
		outcome = callLogMessageOutcome(message.GetCallOutcome())
	}
	source := "message"
	if evt.SourceWebMsg != nil {
		source = "history_sync"
	}
	contract := &CallContract{
		Version:        callContractVersion,
		CallID:         strings.TrimSpace(string(evt.Info.ID)),
		PeerJID:        callJIDString(peer),
		GroupJID:       callJIDString(group),
		Direction:      direction,
		Kind:           kind,
		State:          callLogState(outcome),
		EventTimestamp: callTimestamp(evt.Info.Timestamp),
		TerminalReason: outcome,
		Sequence:       callRecordSequence,
		Outcome:        outcome,
		Source:         source,
	}
	if message.DurationSecs != nil && message.GetDurationSecs() >= 0 {
		duration := message.GetDurationSecs()
		contract.DurationSeconds = &duration
	}
	actor := newActor(ctx, client, peer, evt.Info.SenderAlt)
	if transport := callContractTransport(contract); transport != "" {
		actor.LID = transport
	}
	return EventCall, &EventPayload{Call: Call{
		State:     legacyCallState(contract.State),
		Actor:     actor,
		Timestamp: evt.Info.Timestamp.Unix(),
	}}
}

// NewActor returns a concrete [Actor] for the given primary or alternative [types.JID], representing
// one or more phone-number or anonymous IDs (JIDs and LIDs, in WhatsApp nomenclature).
//
// This function makes a best-effort search for JID or LID when either are missing, based on
// internal mappings, or other stored data; it is possible, however, that [Actor] representations
// returned are partial or empty.
const jidResolutionTimeout = 750 * time.Millisecond

func getPNForLIDBounded(ctx context.Context, client *whatsmeow.Client, jid types.JID) (types.JID, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	lookupCtx, cancel := context.WithTimeout(ctx, jidResolutionTimeout)
	defer cancel()
	return client.Store.LIDs.GetPNForLID(lookupCtx, jid)
}

func getLIDForPNBounded(ctx context.Context, client *whatsmeow.Client, jid types.JID) (types.JID, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	lookupCtx, cancel := context.WithTimeout(ctx, jidResolutionTimeout)
	defer cancel()
	return client.Store.LIDs.GetLIDForPN(lookupCtx, jid)
}

func newActor(ctx context.Context, client *whatsmeow.Client, primaryJID types.JID, altJIDs ...types.JID) Actor {
	var phoneID, anonID types.JID
	var actor Actor

	// Find (phone-number) JID and (numeric) LID from list of identifiers given in best-effort search.
	for _, id := range append([]types.JID{primaryJID}, altJIDs...) {
		if id.Server == types.HiddenUserServer && anonID.IsEmpty() {
			anonID = id
		} else if id.Server == types.DefaultUserServer && phoneID.IsEmpty() {
			phoneID = id
		} else if !id.IsEmpty() {
			client.Log.Debugf("Unused JID or LID: %s", id)
		}
	}

	// Try to get JID or LID from internal mapping, if possible.
	if phoneID.IsEmpty() && !anonID.IsEmpty() {
		phoneID, _ = getPNForLIDBounded(ctx, client, anonID)
	} else if !phoneID.IsEmpty() && anonID.IsEmpty() {
		anonID, _ = getLIDForPNBounded(ctx, client, phoneID)
	}

	// Set actor JID and LID based on values given, or try to fall back stored values for own device
	// if we've surmised that either JID or LID is for the self-actor.
	if !phoneID.IsEmpty() {
		actor.JID = phoneID.ToNonAD().String()
		actor.IsMe = phoneID.ToNonAD() == client.Store.GetJID().ToNonAD()
	}
	if !anonID.IsEmpty() {
		actor.LID = anonID.ToNonAD().String()
		// CAN: a stale PN/LID mapping must not erase authenticated own-PN evidence.
		actor.IsMe = actor.IsMe || anonID.ToNonAD() == client.Store.GetLID().ToNonAD()
	}

	if actor.IsMe {
		// CAN: use the authenticated device identity, not an obsolete own alias.
		if own := client.Store.GetJID(); !own.IsEmpty() {
			actor.JID = own.ToNonAD().String()
		}
		if own := client.Store.GetLID(); !own.IsEmpty() {
			actor.LID = own.ToNonAD().String()
		}
	}

	return actor
}

// NewChat returns a concrete [Chat] instance for the JID given, which is expected to be a concrete
// group-chat JID or phone-number JID. In cases where the JID given is an anonymous LID for a user,
// we will attempt a best-effort mapping back to the phone-number JID.
func newChat(ctx context.Context, client *whatsmeow.Client, jid types.JID, isGroup bool) Chat {
	var chatJID types.JID
	if jid.Server == types.DefaultUserServer || jid.Server == types.GroupServer {
		chatJID = jid
	} else if jid.Server == types.HiddenUserServer && !isGroup {
		chatJID, _ = getPNForLIDBounded(ctx, client, jid)
	}

	if chatJID.IsEmpty() {
		return Chat{}
	}

	return Chat{
		JID:     chatJID.ToNonAD().String(),
		IsGroup: isGroup,
	}
}

// canHistoryReceipt extracts only explicit per-person receipt evidence.
func canHistoryReceipt(delivered, read int64) (ReceiptKind, int64, bool) {
	if read > 0 {
		return ReceiptRead, read, true
	}
	if delivered > 0 {
		return ReceiptDelivered, delivered, true
	}
	return ReceiptDelivered, 0, false
}

// canOwnCallAlias recognizes a stored PN/LID alias without guessing from phone digits.
func canOwnCallAlias(client *whatsmeow.Client, creator types.JID) bool {
	if client == nil || client.Store == nil || client.Store.LIDs == nil {
		return false
	}
	var mapped types.JID
	if creator.Server == types.DefaultUserServer {
		mapped, _ = getLIDForPNBounded(context.Background(), client, creator)
	} else if creator.Server == types.HiddenUserServer {
		mapped, _ = getPNForLIDBounded(context.Background(), client, creator)
	}
	return !mapped.IsEmpty() && isOwnCallJID(client, mapped)
}

// canHistoricalCallEvent preserves typed call logs before the group-only message filter.
func canHistoricalCallEvent(ctx context.Context, client *whatsmeow.Client, info *waWeb.WebMessageInfo) (EventKind, *EventPayload, bool) {
	if info == nil || info.GetMessage() == nil {
		return EventUnknown, nil, false
	}
	raw := (&events.Message{RawMessage: info.GetMessage()}).UnwrapRaw()
	if raw.Message.GetCallLogMesssage() == nil {
		return EventUnknown, nil, false
	}
	chat, err := types.ParseJID(info.GetKey().GetRemoteJID())
	if client == nil || chat.User == "" || info.GetMessageTimestamp() == 0 || err != nil ||
		(chat.Server != types.DefaultUserServer && chat.Server != types.HiddenUserServer && chat.Server != types.GroupServer) {
		return EventUnknown, nil, true
	}
	evt, err := client.ParseWebMessage(chat, info)
	if err != nil {
		client.Log.Warnf("Unable to parse historical call log (invalid metadata)")
		return EventUnknown, nil, true
	}
	kind, payload := newCallLogMessageEvent(ctx, client, evt)
	return kind, payload, true
}

// canCallPeerMeta does not route an incoming acceptance from our phone into a self-chat.
func canCallPeerMeta(client *whatsmeow.Client, meta types.BasicCallMeta, direction string) types.BasicCallMeta {
	if direction != callDirectionIncoming || (!meta.From.IsEmpty() && !isOwnCallJID(client, meta.From)) {
		return meta
	}
	for _, creator := range []types.JID{meta.CallCreatorAlt, meta.CallCreator} {
		if creator.User != "" && !isOwnCallJID(client, creator) &&
			(creator.Server == types.DefaultUserServer || creator.Server == types.HiddenUserServer) {
			meta.From = creator.ToNonAD()
			break
		}
	}
	return meta
}
