package whatsapp

// CAN Muse approvals: Muse ("hatch") is an agent that can run programs; before
// sensitive actions it asks for approval through AI_METADATA_OPERATION hatch
// sync events. The bridge renders each approval record as a readable message
// in the Muse chat (see can_hatch_decide.go for answering it). Logs never
// include approval values.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"sync"
)

const (
	canApprovalMaxRows   = 16
	canApprovalMaxOpts   = 8
	canApprovalMaxField  = 600
	canApprovalMaxBody   = 16 * 1024
	canApprovalMaxStates = 256
)

type canHatchApproval struct {
	ID        string
	Lifecycle string
	Body      string
	Timestamp int64
	Pending   bool                // answerable from the client
	Choices   []canApprovalOption // aligned with the numbered options in Body
}

// canApprovalOption is one decision the user can send, as offered by Muse.
type canApprovalOption struct {
	Kind  string // protocol decision, e.g. allow_once, allow_always, deny
	Scope string // label scope; required as always_scope for allow_always
	Text  string
}

// canApprovalDecisions are the decisions observed from the official client.
// Any other kind is shown but never answered from this bridge.
var canApprovalDecisions = map[string]bool{"allow_once": true, "allow_always": true, "deny": true}

func canApprovalAnswerable(choices []canApprovalOption) bool {
	for _, choice := range choices {
		if !canApprovalDecisions[choice.Kind] || (choice.Kind == "allow_always" && !canEnumLike.MatchString(choice.Scope)) {
			return false
		}
	}
	return true
}

type canHatchEnvelope struct {
	Payload struct {
		TsMs    json.RawMessage `json:"ts_ms"`
		Payload struct {
			Lifecycle json.RawMessage `json:"lifecycle"`
			Record    *struct {
				ApprovalID      json.RawMessage `json:"approval_id"`
				Decision        json.RawMessage `json:"decision"`
				DecisionOptions []struct {
					Kind      string `json:"kind"`
					LabelText string `json:"label_text"`
					Label     struct {
						Kind  string `json:"kind"`
						Scope string `json:"scope"`
					} `json:"label"`
				} `json:"decision_options"`
				Display *struct {
					DisplayName        json.RawMessage `json:"display_name"`
					PermissionQuestion json.RawMessage `json:"permission_question"`
					PermissionCaption  json.RawMessage `json:"permission_caption"`
					DetailRows         []struct {
						Label json.RawMessage `json:"label"`
						Value json.RawMessage `json:"value"`
					} `json:"detail_rows"`
				} `json:"display"`
			} `json:"record"`
		} `json:"payload"`
	} `json:"payload"`
}

// canJSONText returns a JSON string or number as bounded single-line text.
func canJSONText(raw json.RawMessage) string {
	raw = bytes.TrimSpace(raw)
	if len(raw) == 0 || bytes.Equal(raw, []byte("null")) {
		return ""
	}
	var text string
	if json.Unmarshal(raw, &text) != nil {
		var number json.Number
		if json.Unmarshal(raw, &number) != nil {
			return ""
		}
		text = number.String()
	}
	text = strings.Join(strings.Fields(text), " ")
	if len(text) > canApprovalMaxField {
		text = strings.TrimSpace(text[:canApprovalMaxField]) + "…"
	}
	return text
}

// canDecisionKind reads a decision given as a string or as {kind|decision|value}.
func canDecisionKind(raw json.RawMessage) string {
	if text := canJSONText(raw); text != "" {
		return text
	}
	var object map[string]json.RawMessage
	if json.Unmarshal(raw, &object) != nil {
		return ""
	}
	for _, key := range []string{"kind", "decision", "value", "type"} {
		if text := canJSONText(object[key]); text != "" {
			return text
		}
	}
	return ""
}

// canLifecycleText reads a lifecycle given as a string or as {state|status|phase}.
func canLifecycleText(raw json.RawMessage) string {
	if text := canJSONText(raw); text != "" {
		return text
	}
	var object map[string]json.RawMessage
	if json.Unmarshal(raw, &object) != nil {
		return ""
	}
	for _, key := range []string{"state", "status", "phase", "stage"} {
		if text := canJSONText(object[key]); text != "" {
			return text
		}
	}
	return ""
}

var canLifecycleLabels = map[string]string{
	"pending": "pendiente", "requested": "pendiente", "open": "pendiente",
	"approved": "aprobada", "allowed": "aprobada", "granted": "aprobada",
	"denied": "denegada", "rejected": "denegada", "declined": "denegada",
	"expired": "caducada", "timeout": "caducada", "cancelled": "cancelada", "canceled": "cancelada",
	"resolved": "resuelta", "completed": "resuelta",
}

func canLifecycleLabel(lifecycle string) string {
	if label := canLifecycleLabels[strings.ToLower(lifecycle)]; label != "" {
		return label
	}
	return lifecycle
}

// canParseHatchApproval renders an approval record; ok is false for other events.
func canParseHatchApproval(data []byte) (canHatchApproval, bool) {
	var envelope canHatchEnvelope
	if len(data) == 0 || json.Unmarshal(data, &envelope) != nil {
		return canHatchApproval{}, false
	}
	record := envelope.Payload.Payload.Record
	if record == nil || record.Display == nil {
		return canHatchApproval{}, false
	}
	id := canJSONText(record.ApprovalID)
	display := record.Display
	question := canJSONText(display.PermissionQuestion)
	if id == "" || (question == "" && len(display.DetailRows) == 0) {
		return canHatchApproval{}, false
	}
	lifecycle := canLifecycleText(envelope.Payload.Payload.Lifecycle)

	var b strings.Builder
	b.WriteString("Solicitud de aprobación de Muse")
	if name := canJSONText(display.DisplayName); name != "" {
		b.WriteString(": " + name)
	}
	if question != "" {
		b.WriteString("\n" + question)
	}
	if caption := canJSONText(display.PermissionCaption); caption != "" && caption != question {
		b.WriteString("\n" + caption)
	}
	rows := 0
	for _, row := range display.DetailRows {
		label, value := canJSONText(row.Label), canJSONText(row.Value)
		if value == "" || rows >= canApprovalMaxRows {
			continue
		}
		if rows == 0 {
			b.WriteString("\n\nDetalles:")
		}
		if label != "" {
			b.WriteString("\n- " + label + ": " + value)
		} else {
			b.WriteString("\n- " + value)
		}
		rows++
	}
	var options []string
	var choices []canApprovalOption
	decided := canDecisionKind(record.Decision)
	decidedLabel := ""
	for _, option := range record.DecisionOptions {
		text := strings.Join(strings.Fields(option.LabelText), " ")
		if text == "" {
			text = strings.TrimSpace(strings.Join([]string{option.Label.Kind, option.Label.Scope}, " "))
		}
		if text == "" {
			text = option.Kind
		}
		if decided != "" && option.Kind == decided && decidedLabel == "" {
			decidedLabel = text
		}
		if text != "" && len(options) < canApprovalMaxOpts {
			options = append(options, text)
			// Kept aligned with the numbered list: the number selects this option.
			choices = append(choices, canApprovalOption{Kind: option.Kind, Scope: option.Label.Scope, Text: text})
		}
	}
	if len(options) > 0 {
		b.WriteString("\n\nOpciones:")
		for i, option := range options {
			b.WriteString(fmt.Sprintf("\n%d. %s", i+1, option))
		}
	}
	if decided != "" {
		if decidedLabel == "" {
			decidedLabel = decided
		}
		b.WriteString("\nDecidido: " + decidedLabel)
	}
	if lifecycle != "" {
		b.WriteString("\nEstado: " + canLifecycleLabel(lifecycle))
	}
	pending := decided == "" && (lifecycle == "" || canLifecycleLabel(lifecycle) == "pendiente")
	actionable := pending && len(choices) > 0 && canApprovalAnswerable(choices)
	if actionable {
		b.WriteString("\n\nPuedes responder con los botones de este mensaje (Tab desde el historial) o en el teléfono.")
	} else if decided == "" {
		b.WriteString("\n\nConfírmala o deniégala en el teléfono.")
	}
	body := b.String()
	if len(body) > canApprovalMaxBody {
		body = body[:canApprovalMaxBody] + "…"
	}
	var timestamp int64
	if ms, err := strconv.ParseInt(canJSONText(envelope.Payload.TsMs), 10, 64); err == nil && ms > 0 {
		timestamp = ms / 1000
	}
	return canHatchApproval{
		ID: id, Lifecycle: lifecycle, Body: body, Timestamp: timestamp,
		Pending: actionable, Choices: choices,
	}, true
}

// canApprovalStates remembers the last published state of each approval.
type canApprovalStateSet struct {
	mutex  sync.Mutex
	states map[string]string
	order  []string
}

var canApprovalStates = &canApprovalStateSet{states: map[string]string{}}

// update returns (seen, changed) and records the state.
func (s *canApprovalStateSet) update(id, body string) (bool, bool) {
	s.mutex.Lock()
	defer s.mutex.Unlock()
	previous, seen := s.states[id]
	if seen && previous == body {
		return true, false
	}
	if !seen {
		s.order = append(s.order, id)
		if len(s.order) > canApprovalMaxStates {
			delete(s.states, s.order[0])
			s.order = s.order[1:]
		}
	}
	s.states[id] = body
	return seen, true
}

// canEnumLike matches protocol identifiers (decision kinds and scopes).
var canEnumLike = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,63}$`)

// canApprovalMessageIDPrefix prefixes the XMPP message ID of an approval; the
// client answers by this ID, so no internal code is shown to the user.
const canApprovalMessageIDPrefix = "can-muse-approval-"

func canApprovalMessageID(id string) string {
	return canApprovalMessageIDPrefix + id
}

// canHatchApprovalEvent turns an approval record into a message (new) or a
// correction (state changed). Unchanged repeats are dropped.
func canHatchApprovalEvent(message *Message, data []byte) (EventKind, *EventPayload, bool) {
	approval, ok := canParseHatchApproval(data)
	if !ok {
		return EventUnknown, nil, false
	}
	canPendingApprovals.record(approval)
	seen, changed := canApprovalStates.update(approval.ID, approval.Body)
	if !changed {
		return EventUnknown, nil, true
	}
	id := canApprovalMessageID(approval.ID)
	message.Body = approval.Body
	if approval.Timestamp > 0 && !seen {
		message.Timestamp = approval.Timestamp
	}
	if seen {
		message.Kind = MessageEdit
		message.ReferenceID = id
	} else {
		message.Kind = MessagePlain
		message.ID = id
	}
	return EventMessage, &EventPayload{Message: *message}, true
}
