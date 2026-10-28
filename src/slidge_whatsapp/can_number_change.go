package whatsapp

import (
	"context"

	"go.mau.fi/whatsmeow"
	waCommon "go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/types"
)

// canHistoricalActor uses explicit FromMe before a possibly obsolete participant PN.
// Unknown remote senders are not converted into the local user.
func canHistoricalActor(ctx context.Context, client *whatsmeow.Client, key *waCommon.MessageKey, participant string) (Actor, bool) {
	if key.GetFromMe() {
		actor := newActor(ctx, client, client.Store.GetJID(), client.Store.GetLID())
		actor.IsMe = true
		return actor, actor.JID != "" || actor.LID != ""
	}
	if participant == "" {
		participant = key.GetParticipant()
	}
	jid, err := types.ParseJID(participant)
	if err != nil || jid.User == "" ||
		(jid.Server != types.DefaultUserServer && jid.Server != types.HiddenUserServer) {
		return Actor{}, false
	}
	actor := newActor(ctx, client, jid)
	return actor, actor.JID != "" || actor.LID != ""
}
