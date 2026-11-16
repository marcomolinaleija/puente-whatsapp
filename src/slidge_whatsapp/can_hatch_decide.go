package whatsapp

// CAN Muse approvals: answer a pending approval exactly as the official client
// does (hitl.approval.decide over AI_METADATA_OPERATION). The client sends a
// confirmed local command; Python turns it into a marked plain message and
// SendMessage hands it here instead of sending text to WhatsApp. Only an
// approval that is still pending, by its ID, with an option Muse offered, can
// be answered, and each approval is answered at most once.

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
	"sync"
	"time"

	"google.golang.org/protobuf/proto"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waAICommon"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
)

// canApprovalCommandMarker prefixes the internal message built by Python for
// "/muse-decidir <number> <approval id>"; it never reaches WhatsApp as text.
const canApprovalCommandMarker = "\x1eCAN_MUSE_DECISION\x1f"

const (
	canApprovalDecideMethod = "hitl.approval.decide"
	canApprovalMaxIDLength  = 256
)

type canPendingApproval struct {
	Choices  []canApprovalOption
	Answered bool
}

type canPendingApprovalSet struct {
	mutex sync.Mutex
	byID  map[string]*canPendingApproval
	order []string
}

var canPendingApprovals = &canPendingApprovalSet{byID: map[string]*canPendingApproval{}}

// record tracks the latest state of an approval: answerable only while pending.
func (s *canPendingApprovalSet) record(approval canHatchApproval) {
	s.mutex.Lock()
	defer s.mutex.Unlock()
	if !approval.Pending {
		delete(s.byID, approval.ID)
		return
	}
	if current, ok := s.byID[approval.ID]; ok {
		current.Choices = approval.Choices
		return
	}
	s.byID[approval.ID] = &canPendingApproval{Choices: approval.Choices}
	s.order = append(s.order, approval.ID)
	if len(s.order) > canApprovalMaxStates {
		delete(s.byID, s.order[0])
		s.order = s.order[1:]
	}
}

// claim reserves one answer for a pending approval option (1-based number).
func (s *canPendingApprovalSet) claim(id string, number int) (canApprovalOption, error) {
	s.mutex.Lock()
	defer s.mutex.Unlock()
	pending, ok := s.byID[id]
	if !ok {
		return canApprovalOption{}, fmt.Errorf("la aprobación ya no está pendiente")
	}
	if pending.Answered {
		return canApprovalOption{}, fmt.Errorf("la aprobación ya se respondió")
	}
	if number < 1 || number > len(pending.Choices) {
		return canApprovalOption{}, fmt.Errorf("la opción %d no existe en esta aprobación", number)
	}
	choice := pending.Choices[number-1]
	if !canApprovalDecisions[choice.Kind] {
		return canApprovalOption{}, fmt.Errorf("esa opción sólo puede elegirse en el teléfono")
	}
	pending.Answered = true
	return choice, nil
}

// release lets the user retry when sending failed before reaching WhatsApp.
func (s *canPendingApprovalSet) release(id string) {
	s.mutex.Lock()
	defer s.mutex.Unlock()
	if pending, ok := s.byID[id]; ok {
		pending.Answered = false
	}
}

// canParseApprovalCommand reads "<marker><number> <approval id>".
func canParseApprovalCommand(body string) (int, string, error) {
	number, id, found := strings.Cut(strings.TrimPrefix(body, canApprovalCommandMarker), " ")
	id = strings.TrimSpace(id)
	if !found || id == "" || len(id) > canApprovalMaxIDLength {
		return 0, "", fmt.Errorf("comando de aprobación no válido")
	}
	value, err := strconv.Atoi(number)
	if err != nil {
		return 0, "", fmt.Errorf("número de opción no válido")
	}
	return value, id, nil
}

// canApprovalDecisionData builds the RPC payload used by the official client.
func canApprovalDecisionData(approvalID string, choice canApprovalOption) ([]byte, error) {
	params := map[string]any{"approval_id": approvalID, "decision": choice.Kind}
	if choice.Kind == "allow_always" {
		if !canEnumLike.MatchString(choice.Scope) {
			return nil, fmt.Errorf("falta el alcance de «permitir siempre»")
		}
		params["always_scope"] = choice.Scope
	}
	return json.Marshal(map[string]any{
		"type":    "req",
		"version": 1,
		"payload": map[string]any{"method": canApprovalDecideMethod, "params": params},
	})
}

func canNewRequestID() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	b[6] = b[6]&0x0f | 0x40 // UUID v4, as used by the official client
	b[8] = b[8]&0x3f | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

// canApprovalDecisionMessage wraps the RPC like the official client does.
func canApprovalDecisionMessage(data []byte, now time.Time) *waE2E.Message {
	return &waE2E.Message{
		ProtocolMessage: &waE2E.ProtocolMessage{
			Type: waE2E.ProtocolMessage_AI_METADATA_OPERATION.Enum(),
			AiMetadataOperation: &waAICommon.AIMetadataOperation{
				HatchMetadataSync: &waAICommon.HatchMetadataSync{
					Data:        data,
					TimestampMS: proto.Int64(now.UnixMilli()),
					RequestID:   proto.String(canNewRequestID()),
				},
			},
		},
		MessageContextInfo: &waE2E.MessageContextInfo{BotMetadata: &waAICommon.BotMetadata{}},
	}
}

// canSendApprovalDecision answers one pending Muse approval.
func (s *Session) canSendApprovalDecision(ctx context.Context, client *whatsmeow.Client, chat types.JID, body string) error {
	if client == nil || chat != whatsmeow.CANMuseJID {
		return fmt.Errorf("las aprobaciones sólo se responden en el chat de Muse")
	}
	number, id, err := canParseApprovalCommand(body)
	if err != nil {
		return err
	}
	choice, err := canPendingApprovals.claim(id, number)
	if err != nil {
		return err
	}
	data, err := canApprovalDecisionData(id, choice)
	if err == nil {
		_, err = client.SendMessage(ctx, chat, canApprovalDecisionMessage(data, time.Now()), whatsmeow.SendRequestExtra{})
	}
	if err != nil {
		canPendingApprovals.release(id)
		client.Log.Warnf("Muse approval decision failed: %s", canShortError(err))
		return fmt.Errorf("no se pudo enviar la decisión: %w", err)
	}
	return nil
}
