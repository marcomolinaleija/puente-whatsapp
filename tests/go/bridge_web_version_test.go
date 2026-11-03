package whatsapp

import (
	"testing"

	"go.mau.fi/whatsmeow/store"
)

func TestCANWhatsAppWebBaseline(t *testing.T) {
	want := store.WAVersionContainer{2, 3000, 1052556849}
	if got := store.GetWAVersion(); got != want {
		t.Fatalf("compiled WhatsApp Web version = %s, want %s", got, want)
	}
	version := store.BaseClientPayload.GetUserAgent().GetAppVersion()
	if got := (store.WAVersionContainer{version.GetPrimary(), version.GetSecondary(), version.GetTertiary()}); got != want {
		t.Fatalf("login payload version = %s, want %s", got, want)
	}
	if !store.DeviceProps.GetHistorySyncConfig().GetSupportCallLogHistory() {
		t.Fatal("updating the Web version disabled call history support")
	}
}
