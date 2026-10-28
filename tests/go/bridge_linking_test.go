package whatsapp

import (
    "encoding/json"
    "testing"
    "time"
)

func TestLinkingQRPreservesNativeExpiry(t *testing.T) {
    for _, ttl := range []time.Duration{60*time.Second, 20*time.Second, 250*time.Millisecond} {
        var value struct { Code string; TTL float64 }
        if err := json.Unmarshal([]byte(canLinkQRPayload("fictional-qr", ttl)), &value); err != nil {
            t.Fatal(err)
        }
        if value.Code != "fictional-qr" || value.TTL != ttl.Seconds() {
            t.Fatalf("native QR expiry lost: %#v", value)
        }
    }
}
