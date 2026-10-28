package whatsapp

import (
	// Standard library.
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"image/jpeg"
	"math/rand"
	"slices"
	"strings"
	"sync"
	"time"

	// Internal packages.
	"codeberg.org/slidge/slidge-whatsapp/slidge_whatsapp/media"

	// Third-party libraries.
	_ "github.com/mattn/go-sqlite3"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/appstate"
	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

const (
	// The default host part for user JIDs on WhatsApp.
	DefaultUserServer = types.DefaultUserServer

	// The default host part for group JIDs on WhatsApp.
	DefaultGroupServer = types.GroupServer

	// The number of times keep-alive checks can fail before attempting to re-connect the session.
	keepAliveFailureThreshold = 3

	// The minimum and maximum wait interval between connection retries after keep-alive check failure.
	keepAliveMinRetryInterval = 5 * time.Second
	keepAliveMaxRetryInterval = 5 * time.Minute

	// The amount of time to wait before re-requesting contact presences WhatsApp. This is required
	// since otherwise WhatsApp will assume that you're inactive, and will stop sending presence
	// updates for contacts and groups. By default, this interval has a jitter of ± half its value
	// (e.g. for an initial interval of 2 hours, the final value will range from 1 to 3 hours) in
	// order to provide a more natural interaction with remote WhatsApp servers.
	presenceRefreshInterval = 12 * time.Hour

	// Similarly, a sleep interval between making avatar-related calls to whatsapp
	requestAvatarInterval = 100 * time.Millisecond

	// The maximum number of messages to request at a time when performing on-demand history
	// synchronization.
	maxHistorySyncMessages = 50
)

// A Session represents a connection (active or not) between a linked device and WhatsApp. Active
// sessions need to be established by logging in, after which incoming events will be forwarded to
// the adapter event handler, and outgoing events will be forwarded to WhatsApp.
type Session struct {
	// CAN inbound isolation v1
	canInboundMutex sync.Mutex
	canInbound *canInboundDispatcher
	canCallRecoveryMutex sync.Mutex
	canCallRecoveries    map[string]*canCallRecoveryEntry
	device               LinkedDevice      // The linked device this session corresponds to.
	client               *whatsmeow.Client // The concrete client connection to WhatsApp for this session.
	clientMutex          sync.RWMutex      // Protects client snapshots during concurrent session cleanup.
	gateway              *Gateway          // The Gateway this Session is attached to.

	ctx       context.Context         // A shared context for all top-level [Session] functions.
	ctxCancel context.CancelCauseFunc // The function to call when cancelling the [Session] context.

	eventHandler   HandleEventFunc   // The handler function used for propagating adapter events.
	presenceMutex  sync.Mutex        // Protects the current presence-refresh lifecycle.
	presenceChan   chan PresenceKind // Receives availability changes for the current refresher.
	presenceCancel context.CancelFunc
	presenceDone   chan struct{}

	lastAvatarCall time.Time  // We keep try of the last time we called GetAvatar here
	avatarMutex    sync.Mutex // A mutex to safely
}

func (s *Session) currentClient() *whatsmeow.Client {
	s.clientMutex.RLock()
	defer s.clientMutex.RUnlock()
	return s.client
}

func (s *Session) setClient(client *whatsmeow.Client) {
	s.clientMutex.Lock()
	s.client = client
	s.clientMutex.Unlock()
}

func (s *Session) clearClient(client *whatsmeow.Client) {
	s.clientMutex.Lock()
	if s.client == client {
		s.canCancelCallRecoveries()
		s.client = nil
	}
	s.clientMutex.Unlock()
}

func (s *Session) startPresenceRefresh(ctx context.Context, client *whatsmeow.Client) {
	s.stopPresenceRefresh()

	refreshCtx, cancel := context.WithCancel(ctx)
	presenceChan := make(chan PresenceKind, 1)
	done := make(chan struct{})

	s.presenceMutex.Lock()
	s.presenceChan = presenceChan
	s.presenceCancel = cancel
	s.presenceDone = done
	s.presenceMutex.Unlock()

	go func() {
		defer close(done)
		newTimer := func(d time.Duration) *time.Timer {
			return time.NewTimer(d + time.Duration(rand.Int63n(int64(d))-int64(d/2)))
		}
		timer := newTimer(presenceRefreshInterval)
		timerStopped := false
		presence := PresenceAvailable
		defer func() {
			if !timer.Stop() {
				select {
				case <-timer.C:
				default:
				}
			}
		}()

		for {
			select {
			case <-refreshCtx.Done():
				return
			case <-timer.C:
				if presence == PresenceAvailable {
					_ = s.subscribeToPresences(refreshCtx, client)
					timer = newTimer(presenceRefreshInterval)
					timerStopped = false
				} else {
					timerStopped = true
				}
			case presence = <-presenceChan:
				if timerStopped && presence == PresenceAvailable {
					_ = s.subscribeToPresences(refreshCtx, client)
					timer = newTimer(presenceRefreshInterval)
					timerStopped = false
				}
			}
		}
	}()
}

func (s *Session) stopPresenceRefresh() {
	s.presenceMutex.Lock()
	cancel := s.presenceCancel
	done := s.presenceDone
	s.presenceChan = nil
	s.presenceCancel = nil
	s.presenceDone = nil
	s.presenceMutex.Unlock()

	if cancel != nil {
		cancel()
	}
	if done != nil {
		<-done
	}
}

func (s *Session) queuePresenceRefresh(presence PresenceKind) {
	s.presenceMutex.Lock()
	presenceChan := s.presenceChan
	s.presenceMutex.Unlock()
	if presenceChan == nil {
		return
	}

	select {
	case presenceChan <- presence:
	default:
		// Only the latest availability matters to the refresh scheduler.
		select {
		case <-presenceChan:
		default:
		}
		select {
		case presenceChan <- presence:
		default:
		}
	}
}

// Login attempts to authenticate the given [Session], either by re-using the [LinkedDevice] attached
// or by initiating a pairing session for a new linked device. Callers are expected to have set an
// event handler in order to receive any incoming events from the underlying WhatsApp session.
// syncCallLogAppState asks WhatsApp for the regular app-state snapshot. Call logs live
// under the regular `call_log` index and are otherwise only delivered as incremental updates.
func (s *Session) syncCallLogAppState(client *whatsmeow.Client) {
	if client == nil {
		return
	}
	if err := client.FetchAppState(s.ctx, appstate.WAPatchRegular, true, false); err != nil {
		client.Log.Warnf("Failed to backfill WhatsApp call log app state: %v", err)
	}
}

func (s *Session) Login() error {
	var store *store.Device
	var err error

	// Try to fetch existing device from given device JID.
	s.ctx, s.ctxCancel = context.WithCancelCause(context.Background())
	s.canStartInbound()
	if s.device.ID != "" {
		store, err = s.gateway.container.GetDevice(s.ctx, s.device.JID())
		if err != nil {
			return err
		}
	}

	if store == nil {
		store = s.gateway.container.NewDevice()
	}

	client := whatsmeow.NewClient(store, s.gateway.logger)
	s.setClient(client)
	// CAN linking lifecycle v1
	client.AddEventHandler(func(event any) {
		if s.currentClient() == client { s.handleEvent(event) }
	})
	client.AutomaticMessageRerequestFromPhone = true
	client.EmitAppStateEventsOnFullSync = true

	// Refresh contact presences on a set interval, to avoid issues with WhatsApp dropping them.
	// The refresher owns a client snapshot and is stopped before session cleanup.
	s.startPresenceRefresh(s.ctx, client)

	// Simply connect our client if already registered.
	if client.Store.ID != nil {
		if err := client.ConnectContext(s.ctx); err != nil {
			return err
		}
		return nil
	}

	// Attempt out-of-band registration of client via QR code.
	ctx := s.ctx
	qrChan, qrErr := client.GetQRChannel(ctx)
	if qrErr != nil {
		s.stopPresenceRefresh()
		s.clearClient(client)
		s.ctxCancel(qrErr)
		return fmt.Errorf("cannot initialize QR channel: %w", qrErr)
	}
	if err = client.ConnectContext(s.ctx); err != nil {
		s.stopPresenceRefresh()
		s.clearClient(client)
		s.ctxCancel(err)
		return err
	}

	go func() {
		for e := range qrChan {
			if s.currentClient() != client || ctx.Err() != nil { return }
			switch e.Event {
			case whatsmeow.QRChannelEventCode:
				s.propagateEvent(EventQRCode, &EventPayload{QRCode: canLinkQRPayload(e.Code, e.Timeout)})
			case whatsmeow.QRChannelSuccess.Event:
				return
			default:
				reason := "pair_failed"
				switch e.Event {
				case whatsmeow.QRChannelTimeout.Event: reason = "expired"
				case whatsmeow.QRChannelClientOutdated.Event: reason = "client_outdated"
				case "passkey-request", "passkey-confirmation": reason = "unsupported_pairing"
				}
				// Unknown/empty events never masquerade as a successful connection.
				s.propagateEvent(EventConnect, &EventPayload{Connect: Connect{Error: "can-link:"+reason}})
				return
			}
		}
		if s.currentClient() == client && ctx.Err() == nil && !client.IsLoggedIn() {
			s.propagateEvent(EventConnect, &EventPayload{Connect: Connect{Error: "can-link:pair_failed"}})
		}
	}()

	return nil
}

// Logout disconnects and removes the current linked device locally and initiates a logout remotely.
func (s *Session) Logout() error {
	client := s.currentClient()
	s.stopPresenceRefresh()
	if client == nil || client.Store == nil || client.Store.ID == nil {
		return nil
	}

	err := client.Logout(s.ctx)
	s.ctxCancel(nil)
	s.clearClient(client)
	return err
}

// Disconnects detaches the current connection to WhatsApp without removing any linked device state.
func (s *Session) Disconnect() error {
	client := s.currentClient()
	s.stopPresenceRefresh()
	if client == nil {
		s.canStopInbound()
		return nil
	}

	client.Disconnect()
	s.ctxCancel(nil)
	s.clearClient(client)
	s.canStopInbound()
	return nil
}

// PairPhone returns a one-time code from WhatsApp, used for pairing this [Session] against the
// user's primary device, as identified by the given phone number. This will return an error if the
// [Session] is already paired, or if the phone number given is empty or invalid.
func (s *Session) PairPhone(phone string) (string, error) {
	client := s.currentClient()
	if client == nil || !client.IsConnected() {
		return "", fmt.Errorf("cannot pair for uninitialized session")
	} else if client.Store.ID != nil {
		return "", fmt.Errorf("refusing to pair for connected session")
	} else if phone == "" {
		return "", fmt.Errorf("cannot pair for empty phone number")
	}

	code, err := client.PairPhone(s.ctx, phone, true, whatsmeow.PairClientChrome, "Chrome (Linux)")
	if err != nil {
		return "", fmt.Errorf("failed to pair with phone number: %s", err)
	}

	return code, nil
}

func pollVoteMessageInfo(message Message, chatJID types.JID) (*types.MessageInfo, []string, error) {
	if message.ID == "" {
		return nil, nil, fmt.Errorf("poll vote requires a poll ID")
	}
	if message.Chat.IsGroup != (chatJID.Server == types.GroupServer) {
		return nil, nil, fmt.Errorf("poll vote chat kind does not match JID '%s'", chatJID)
	}

	creator := message.Actor.JID
	pollChat := chatJID
	if message.Chat.IsGroup {
		if message.Actor.LID == "" {
			return nil, nil, fmt.Errorf("group poll vote requires the creator LID")
		}
		creator = message.Actor.LID
	} else if message.Actor.LID != "" {
		creator = message.Actor.LID
	}
	if creator == "" {
		return nil, nil, fmt.Errorf("poll vote requires the creator identity")
	}
	sender, err := types.ParseJID(creator)
	if err != nil {
		return nil, nil, fmt.Errorf("could not parse poll creator identity '%s': %v", creator, err)
	}
	if !message.Chat.IsGroup && !message.Actor.IsMe && message.Actor.LID != "" {
		// Incoming direct polls are commonly stored under the contact LID.
		// Preserve that exact chat key for both secret lookup and encryption.
		pollChat = sender
	}

	if len(message.Poll.Options) < 1 || len(message.Poll.Options) > 12 {
		return nil, nil, fmt.Errorf("poll vote requires between 1 and 12 options")
	}
	optionNames := make([]string, 0, len(message.Poll.Options))
	seen := make(map[string]struct{}, len(message.Poll.Options))
	for _, option := range message.Poll.Options {
		if strings.TrimSpace(option.Title) == "" {
			return nil, nil, fmt.Errorf("poll vote options cannot be empty")
		}
		if _, ok := seen[option.Title]; ok {
			return nil, nil, fmt.Errorf("poll vote options cannot be duplicated")
		}
		seen[option.Title] = struct{}{}
		optionNames = append(optionNames, option.Title)
	}

	return &types.MessageInfo{
		MessageSource: types.MessageSource{
			Chat:     pollChat,
			Sender:   sender,
			IsFromMe: message.Actor.IsMe,
			IsGroup:  message.Chat.IsGroup,
		},
		ID: types.MessageID(message.ID),
	}, optionNames, nil
}

// SendMessage processes the given Message and sends a WhatsApp message for the kind and contact JID
// specified within. In general, different message kinds require different fields to be set; see the
// documentation for the [Message] type for more information.
func (s *Session) SendMessage(message Message) error {
	if s.client == nil || s.client.Store.ID == nil {
		return fmt.Errorf("cannot send message for unauthenticated session")
	}

	jid, err := types.ParseJID(message.Chat.JID)
	if err != nil {
		return fmt.Errorf("could not parse sender JID for message: %s", err)
	}

	var payload *waE2E.Message
	var extra whatsmeow.SendRequestExtra

	switch message.Kind {
	case MessageAttachment:
		// Handle message with attachment, if any.
		if len(message.Attachments) == 0 {
			return nil
		}

		// Upload attachment into WhatsApp before sending message.
		if payload, err = uploadAttachment(s.ctx, s.client, &message.Attachments[0]); err != nil {
			return fmt.Errorf("failed uploading attachment: %s", err)
		}
		// The upload helper builds the media payload directly. Apply XEP-0461
		// context to that payload before sending it to WhatsApp.
		setReplyContext(payload, message)
		extra.ID = message.ID
	case MessageEdit:
		// Edit existing message by ID.
		payload = s.client.BuildEdit(s.device.JID().ToNonAD(), message.ID, s.getMessagePayload(s.ctx, message))
	case MessageRevoke:
		// Don't send message, but revoke existing message by ID.
		var originLID types.JID
		if message.Chat.IsGroup && !message.OriginActor.IsMe {
			// A message moderation
			originLID, err = types.ParseJID(message.OriginActor.LID)
			if err != nil {
				return fmt.Errorf("could not parse actor '%s' for message: %s", message.OriginActor.LID, err)
			}
		} else {
			// A message retraction by the person who sent it
			originLID = types.EmptyJID
		}
		payload = s.client.BuildRevoke(jid, originLID, message.ID)
	case MessagePoll:
		pollInfo, optionNames, pollErr := pollVoteMessageInfo(message, jid)
		if pollErr != nil {
			return pollErr
		}
		if message.Actor.IsMe && !message.Chat.IsGroup && message.Actor.LID != "" {
			recipientLID, lidErr := s.client.Store.LIDs.GetLIDForPN(s.ctx, pollInfo.Chat)
			if lidErr != nil {
				return fmt.Errorf("failed to resolve direct poll recipient LID: %s", lidErr)
			}
			if recipientLID.IsEmpty() {
				return fmt.Errorf("missing direct poll recipient LID")
			}
			pollInfo.Chat = recipientLID
		}
		payload, err = s.client.BuildPollVote(s.ctx, pollInfo, optionNames)
		if err != nil {
			return fmt.Errorf("failed building poll vote: %s", err)
		}
		// Whatsmeow requires the encrypted vote to be sent to the same chat key
		// used by the original poll secret. For LID-addressed direct polls this
		// intentionally differs from the XMPP contact's phone-number JID.
		jid = pollInfo.Chat
		s.gateway.logger.Infof(
			"Sending poll vote id=%s target=%s creator=%s",
			message.ID, jid, pollInfo.Sender,
		)
	case MessageReaction:
		// Send message as emoji reaction to a given message.
		var participant string
		if message.Chat.IsGroup {
			participant = message.OriginActor.LID
		} else {
			participant = message.OriginActor.JID
		}
		payload = &waE2E.Message{
			ReactionMessage: &waE2E.ReactionMessage{
				Key: &waCommon.MessageKey{
					RemoteJID:   &message.Chat.JID,
					FromMe:      &message.OriginActor.IsMe,
					ID:          &message.ID,
					Participant: &participant,
				},
				Text:              &message.Body,
				SenderTimestampMS: ptrTo(time.Now().UnixMilli()),
			},
		}
	default:
		payload = s.getMessagePayload(s.ctx, message)
		extra.ID = message.ID
	}

	if message.IsForwarded && (message.Kind == MessagePlain || message.Kind == MessageAttachment) {
		setForwardedContext(payload)
	}

	s.gateway.logger.Debugf("Sending message to JID '%s': %+v", jid, payload)
	_, err = s.client.SendMessage(s.ctx, jid, payload, extra)
	return err
}

func setForwardedContext(payload *waE2E.Message) {
	if payload == nil {
		return
	}

	isForwarded := true
	setFlag := func(info **waE2E.ContextInfo) {
		if *info == nil {
			*info = &waE2E.ContextInfo{}
		}
		(*info).IsForwarded = &isForwarded
	}

	switch {
	case payload.ExtendedTextMessage != nil:
		setFlag(&payload.ExtendedTextMessage.ContextInfo)
	case payload.Conversation != nil:
		text := payload.GetConversation()
		payload.Conversation = nil
		payload.ExtendedTextMessage = &waE2E.ExtendedTextMessage{Text: &text}
		setFlag(&payload.ExtendedTextMessage.ContextInfo)
	case payload.ImageMessage != nil:
		setFlag(&payload.ImageMessage.ContextInfo)
	case payload.AudioMessage != nil:
		setFlag(&payload.AudioMessage.ContextInfo)
	case payload.VideoMessage != nil:
		setFlag(&payload.VideoMessage.ContextInfo)
	case payload.DocumentMessage != nil:
		setFlag(&payload.DocumentMessage.ContextInfo)
	case payload.StickerPackMessage != nil:
		setFlag(&payload.StickerPackMessage.ContextInfo)
	case payload.LocationMessage != nil:
		setFlag(&payload.LocationMessage.ContextInfo)
	}
}

const (
	// The maximum size thumbnail image we'll send in outgoing URL preview messages.
	maxPreviewThumbnailSize = 1024 * 500 // 500KiB
)

// GetMessagePayload returns a concrete WhatsApp protocol message for the given Message representation.
// The specific fields set within the protocol message, as well as its type, can depend on specific
// fields set in the Message type, and may be nested recursively (e.g. when replying to a reply).
// reply context is applied to media payloads
func setReplyContext(payload *waE2E.Message, message Message) {
	if payload == nil || message.ReplyID == "" {
		return
	}

	participant := message.OriginActor.JID
	_, quotedGroupJID, privateGroupReply := strings.Cut(
		message.OriginActor.LID,
		privateReplyGroupSeparator,
	)
	if message.Chat.IsGroup {
		participant = message.OriginActor.LID
	} else if privateGroupReply {
		participant, _, _ = strings.Cut(message.OriginActor.LID, privateReplyGroupSeparator)
		if participant == "" {
			participant = message.OriginActor.JID
		}
	}
	if participant == "" {
		return
	}

	contextInfo := &waE2E.ContextInfo{
		StanzaID:      &message.ReplyID,
		QuotedMessage: &waE2E.Message{Conversation: ptrTo(message.ReplyBody)},
		Participant:   &participant,
	}
	if privateGroupReply && quotedGroupJID != "" {
		contextInfo.RemoteJID = &quotedGroupJID
	}

	mergeContext := func(existing **waE2E.ContextInfo) {
		if *existing == nil {
			*existing = contextInfo
			return
		}
		if (*existing).StanzaID == nil {
			(*existing).StanzaID = contextInfo.StanzaID
		}
		if (*existing).QuotedMessage == nil {
			(*existing).QuotedMessage = contextInfo.QuotedMessage
		}
		if (*existing).Participant == nil {
			(*existing).Participant = contextInfo.Participant
		}
		if (*existing).RemoteJID == nil {
			(*existing).RemoteJID = contextInfo.RemoteJID
		}
	}

	switch {
	case payload.ImageMessage != nil:
		mergeContext(&payload.ImageMessage.ContextInfo)
	case payload.AudioMessage != nil:
		mergeContext(&payload.AudioMessage.ContextInfo)
	case payload.VideoMessage != nil:
		mergeContext(&payload.VideoMessage.ContextInfo)
	case payload.DocumentMessage != nil:
		mergeContext(&payload.DocumentMessage.ContextInfo)
	case payload.StickerMessage != nil:
		mergeContext(&payload.StickerMessage.ContextInfo)
	case payload.StickerPackMessage != nil:
		mergeContext(&payload.StickerPackMessage.ContextInfo)
	}
}

func (s *Session) getMessagePayload(ctx context.Context, message Message) *waE2E.Message {
	// Compose extended message when made as a reply to a different message.
	// CAN payload composition v1
	var payload *waE2E.Message
	if message.ReplyID != "" {
		participant := message.OriginActor.JID
		_, quotedGroupJID, privateGroupReply := strings.Cut(message.OriginActor.LID, privateReplyGroupSeparator)
		if message.Chat.IsGroup {
			participant = message.OriginActor.LID
		} else if privateGroupReply {
			participant, _, _ = strings.Cut(message.OriginActor.LID, privateReplyGroupSeparator)
			if participant == "" {
				participant = message.OriginActor.JID
			}
		}

		// Setting an invalid participant prevents the message from being displayed
		// at all, so we want to avoid that.
		if participant != "" {
			contextInfo := &waE2E.ContextInfo{
				StanzaID:      &message.ReplyID,
				QuotedMessage: &waE2E.Message{Conversation: ptrTo(message.ReplyBody)},
				Participant:   &participant,
			}
			// A direct response to a group message needs the original group.
			if privateGroupReply && quotedGroupJID != "" {
				contextInfo.RemoteJID = &quotedGroupJID
			}
			payload = &waE2E.Message{
				ExtendedTextMessage: &waE2E.ExtendedTextMessage{
					Text:        &message.Body,
					ContextInfo: contextInfo,
				},
			}
		} else {
			// TODO: prepend ReplyBody prenpended with ">" to Body, as a fallback for invalid
			//       OriginSenders
			payload = &waE2E.Message{Conversation: &message.Body}
		}
	}

	// Add URL preview, if any was given in message.
	if message.Preview.URL != "" {
		if payload == nil { payload = &waE2E.Message{} }
		if payload.ExtendedTextMessage == nil {
			payload.Conversation = nil
			payload.ExtendedTextMessage = &waE2E.ExtendedTextMessage{Text: &message.Body}
		}

		switch message.Preview.Kind {
		case PreviewPlain:
			payload.ExtendedTextMessage.PreviewType = ptrTo(waE2E.ExtendedTextMessage_NONE)
		case PreviewVideo:
			payload.ExtendedTextMessage.PreviewType = ptrTo(waE2E.ExtendedTextMessage_VIDEO)
		}

		payload.ExtendedTextMessage.MatchedText = &message.Preview.URL
		payload.ExtendedTextMessage.Title = &message.Preview.Title
		payload.ExtendedTextMessage.Description = &message.Preview.Description

		if len(message.Preview.Thumbnail) > 0 && len(message.Preview.Thumbnail) < maxPreviewThumbnailSize {
			data, err := media.Convert(ctx, message.Preview.Thumbnail, &previewThumbnailSpec)
			if err == nil {
				payload.ExtendedTextMessage.JPEGThumbnail = data
				if info, err := jpeg.DecodeConfig(bytes.NewReader(data)); err == nil {
					payload.ExtendedTextMessage.ThumbnailWidth = ptrTo(uint32(info.Width))
					payload.ExtendedTextMessage.ThumbnailHeight = ptrTo(uint32(info.Height))
				}
			}
		}
	}

	// Attach any inline mentions extended metadata.
	if len(message.MentionJIDs) > 0 {
		if payload == nil { payload = &waE2E.Message{} }
		if payload.ExtendedTextMessage == nil {
			payload.Conversation = nil
			payload.ExtendedTextMessage = &waE2E.ExtendedTextMessage{Text: &message.Body}
		}
		if payload.ExtendedTextMessage.ContextInfo == nil {
			payload.ExtendedTextMessage.ContextInfo = &waE2E.ContextInfo{}
		}
		payload.ExtendedTextMessage.ContextInfo.MentionedJID = message.MentionJIDs
	}

	// Process any location information in message, if possible.
	if message.Location.Latitude != 0 || message.Location.Longitude != 0 {
		// A location is the content; keep reply/mention context, not a second body.
		contextInfo := payload.GetExtendedTextMessage().GetContextInfo()
		payload = &waE2E.Message{LocationMessage: &waE2E.LocationMessage{
			DegreesLatitude: &message.Location.Latitude,
			DegreesLongitude: &message.Location.Longitude,
			AccuracyInMeters: ptrTo(uint32(message.Location.Accuracy)),
			ContextInfo: contextInfo,
		}}
	}

	if payload == nil {
		payload = &waE2E.Message{Conversation: &message.Body}
	}

	return payload
}

// GenerateMessageID returns a valid, pseudo-random message ID for use in outgoing messages.
func (s *Session) GenerateMessageID() string {
	return s.client.GenerateMessageID()
}

// SendChatState sends the given chat state notification (e.g. composing message) to WhatsApp for the
// contact specified within.
func (s *Session) SendChatState(state ChatState) error {
	if s.client == nil || s.client.Store.ID == nil {
		return fmt.Errorf("cannot send chat state for unauthenticated session")
	}

	jid, err := types.ParseJID(state.Chat.JID)
	if err != nil {
		return fmt.Errorf("could not parse sender JID for chat state: %s", err)
	}

	var presence types.ChatPresence
	switch state.Kind {
	case ChatStateComposing:
		presence = types.ChatPresenceComposing
	case ChatStatePaused:
		presence = types.ChatPresencePaused
	}

	return s.client.SendChatPresence(s.ctx, jid, presence, types.ChatPresenceMedia(state.Media))
}

// SendReceipt sends a read receipt to WhatsApp for the message IDs specified within.
func (s *Session) SendReceipt(receipt Receipt) error {
	if s.client == nil || s.client.Store.ID == nil {
		return fmt.Errorf("cannot send receipt for unauthenticated session")
	}

	var chatJID, senderLID types.JID
	var err error

	if chatJID, err = types.ParseJID(receipt.Chat.JID); err != nil {
		return fmt.Errorf("could not parse chat JID for receipt: %s", err)
	}

	if receipt.Chat.IsGroup {
		if senderLID, err = types.ParseJID(receipt.OriginActor.LID); err != nil {
			return fmt.Errorf("could not parse chat OriginActor.LID for receipt: %s", err)
		}
	}

	ids := slices.Clone(receipt.MessageIDs)
	return s.client.MarkRead(s.ctx, ids, time.Unix(receipt.Timestamp, 0), chatJID, senderLID)
}

// SendPresence sets the activity state and (optional) status message for the current session and
// user. An error is returned if setting availability fails for any reason.
// passiveWhatsAppPresence keeps the linked device connected without advertising it
// as the foreground WhatsApp session. This preserves notifications on the phone.
func passiveWhatsAppPresence(_ PresenceKind) types.Presence {
	return types.PresenceUnavailable
}

func (s *Session) SendPresence(presence PresenceKind, statusMessage string) error {
	client := s.currentClient()
	if client == nil || client.Store == nil || client.Store.ID == nil {
		return fmt.Errorf("cannot send presence for unauthenticated session")
	}

	s.queuePresenceRefresh(presence)
	err := client.SendPresence(s.ctx, passiveWhatsAppPresence(presence))

	if err == nil && statusMessage != "" {
		err = client.SetStatusMessage(s.ctx, statusMessage)
	}

	return err
}

// GetContacts subscribes to the WhatsApp roster currently stored in the Session's internal state.
// If `refresh` is `true`, FetchRoster will pull application state from the remote service and
// synchronize any contacts found with the adapter.
func preferContactCandidate(current, candidate Contact) bool {
	if current.IsFriend != candidate.IsFriend {
		return candidate.IsFriend
	}
	return current.Name == "" && candidate.Name != ""
}

func (s *Session) GetContacts(refresh bool) ([]Contact, error) {
	// CAN bounded roster snapshot v2
	client := s.currentClient()
	if client == nil || client.Store == nil || client.Store.ID == nil {
		return nil, fmt.Errorf("cannot get contacts for unauthenticated session")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()

	// Synchronize remote application state with local state if requested.
	if refresh {
		err := client.FetchAppState(ctx, appstate.WAPatchCriticalUnblockLow, false, false)
		if err != nil {
			s.gateway.logger.Warnf("Could not get app state from server: %s", err)
		}
	}

	// Synchronize local contact state with overarching gateway for all local contacts.
	data, err := client.Store.Contacts.GetAllContacts(ctx)
	if err != nil {
		return nil, fmt.Errorf("failed getting local contacts: %s", err)
	}

	contactsByJID := make(map[string]Contact)
	for jid, info := range data {
		candidate := newContact(client, newActor(ctx, client, jid), info)
		key := candidate.Actor.JID
		if key == "" {
			continue
		}
		current, found := contactsByJID[key]
		if !found || preferContactCandidate(current, candidate) {
			contactsByJID[key] = candidate
		}
	}

	contacts := make([]Contact, 0, len(contactsByJID))
	for _, contact := range contactsByJID {
		contacts = append(contacts, contact)
	}
	if s.currentClient() != client { return nil, fmt.Errorf("roster session changed") }
	return contacts, nil
}

func (s *Session) SubscribeToPresences() error {
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()
	return s.subscribeToPresences(ctx, s.currentClient())
}

func (s *Session) subscribeToPresences(ctx context.Context, client *whatsmeow.Client) error {
	if client == nil || client.Store == nil || client.Store.Contacts == nil {
		return nil
	}

	data, err := client.Store.Contacts.GetAllContacts(ctx)
	if err != nil {
		return fmt.Errorf("failed getting local contacts: %s", err)
	}
	for jid := range data {
		if jid.Server != types.DefaultUserServer {
			continue
		}

		if err = client.SubscribePresence(ctx, jid); err != nil {
			s.gateway.logger.Debugf("Failed to subscribe to presence for %s", jid)
		}
	}
	return nil
}

// GetGroups returns a list of all group-chats currently joined in WhatsApp, along with additional
// information on present participants.
func (s *Session) GetGroups() ([]Group, error) {
	// CAN bounded roster snapshot v2
	client := s.currentClient()
	if client == nil || client.Store == nil || client.Store.ID == nil {
		return nil, fmt.Errorf("cannot get groups for unauthenticated session")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	defer cancel()

	data, err := client.GetJoinedGroups(ctx)
	if err != nil {
		return nil, fmt.Errorf("failed getting groups: %s", err)
	}

	var groups []Group
	for _, info := range data {
		groups = append(groups, newGroup(ctx, client, info))
	}

	if s.currentClient() != client { return nil, fmt.Errorf("roster session changed") }
	return groups, nil
}

// CreateGroup attempts to create a new WhatsApp group for the given human-readable name and
// participant JIDs given.
func (s *Session) CreateGroup(name string, participants []string) (Group, error) {
	if s.client == nil || s.client.Store.ID == nil {
		return Group{}, fmt.Errorf("cannot create group for unauthenticated session")
	}

	var jids []types.JID
	for _, p := range participants {
		jid, err := types.ParseJID(p)
		if err != nil {
			return Group{}, fmt.Errorf("could not parse participant JID: %s", err)
		}

		jids = append(jids, jid)
	}

	req := whatsmeow.ReqCreateGroup{Name: name, Participants: jids}
	info, err := s.client.CreateGroup(s.ctx, req)
	if err != nil {
		return Group{}, fmt.Errorf("could not create group: %s", err)
	}

	return newGroup(s.ctx, s.client, info), nil
}

// LeaveGroup attempts to remove our own user from the given WhatsApp group, for the JID given.
func (s *Session) LeaveGroup(resourceID string) error {
	if s.client == nil || s.client.Store.ID == nil {
		return fmt.Errorf("cannot leave group for unauthenticated session")
	}

	jid, err := types.ParseJID(resourceID)
	if err != nil {
		return fmt.Errorf("could not parse JID for leaving group: %s", err)
	}

	return s.client.LeaveGroup(s.ctx, jid)
}

// GetAvatar fetches a profile picture for the Contact or Group JID given. If a non-empty `avatarID`
// is also given, GetAvatar will return an empty [Avatar] instance with no error if the remote state
// for the given ID has not changed.
func (s *Session) GetAvatar(resourceID, avatarID string) (Avatar, error) {
	if s.client == nil || s.client.Store.ID == nil {
		return Avatar{}, fmt.Errorf("cannot get avatar for unauthenticated session")
	}

	jid, err := types.ParseJID(resourceID)
	if err != nil {
		return Avatar{}, fmt.Errorf("could not parse JID for avatar: %s", err)
	}

	p, err := s.client.GetProfilePictureInfo(s.ctx, jid, &whatsmeow.GetProfilePictureParams{ExistingID: avatarID})
	if errors.Is(err, whatsmeow.ErrProfilePictureNotSet) || errors.Is(err, whatsmeow.ErrProfilePictureUnauthorized) {
		return Avatar{}, nil
	} else if err != nil {
		return Avatar{}, fmt.Errorf("could not get avatar: %s", err)
	} else if p != nil {
		return Avatar{ID: p.ID, URL: p.URL}, nil
	}

	return Avatar{ID: avatarID}, nil
}

// Enqueue an avatar refresh. Processed in an anonymous go routine defined in Session.login()
func (s *Session) RequestAvatar(resourceID, avatarID string) {
	go func() {
		s.avatarMutex.Lock()
		defer s.avatarMutex.Unlock()

		now := time.Now()
		timeSinceLastCall := now.Sub(s.lastAvatarCall)
		if timeSinceLastCall < requestAvatarInterval {
			time.Sleep(requestAvatarInterval + time.Duration(rand.Int63n(int64(requestAvatarInterval))-int64(requestAvatarInterval/2)))
		}
		avatar, err := s.GetAvatar(resourceID, avatarID)
		s.lastAvatarCall = time.Now()

		if err != nil {
			s.gateway.logger.Errorf("Skipped fetching avatar for %s: %s", resourceID, err)
			return
		}
		if avatar.ID == avatarID {
			s.gateway.logger.Debugf("Cached avatar is up-to-date")
			return
		}
		jid, err := types.ParseJID(resourceID)
		if err != nil {
			return
		}
		avatar.ResourceID = resourceID
		avatar.IsGroup = jid.Server == DefaultGroupServer
		s.propagateEvent(EventAvatar, &EventPayload{Avatar: avatar})
	}()
}

// SetAvatar updates the profile picture for the Contact or Group JID given; it can also update the
// profile picture for our own user by providing an empty JID. The unique picture ID is returned,
// typically used as a cache reference or in providing to future calls for [Session.GetAvatar].
func (s *Session) SetAvatar(resourceID string, avatar []byte) (string, error) {
	if s.client == nil || s.client.Store.ID == nil {
		return "", fmt.Errorf("cannot set avatar for unauthenticated session")
	}

	var jid types.JID
	var err error

	// Setting the profile picture for the user expects an empty `resourceID`.
	if resourceID == "" {
		jid = types.EmptyJID
	} else if jid, err = types.ParseJID(resourceID); err != nil {
		return "", fmt.Errorf("could not parse JID for avatar: %s", err)
	}

	if len(avatar) == 0 {
		return s.client.SetGroupPhoto(s.ctx, jid, nil)
	} else {
		// Ensure avatar is in JPEG format, and convert before setting if needed.
		data, err := media.Convert(s.ctx, avatar, &media.Spec{MIME: media.TypeJPEG})
		if err != nil {
			return "", fmt.Errorf("failed converting avatar to JPEG: %s", err)
		}

		return s.client.SetGroupPhoto(s.ctx, jid, data)
	}
}

// SetGroupName updates the name of a WhatsApp group for the Group JID given.
func (s *Session) SetGroupName(resourceID, name string) error {
	if s.client == nil || s.client.Store.ID == nil {
		return fmt.Errorf("cannot set group name for unauthenticated session")
	}

	jid, err := types.ParseJID(resourceID)
	if err != nil {
		return fmt.Errorf("could not parse JID for group name change: %s", err)
	}

	return s.client.SetGroupName(s.ctx, jid, name)
}

// SetGroupName updates the topic of a WhatsApp group for the Group JID given.
func (s *Session) SetGroupTopic(resourceID, topic string) error {
	if s.client == nil || s.client.Store.ID == nil {
		return fmt.Errorf("cannot set group topic for unauthenticated session")
	}

	jid, err := types.ParseJID(resourceID)
	if err != nil {
		return fmt.Errorf("could not parse JID for group topic change: %s", err)
	}

	return s.client.SetGroupTopic(s.ctx, jid, "", "", topic)

}

// UpdateGroupParticipants processes changes to the given group's participants, including additions,
// removals, and changes to privileges. Participant JIDs given must be part of the authenticated
// session's roster at least, and must also be active group participants for other types of changes.
func (s *Session) UpdateGroupParticipants(resourceID string, participants []GroupParticipant) ([]GroupParticipant, error) {
	if s.client == nil || s.client.Store.ID == nil {
		return nil, fmt.Errorf("cannot update group participants for unauthenticated session")
	}

	jid, err := types.ParseJID(resourceID)
	if err != nil {
		return nil, fmt.Errorf("could not parse JID for group participant update: %s", err)
	}

	var changes = make(map[whatsmeow.ParticipantChange][]types.JID)
	for _, p := range participants {
		participantJID, err := types.ParseJID(p.Actor.JID)
		if err != nil {
			return nil, fmt.Errorf("could not parse participant JID for update: %s", err)
		}

		if c, err := s.client.Store.Contacts.GetContact(s.ctx, participantJID); err != nil {
			return nil, fmt.Errorf("could not fetch contact for participant: %s", err)
		} else if !c.Found {
			return nil, fmt.Errorf("cannot update group participant for contact '%s' not in roster", participantJID)
		}

		c := p.Action.toParticipantChange()
		changes[c] = append(changes[c], participantJID)
	}

	var result []GroupParticipant
	for change, participantJIDs := range changes {
		participants, err := s.client.UpdateGroupParticipants(s.ctx, jid, participantJIDs, change)
		if err != nil {
			return nil, fmt.Errorf("failed setting group affiliation: %s", err)
		}
		for i := range participants {
			p := newGroupParticipant(s.ctx, s.client, participants[i])
			if p.Actor.JID == "" {
				continue
			}
			result = append(result, p)
		}
	}

	return result, nil
}

// FindContact attempts to check for a registered contact on WhatsApp corresponding to the given
// phone number, returning a concrete instance if found; typically, only the contact JID is set. No
// error is returned if no contact was found, but any unexpected errors will otherwise be returned
// directly.
func (s *Session) FindContact(phone string) (Contact, error) {
	if s.client == nil || s.client.Store.ID == nil {
		return Contact{}, fmt.Errorf("cannot find contact for unauthenticated session")
	}

	jid := types.NewJID(phone, DefaultUserServer)
	actor := newActor(s.ctx, s.client, jid)
	if info, err := s.client.Store.Contacts.GetContact(s.ctx, jid); err == nil && info.Found {
		if c := newContact(s.client, actor, info); c.Actor.JID != "" {
			return c, nil
		}
	}

	resp, err := s.client.IsOnWhatsApp(s.ctx, []string{phone})
	if err != nil {
		return Contact{}, fmt.Errorf("failed looking up contact '%s': %s", phone, err)
	} else if len(resp) != 1 {
		return Contact{}, fmt.Errorf("failed looking up contact '%s': invalid response", phone)
	} else if !resp[0].IsIn || resp[0].JID.IsEmpty() {
		return Contact{}, nil
	}

	actor = newActor(s.ctx, s.client, resp[0].JID)
	if actor.JID == "" {
		return Contact{}, nil
	}

	return Contact{Actor: actor}, nil
}

// RequestMessageHistory sends and asynchronous request for message history related to the given
// resource (e.g. Contact or Group JID), ending at the oldest message given. Messages returned from
// history should then be handled as a `HistorySync` event of type `ON_DEMAND`, in the session-wide
// event handler. An error will be returned if requesting history fails for any reason.
func (s *Session) RequestMessageHistory(resourceID string, oldestMessage Message) error {
	if s.client == nil || s.client.Store.ID == nil {
		return fmt.Errorf("cannot request history for unauthenticated session")
	}

	jid, err := types.ParseJID(resourceID)
	if err != nil {
		return fmt.Errorf("could not parse JID for history request: %s", err)
	}

	info := &types.MessageInfo{
		ID:            oldestMessage.ID,
		MessageSource: types.MessageSource{Chat: jid, IsFromMe: oldestMessage.Actor.IsMe},
		Timestamp:     time.Unix(oldestMessage.Timestamp, 0).UTC(),
	}

	req := s.client.BuildHistorySyncRequest(info, maxHistorySyncMessages)
	_, err = s.client.SendMessage(s.ctx, s.device.JID().ToNonAD(), req, whatsmeow.SendRequestExtra{Peer: true})
	if err != nil {
		return fmt.Errorf("failed to request history for %s: %s", resourceID, err)
	}

	return nil
}

// SetEventHandler assigns the given handler function for propagating internal events into the Python
// gateway. Note that the event handler function is not entirely safe to use directly, and all calls
// should instead be sent to the [Gateway] via its internal call channel.
func (s *Session) SetEventHandler(h HandleEventFunc) {
	s.eventHandler = h
}

func shouldDropTransientEvent(kind EventKind, payload *EventPayload) bool {
	switch kind {
	case EventPresence:
		// Python currently ignores presence without a canonical phone JID.
		return payload.Presence.Actor.JID == ""
	case EventChatState:
		state := payload.ChatState
		// Group participants may legitimately be LID-only. Only incomplete direct states are useless.
		return !state.Chat.IsGroup && (state.Actor.JID == "" || state.Chat.JID == "")
	default:
		return false
	}
}

// PropagateEvent handles the given event kind and payload with the adapter event handler defined in
// [Session.SetEventHandler].
func (s *Session) propagateEvent(kind EventKind, payload *EventPayload) {
    s.canQueuePayload(kind, payload)
}

// HandleEvent processes the given incoming WhatsApp event, checking its concrete type and
// propagating it to the adapter event handler. Unknown or unhandled events are ignored, and any
// errors that occur during processing are logged.
func (s *Session) handleEvent(evt any) {
	client := s.currentClient()
	if client == nil {
		return
	}
	s.canTraceCallSignal(evt)
	s.gateway.logger.Debugf("Handling event '%T': %+v", evt, jsonStringer{evt})

	switch evt := evt.(type) {
	case *events.AppState:
		if action := evt.GetCallLogAction(); action != nil {
			if record := action.GetCallLogRecord(); record != nil {
				kind, payload := newCallLogRecordEvent(s.ctx, client, record, "app_state")
				if payload != nil {
					s.propagateEvent(kind, payload)
				}
			}
		}
	case *events.AppStateSyncComplete:
		if client.IsLoggedIn() && client.Store.ID != nil && evt.Name == appstate.WAPatchCriticalBlock {
			s.propagateEvent(EventConnect, &EventPayload{Connect: Connect{JID: s.device.JID().ToNonAD().String()}})
			if err := client.SendPresence(s.ctx, passiveWhatsAppPresence(PresenceAvailable)); err != nil {
				s.gateway.logger.Warnf("Failed to send available presence: %s", err)
			}
		}
	case *events.ConnectFailure:
		switch evt.Reason {
		case events.ConnectFailureLoggedOut:
			// These events are handled separately.
		default:
			s.gateway.logger.Errorf("failed to connect: %s", evt.Message)
			s.propagateEvent(EventConnect, &EventPayload{Connect: Connect{Error: evt.Message}})
		}
	case *events.Disconnected:
		s.propagateEvent(EventConnect, &EventPayload{Connect: Connect{Error: "can-link:disconnected"}})
	case *events.ClientOutdated:
		s.propagateEvent(EventConnect, &EventPayload{Connect: Connect{Error: "can-link:client_outdated"}})
	case *events.StreamReplaced:
		s.propagateEvent(EventConnect, &EventPayload{Connect: Connect{Error: "can-link:stream_replaced"}})
	case *events.PairError:
		s.propagateEvent(EventConnect, &EventPayload{Connect: Connect{Error: "can-link:pair_failed"}})
	case *events.Connected, *events.PushNameSetting:
		// Backfill only after authentication, also on a real reconnect.
		if _, connected := evt.(*events.Connected); connected {
			s.canCallLog("CAN_CALL stage=diagnostics_ready")
			go s.syncCallLogAppState(client)
		}
		if !client.IsLoggedIn() || client.Store.ID == nil {
			return
		}
		s.propagateEvent(EventConnect, &EventPayload{Connect: Connect{JID: s.device.JID().ToNonAD().String()}})
		if err := client.SendPresence(s.ctx, passiveWhatsAppPresence(PresenceAvailable)); err != nil {
			s.gateway.logger.Warnf("Failed to send available presence: %s", err)
		}
	case *events.HistorySync:
        ctx := s.ctx
        d := s.canGetInbound()
        d.enqueue(d.history, func(){
            if ctx.Err()==nil && s.currentClient()==client { s.canHandleHistory(ctx,client,evt) }
        }, false)
    case *events.Message:
        s.canQueueMessage(s.ctx,client,evt)
	case *events.Receipt:
		s.propagateEvent(newReceiptEvent(s.ctx, client, evt))
	case *events.MarkChatAsRead:
		s.propagateEvent(newMarkChatAsReadEvent(s.ctx, client, evt))
	case *events.Presence:
		s.propagateEvent(newPresenceEvent(s.ctx, client, evt))
	case *events.Contact:
		s.propagateEvent(newContactEvent(s.ctx, client, evt))
	case *events.PushName:
		s.propagateEvent(newContactEventFromPushName(s.ctx, client, evt))
	case *events.JoinedGroup:
		s.propagateEvent(EventGroup, &EventPayload{Group: newGroup(s.ctx, client, &evt.GroupInfo)})
	case *events.GroupInfo:
		s.propagateEvent(newGroupEvent(s.ctx, client, evt))
	case *events.ChatPresence:
		s.propagateEvent(newChatStateEvent(s.ctx, client, evt))
	case *events.CallOffer:
		s.propagateEvent(newCallEvent(
			s.ctx, client, callStateOffered, callDirectionIncoming, canCallOfferKind(evt.Data), 1, "", evt.BasicCallMeta,
		))
	case *events.CallOfferNotice:
		s.propagateEvent(newCallEvent(
			s.ctx, client, callStateOffered, callDirectionIncoming, callKindFromMedia(evt.Media), 1, "", evt.BasicCallMeta,
		))
	case *events.CallAccept:
		s.propagateEvent(newCallEvent(
			s.ctx, client, callStateAccepted, callDirectionFromMeta(client, evt.BasicCallMeta), callKindUnknown, 2, "", evt.BasicCallMeta,
		))
	case *events.CallReject:
		s.propagateEvent(newCallEvent(
			s.ctx, client, callStateRejected, callDirectionOutgoing, callKindUnknown, 3, "", evt.BasicCallMeta,
		))
	case *events.CallTerminate:
		reason := strings.TrimSpace(evt.Reason)
		state := callStateFromTerminateReason(reason)
		sequence := callSequenceFromTerminateReason(reason)
		if reason == "accepted_elsewhere" {
			// Another linked device answered; this is not a terminal event.
			state = callStateAccepted
			sequence = 2
			reason = ""
		}
		s.propagateEvent(newCallEvent(
			s.ctx, client, state, callDirectionFromMeta(client, evt.BasicCallMeta), callKindUnknown, sequence, reason, evt.BasicCallMeta,
		))
	case *events.LoggedOut:
		s.stopPresenceRefresh()
		if client != nil {
			client.Disconnect()
			if client.Store != nil {
				if err := client.Store.Delete(s.ctx); err != nil {
					s.gateway.logger.Warnf("Unable to delete local device state on logout: %s", err)
				}
			}
			s.clearClient(client)
		}
		s.ctxCancel(nil)
		s.propagateEvent(EventLoggedOut, &EventPayload{LoggedOut: LoggedOut{Reason: evt.Reason.String()}})
		s.canStopInbound()
	case *events.PairSuccess:
		if client.Store.ID == nil {
			s.gateway.logger.Errorf("Pairing succeeded, but device ID is missing")
			return
		}
		s.device.ID = client.Store.ID.String()
		s.propagateEvent(EventPairDeviceID, &EventPayload{PairDeviceID: s.device.ID})
		if err := s.gateway.CleanupSession(LinkedDevice{ID: s.device.ID}); err != nil {
			s.gateway.logger.Warnf("Failed to clean up devices after pair: %s", err)
		}
	case *events.KeepAliveTimeout:
		if evt.ErrorCount > keepAliveFailureThreshold {
			s.gateway.logger.Debugf("Forcing reconnection after keep-alive timeouts...")
			go func() {
				var interval = keepAliveMinRetryInterval
				client.Disconnect()
				for {
					err := client.Connect()
					if err == nil || err == whatsmeow.ErrAlreadyConnected {
						break
					}

					s.gateway.logger.Errorf("Error reconnecting after keep-alive timeouts, retrying in %s: %s", interval, err)
					time.Sleep(interval)

					if interval > keepAliveMaxRetryInterval {
						interval = keepAliveMaxRetryInterval
					} else if interval < keepAliveMaxRetryInterval {
						interval *= 2
					}
				}
			}()
		}
	}
}

// a JSONStringer is a value that returns a JSON-encoded, multi-line version of itself in calls to
// [String].
type jsonStringer struct{ v any }

// String returns a multi-line, indented, JSON representation of the [jsonStringer] value.
func (j jsonStringer) String() string {
	buf, _ := json.MarshalIndent(j.v, "", "    ")
	return string(buf)
}

// PtrTo returns a pointer to the given value, and is used for convenience when converting between
// concrete and pointer values without assigning to a variable.
func ptrTo[T any](t T) *T {
	return &t
}

// canGroupReadPatch binds the app-state horizon to the original message, never wall-clock time.
func canGroupReadPatch(receipt Receipt) (appstate.PatchInfo, error) {
	if !receipt.Chat.IsGroup || len(receipt.MessageIDs) != 1 || receipt.MessageIDs[0] == "" || receipt.Timestamp <= 0 {
		return appstate.PatchInfo{}, fmt.Errorf("invalid group read horizon")
	}
	chat, err := types.ParseJID(receipt.Chat.JID)
	if err != nil || chat.Server != types.GroupServer {
		return appstate.PatchInfo{}, fmt.Errorf("invalid group read chat")
	}
	sender, err := types.ParseJID(receipt.OriginActor.LID)
	if err != nil || sender.IsEmpty() || receipt.OriginActor.IsMe || sender.Server != types.HiddenUserServer {
		return appstate.PatchInfo{}, fmt.Errorf("invalid group read sender")
	}
	key := &waCommon.MessageKey{
		RemoteJID: ptrTo(chat.String()), FromMe: ptrTo(false),
		ID: ptrTo(receipt.MessageIDs[0]), Participant: ptrTo(sender.ToNonAD().String()),
	}
	return appstate.BuildMarkChatAsRead(chat, true, time.Unix(receipt.Timestamp, 0), key), nil
}

// MarkChatRead synchronizes the chat state separately from per-message read receipts.
func (s *Session) MarkChatRead(receipt Receipt) error {
	client := s.currentClient()
	if client == nil || client.Store == nil || client.Store.ID == nil {
		return fmt.Errorf("cannot synchronize read state for unauthenticated session")
	}
	patch, err := canGroupReadPatch(receipt)
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(s.ctx, 45*time.Second)
	defer cancel()
	return canSendGroupReadWithRecovery(ctx, patch, client.SendAppState, func(ctx context.Context, name appstate.WAPatchName, full, onlyNew bool) error {
		s.gateway.logger.Infof("CAN_READ_STATE stage=recover collection=%s", name)
		err := client.FetchAppState(ctx, name, full, onlyNew)
		if err != nil {
			s.gateway.logger.Warnf("CAN_READ_STATE stage=recovery_failed collection=%s", name)
		} else {
			s.gateway.logger.Infof("CAN_READ_STATE stage=recovered collection=%s", name)
		}
		return err
	}, func(ctx context.Context, name appstate.WAPatchName) error {
		// CAN primary snapshot recovery v1: never reset collections or devices.
		_, err := client.SendPeerMessage(ctx, whatsmeow.BuildAppStateRecoveryRequest(name))
		if err == nil {
			s.gateway.logger.Infof("CAN_READ_STATE stage=peer_recovery_requested collection=%s", name)
		}
		return err
	})
}

func (s *Session) canHandleHistory(ctx context.Context, client *whatsmeow.Client, evt *events.HistorySync) {

    emit:=func(kind EventKind,payload *EventPayload){
        if ctx.Err()==nil && s.currentClient()==client {
            if kind==EventCall && payload!=nil { s.canObserveCall(payload) }
            s.canDeliverInbound(kind,payload)
        }
    }
		for _, record := range evt.Data.GetCallLogRecords() {
			kind, payload := newCallLogRecordEvent(ctx, client, record, "history_sync")
			if payload != nil {
				emit(kind, payload)
			}
		}
		switch evt.Data.GetSyncType() {
		case waHistorySync.HistorySync_PUSH_NAME:
			for _, n := range evt.Data.GetPushnames() {
				emit(newContactEventFromHistory(ctx, client, n))
			}
		case waHistorySync.HistorySync_INITIAL_BOOTSTRAP, waHistorySync.HistorySync_RECENT, waHistorySync.HistorySync_ON_DEMAND:
			for _, c := range evt.Data.GetConversations() {
				for _, msg := range c.GetMessages() {
					if ctx.Err()!=nil || s.currentClient()!=client { return }
					emit(newEventFromHistory(ctx, client, msg.GetMessage()))
				}
			}
		}

}

// canLinkQRPayload preserves WhatsMeow's per-code expiry across the existing gopy API.
func canLinkQRPayload(code string, ttl time.Duration) string {
    data, _ := json.Marshal(map[string]any{"code": code, "ttl": ttl.Seconds()})
    return string(data)
}
