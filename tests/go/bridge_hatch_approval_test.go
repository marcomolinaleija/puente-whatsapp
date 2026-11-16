package whatsapp

import (
	"context"
	"strings"
	"testing"
	"time"

	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/types"
)

func canFixtureApproval(lifecycle string) []byte {
	return []byte(`{"type":"event","version":1,"payload":{"event":"approval_record","seq":3,"ts_ms":1763287478000,` +
		`"payload":{"lifecycle":` + lifecycle + `,"record":{"approval_id":"fixture-approval-1",` +
		`"decision_options":[{"kind":"allow","label":{"kind":"allow","scope":"once"},"label_text":"Permitir una vez"},` +
		`{"kind":"deny","label":{"kind":"deny","scope":"once"},"label_text":"Denegar"}],` +
		`"display":{"attention_mode":"high","display_name":"Programa de prueba","icon_key":"terminal",` +
		`"permission_question":"¿Permitir que el agente ejecute este programa?",` +
		`"permission_caption":"Necesita acceso a la red",` +
		`"detail_rows":[{"label":"Ruta","value":"/usr/bin/fixture-tool"},{"label":"Destino","value":"example.invalid"},{"label":"Vacío","value":""}]}}}}}`)
}

func TestCANMuseApprovalIsShownReadOnlyAndUpdatedOnce(t *testing.T) {
	approval, ok := canParseHatchApproval(canFixtureApproval(`"pending"`))
	if !ok || approval.ID != "fixture-approval-1" || approval.Timestamp != 1763287478 {
		t.Fatalf("unexpected approval %+v", approval)
	}
	for _, part := range []string{
		"Solicitud de aprobación de Muse: Programa de prueba",
		"¿Permitir que el agente ejecute este programa?",
		"Necesita acceso a la red",
		"Detalles:\n- Ruta: /usr/bin/fixture-tool\n- Destino: example.invalid",
		"Opciones:\n1. Permitir una vez\n2. Denegar",
		"Estado: pendiente",
		"Confírmala o deniégala en el teléfono",
	} {
		if !strings.Contains(approval.Body, part) {
			t.Fatalf("approval lost %q:\n%s", part, approval.Body)
		}
	}
	if strings.Contains(approval.Body, "Vacío") {
		t.Fatalf("empty detail rows must be skipped")
	}

	chat := Chat{JID: "1807055946647697@bot"}
	first := Message{ID: "EVT1", Chat: chat}
	kind, payload, handled := canHatchApprovalEvent(&first, canFixtureApproval(`"pending"`))
	if !handled || kind != EventMessage || payload.Message.Kind != MessagePlain || payload.Message.ID != "can-muse-approval-fixture-approval-1" {
		t.Fatalf("new approval must be a plain message, got %+v", payload)
	}
	again := Message{ID: "EVT2", Chat: chat}
	if kind, payload, handled := canHatchApprovalEvent(&again, canFixtureApproval(`"pending"`)); !handled || kind != EventUnknown || payload != nil {
		t.Fatalf("unchanged repeats must be dropped")
	}
	resolved := Message{ID: "EVT3", Chat: chat}
	_, payload, _ = canHatchApprovalEvent(&resolved, canFixtureApproval(`{"state":"approved"}`))
	if payload == nil || payload.Message.Kind != MessageEdit || payload.Message.ReferenceID != "can-muse-approval-fixture-approval-1" ||
		!strings.Contains(payload.Message.Body, "Estado: aprobada") {
		t.Fatalf("a state change must correct the same message, got %+v", payload)
	}
}

func TestCANMuseApprovalShowsDecision(t *testing.T) {
	decided := []byte(strings.Replace(string(canFixtureApproval(`"resolved"`)),
		`"approval_id":"fixture-approval-1",`, `"approval_id":"fixture-approval-2","decision":"deny",`, 1))
	approval, ok := canParseHatchApproval(decided)
	if !ok || !strings.Contains(approval.Body, "Decidido: Denegar") || strings.Contains(approval.Body, "onfírmala") ||
		strings.Contains(approval.Body, "botones") {
		t.Fatalf("decided approval must show its decision only, got:\n%s", approval.Body)
	}
}

func canFixtureObservedApproval(id, lifecycle, decision string) []byte {
	decided := ""
	if decision != "" {
		decided = `"decision":"` + decision + `",`
	}
	return []byte(`{"type":"event","version":1,"payload":{"event":"hitl.approval_record","ts_ms":1763287478000,` +
		`"payload":{"lifecycle":"` + lifecycle + `","record":{"approval_id":"` + id + `",` + decided +
		`"decision_options":[` +
		`{"kind":"allow_once","label":{"kind":"allow_for_this_task"},"label_text":"Permitir esta vez"},` +
		`{"kind":"allow_always","label":{"kind":"allow_always_scoped","scope":"destination"},"label_text":"Permitir siempre"},` +
		`{"kind":"deny","label":{"kind":"deny"},"label_text":"Denegar"}],` +
		`"display":{"display_name":"Programa de prueba","permission_question":"¿Permitir?",` +
		`"detail_rows":[{"label":"Destino","value":"example.invalid"}]}}}}}`)
}

func TestCANMuseDecisionIsValidatedAndSentOnce(t *testing.T) {
	id := "fixture-approval-decide"
	approval, ok := canParseHatchApproval(canFixtureObservedApproval(id, "pending", ""))
	if !ok || !approval.Pending || len(approval.Choices) != 3 || strings.Contains(approval.Body, "Código") ||
		strings.Contains(approval.Body, id) ||
		!strings.Contains(approval.Body, "Opciones:\n1. Permitir esta vez\n2. Permitir siempre\n3. Denegar\nEstado: pendiente") {
		t.Fatalf("pending approval must be answerable without internal codes:\n%s", approval.Body)
	}
	message := Message{ID: "EVT", Chat: Chat{JID: "1807055946647697@bot"}}
	_, payload, _ := canHatchApprovalEvent(&message, canFixtureObservedApproval(id, "pending", ""))
	if payload.Message.ID != canApprovalMessageIDPrefix+id {
		t.Fatalf("the client answers by the message ID, got %q", payload.Message.ID)
	}

	for _, body := range []string{
		canApprovalCommandMarker + "2",
		canApprovalCommandMarker + "dos " + id,
		canApprovalCommandMarker + "2 ",
		canApprovalCommandMarker + "2 " + strings.Repeat("x", canApprovalMaxIDLength+1),
	} {
		if _, _, err := canParseApprovalCommand(body); err == nil {
			t.Fatalf("must reject %q", body)
		}
	}
	if number, gotID, err := canParseApprovalCommand(canApprovalCommandMarker + "2 " + id); err != nil || number != 2 || gotID != id {
		t.Fatalf("unexpected parse %d %q %v", number, gotID, err)
	}
	if _, err := canPendingApprovals.claim(id, 4); err == nil {
		t.Fatalf("out-of-range option must be rejected")
	}
	if _, err := canPendingApprovals.claim("fixture-unknown", 1); err == nil {
		t.Fatalf("unknown approval must be rejected")
	}
	choice, err := canPendingApprovals.claim(id, 2)
	if err != nil || choice.Kind != "allow_always" || choice.Scope != "destination" {
		t.Fatalf("unexpected claim %+v %v", choice, err)
	}
	if _, err := canPendingApprovals.claim(id, 3); err == nil {
		t.Fatalf("an approval must be answered only once")
	}
	canPendingApprovals.release(id)
	if _, err := canPendingApprovals.claim(id, 3); err != nil {
		t.Fatalf("a failed send must allow a retry: %v", err)
	}

	data, err := canApprovalDecisionData(id, choice)
	if err != nil || string(data) != `{"payload":{"method":"hitl.approval.decide","params":{"always_scope":"destination","approval_id":"fixture-approval-decide","decision":"allow_always"}},"type":"req","version":1}` {
		t.Fatalf("unexpected RPC %s %v", data, err)
	}
	deny, _ := canApprovalDecisionData(id, canApprovalOption{Kind: "deny"})
	if strings.Contains(string(deny), "always_scope") {
		t.Fatalf("only allow_always carries a scope: %s", deny)
	}
	if _, err := canApprovalDecisionData(id, canApprovalOption{Kind: "allow_always"}); err == nil {
		t.Fatalf("allow_always without scope must be refused")
	}
	msg := canApprovalDecisionMessage(data, time.UnixMilli(1763287478123))
	sync := msg.GetProtocolMessage().GetAiMetadataOperation().GetHatchMetadataSync()
	if msg.GetProtocolMessage().GetType() != waE2E.ProtocolMessage_AI_METADATA_OPERATION ||
		string(sync.GetData()) != string(data) || sync.GetTimestampMS() != 1763287478123 ||
		len(sync.GetRequestID()) != 36 || msg.GetMessageContextInfo().GetBotMetadata() == nil {
		t.Fatalf("unexpected envelope %v", msg)
	}
	if canNewRequestID() == canNewRequestID() {
		t.Fatalf("request IDs must be unique")
	}

	// Resolution removes the approval; a decided record is never answerable.
	canHatchApprovalEvent(&message, canFixtureObservedApproval(id, "resolved", "deny"))
	if _, err := canPendingApprovals.claim(id, 1); err == nil {
		t.Fatalf("resolved approvals must not be answerable")
	}
	s := &Session{}
	if err := s.canSendApprovalDecision(context.Background(), canMetaAITestClient(),
		types.NewJID("12025550100", types.DefaultUserServer), canApprovalCommandMarker+"1 "+id); err == nil {
		t.Fatalf("decisions outside the Muse chat must be refused")
	}

	// Unknown decision kinds are shown but never answerable from the bridge.
	odd := strings.Replace(string(canFixtureObservedApproval("fixture-odd", "pending", "")), `"kind":"deny"`, `"kind":"escalate"`, 1)
	if approval, _ := canParseHatchApproval([]byte(odd)); approval.Pending || strings.Contains(approval.Body, "botones") ||
		!strings.Contains(approval.Body, "Confírmala o deniégala en el teléfono.") {
		t.Fatalf("unknown options must keep the approval phone-only:\n%s", approval.Body)
	}
}

func TestCANMuseApprovalIgnoresOtherHatchEvents(t *testing.T) {
	for _, data := range []string{
		`{"type":"event","payload":{"event":"activity","payload":{"activity_code":"x","activity_text":"Pensando"}}}`,
		`{"type":"event","payload":{"payload":{"record":{"approval_id":"a","display":{}}}}}`,
		`{"type":"request","payload":{"method":"m","params":{"sections":[]}}}`,
		`not json`,
		``,
	} {
		if _, ok := canParseHatchApproval([]byte(data)); ok {
			t.Fatalf("must not publish %q", data)
		}
		message := Message{ID: "EVT", Chat: Chat{JID: "1807055946647697@bot"}}
		if _, _, handled := canHatchApprovalEvent(&message, []byte(data)); handled {
			t.Fatalf("other events must not be handled as approvals: %q", data)
		}
	}
	long := strings.Repeat("x", canApprovalMaxField+50)
	if got := canJSONText([]byte(`"` + long + `"`)); len(got) > canApprovalMaxField+len("…") {
		t.Fatalf("fields must be bounded, got %d bytes", len(got))
	}
	if canJSONText([]byte(`42`)) != "42" || canLifecycleText([]byte(`{"status":"denied"}`)) != "denied" {
		t.Fatalf("numbers and object lifecycles must be readable")
	}
}
