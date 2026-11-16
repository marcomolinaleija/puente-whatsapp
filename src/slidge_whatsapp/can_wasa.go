package whatsapp

// CAN WASA v1: Muse encrypts its answers with a per-bot root secret that the
// phone shares through app state ("wasa_root_secret") and, live, through a
// RootSecretDistributeMessage. The pinned whatsmeow ignores both, so the bridge
// stores those secrets as upstream does: (bot chat, own LID, root ID). Logs
// carry counts and public bot identities only, never secrets or contents.

import (
	"context"
	"sync"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/appstate"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waServerSync"
	"go.mau.fi/whatsmeow/proto/waSyncAction"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
)

const (
	canWASAScanDelay   = 45 * time.Second
	canWASAScanTimeout = 90 * time.Second
	canWASAMaxPatches  = 64
)

var canWASAScanned sync.Map // *whatsmeow.Client -> true

var canWASACollections = []appstate.WAPatchName{
	appstate.WAPatchRegular, appstate.WAPatchRegularHigh, appstate.WAPatchRegularLow,
	appstate.WAPatchCriticalUnblockLow, appstate.WAPatchCriticalBlock,
}

// canWASARootSecrets selects the secrets to store and the active (latest epoch) root.
func canWASARootSecrets(bot, ownLID types.JID, entries []*waSyncAction.WASARootSecretAction_RootSecretEntry) ([]store.MessageSecretInsert, types.MessageID) {
	inserts := make([]store.MessageSecretInsert, 0, len(entries))
	var active *waSyncAction.WASARootSecretAction_RootSecretEntry
	for _, entry := range entries {
		if entry.GetID() == "" || len(entry.GetRootSecret()) != 32 {
			continue
		}
		inserts = append(inserts, store.MessageSecretInsert{
			Chat: bot, Sender: ownLID, ID: types.MessageID(entry.GetID()), Secret: entry.GetRootSecret(),
		})
		if active == nil || entry.GetEpoch() > active.GetEpoch() {
			active = entry
		}
	}
	if active == nil {
		return inserts, ""
	}
	return inserts, types.MessageID(active.GetID())
}

// canApplyWASAMutation stores the root secrets of one wasa_root_secret mutation.
func (s *Session) canApplyWASAMutation(ctx context.Context, client *whatsmeow.Client, index []string, action *waSyncAction.SyncActionValue, source string) bool {
	if client == nil || client.Store == nil || len(index) < 2 || index[0] != appstate.IndexWasaRootSecretAction {
		return false
	}
	bot, err := types.ParseJID(index[1])
	ownLID := client.Store.GetLID().ToNonAD()
	if err != nil || bot.Server != types.BotServer || ownLID.IsEmpty() {
		client.Log.Warnf("CAN_WASA ignored mutation source=%s valid_bot=%t own_lid=%t", source, err == nil && bot.Server == types.BotServer, !ownLID.IsEmpty())
		return false
	}
	inserts, active := canWASARootSecrets(bot.ToNonAD(), ownLID, action.GetWasaRootSecretAction().GetSecrets())
	if len(inserts) == 0 {
		return false
	}
	if err := client.Store.MsgSecrets.PutMessageSecrets(ctx, inserts); err != nil {
		client.Log.Warnf("CAN_WASA store failed source=%s bot=%s", source, bot.User)
		return false
	}
	known := client.CANWASARootSecretID(bot) != ""
	client.CANSetWASARootSecretID(bot, active)
	client.Log.Debugf("CAN_WASA stored source=%s bot=%s secrets=%d active=%t", source, bot.User, len(inserts), active != "")
	if !known && active != "" {
		s.canAnnounceBotContact(bot)
	}
	return true
}

// canStoreRootSecretDistribution mirrors upstream: a root secret the phone sends
// to its companions belongs to the bot chat named in the message.
func (s *Session) canStoreRootSecretDistribution(ctx context.Context, client *whatsmeow.Client, evt *events.Message) {
	if client == nil || evt == nil || !evt.Info.IsFromMe {
		return
	}
	chatJID := ""
	var secret []byte
	for _, candidate := range canDistributionCandidates(evt) {
		if jid := candidate.GetRootSecretDistributeMessage().GetChatJID(); jid != "" {
			chatJID = jid
		}
		if value := candidate.GetMessageContextInfo().GetMessageSecret(); len(value) > 0 {
			secret = value
		}
	}
	if chatJID == "" {
		return
	}
	bot, err := types.ParseJID(chatJID)
	if err != nil || bot.Server != types.BotServer || len(secret) != 32 {
		client.Log.Warnf("CAN_WASA distribution ignored valid_bot=%t secret=%t", err == nil && bot.Server == types.BotServer, len(secret) == 32)
		return
	}
	if err := client.Store.MsgSecrets.PutMessageSecret(ctx, bot.ToNonAD(), evt.Info.Sender, evt.Info.ID, secret); err != nil {
		client.Log.Warnf("CAN_WASA distribution store failed bot=%s", bot.User)
		return
	}
	known := client.CANWASARootSecretID(bot) != ""
	client.CANSetWASARootSecretID(bot, evt.Info.ID)
	client.Log.Debugf("CAN_WASA stored source=distribution bot=%s", bot.User)
	if !known {
		s.canAnnounceBotContact(bot)
	}
}

// canDistributionCandidates returns the raw, device-sent and unwrapped payloads.
func canDistributionCandidates(evt *events.Message) []*waE2E.Message {
	candidates := make([]*waE2E.Message, 0, 3)
	for _, msg := range []*waE2E.Message{evt.RawMessage, evt.RawMessage.GetDeviceSentMessage().GetMessage(), evt.Message} {
		if msg != nil {
			candidates = append(candidates, msg)
		}
	}
	return candidates
}

func (s *Session) canAnnounceBotContact(bot types.JID) {
	chat, ok := canBotChatJID(bot)
	if !ok {
		return
	}
	s.propagateEvent(EventContact, &EventPayload{Contact: Contact{
		Actor: Actor{JID: chat.String()}, Name: canBotName(bot), IsFriend: true,
	}})
}

// canReadOnlyAppState lets a private Processor decode snapshots without touching
// the stored app state versions or MACs, so live sync stays authoritative.
type canReadOnlyAppState struct{ store.AppStateStore }

func (canReadOnlyAppState) PutAppStateVersion(context.Context, string, uint64, [128]byte) error {
	return nil
}
func (canReadOnlyAppState) DeleteAppStateVersion(context.Context, string) error { return nil }
func (canReadOnlyAppState) PutAppStateMutationMACs(context.Context, string, uint64, []store.AppStateMutationMAC) error {
	return nil
}
func (canReadOnlyAppState) DeleteAppStateMutationMACs(context.Context, string, [][]byte) error {
	return nil
}

// canScanWASA reads app state collections without dispatching events and
// stores any WASA root secrets. It runs once per client and only when the
// regular sync did not already provide Muse's root.
func (s *Session) canScanWASA(client *whatsmeow.Client) {
	select {
	case <-time.After(canWASAScanDelay):
	case <-s.ctx.Done():
		return
	}
	if s.currentClient() != client || client.Store == nil || client.Store.ID == nil {
		return
	}
	if client.CANWASARootSecretID(whatsmeow.CANMuseJID) != "" {
		client.Log.Debugf("CAN_WASA scan skipped reason=root_known")
		return
	}
	ctx, cancel := context.WithTimeout(s.ctx, canWASAScanTimeout)
	defer cancel()
	device := *client.Store
	device.AppState = canReadOnlyAppState{client.Store.AppState}
	proc := appstate.NewProcessor(&device, client.Log.Sub("CANWASA"))
	internals := client.DangerousInternals()
	found := 0
	for _, name := range canWASACollections {
		state := appstate.HashState{}
		wantSnapshot := true
		for page := 0; page < canWASAMaxPatches; page++ {
			list, err := internals.FetchAppStatePatches(ctx, name, state.Version, wantSnapshot)
			if err != nil {
				client.Log.Warnf("CAN_WASA scan fetch failed collection=%s error=%q", name, canShortError(err))
				break
			}
			wantSnapshot = false
			mutations, next, err := proc.DecodePatches(ctx, list, state, false)
			if err != nil {
				client.Log.Warnf("CAN_WASA scan decode failed collection=%s error=%q", name, canShortError(err))
				break
			}
			for _, mutation := range mutations {
				if mutation.Operation == waServerSync.SyncdMutation_SET &&
					s.canApplyWASAMutation(ctx, client, mutation.Index, mutation.Action, "scan:"+string(name)) {
					found++
				}
			}
			state = next
			if !list.HasMorePatches {
				break
			}
		}
	}
	client.Log.Debugf("CAN_WASA scan complete collections=%d found=%d", len(canWASACollections), found)
}
