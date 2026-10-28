package whatsapp

import (
    "context"
    "net/http"
    "net/http/httptest"
    "path/filepath"
    "strings"
    "sync"
    "testing"
    "time"

    "go.mau.fi/whatsmeow"
    "go.mau.fi/whatsmeow/proto/waE2E"
    "go.mau.fi/whatsmeow/proto/waCommon"
    "go.mau.fi/whatsmeow/proto/waWeb"
    "go.mau.fi/whatsmeow/store/sqlstore"
    "go.mau.fi/whatsmeow/types"
    "go.mau.fi/whatsmeow/types/events"
)

func waitCAN(t *testing.T, signal <-chan struct{}) {
    t.Helper()
    select {case <-signal:case <-time.After(3*time.Second):t.Fatal("inbound lane did not progress")}
}

func TestCANInboundIsolatesHistoryControlAndAccounts(t *testing.T) {
    if canInboundControl(EventCall) {t.Fatal("call metadata waits for ready; it cannot occupy the linking lane")}
    first,second:=newCANInboundDispatcher(),newCANInboundDispatcher()
    defer first.cancel();defer second.cancel()
    gate,started:=make(chan struct{}),make(chan struct{})
    first.enqueue(first.history,func(){close(started);<-gate},false)
    waitCAN(t,started)
    control,live,other:=make(chan struct{}),make(chan struct{}),make(chan struct{})
    first.enqueue(first.control,func(){close(control)},false)
    first.enqueue(first.live,func(){close(live)},false)
    second.enqueue(second.live,func(){close(other)},false)
    waitCAN(t,control);waitCAN(t,live);waitCAN(t,other)
    close(gate)
}

func TestCANInboundReliableFIFOAndBackpressure(t *testing.T) {
    d:=newCANInboundDispatcher();defer d.cancel()
    gate,started,complete:=make(chan struct{}),make(chan struct{}),make(chan struct{})
    d.enqueue(d.live,func(){close(started);<-gate},false);waitCAN(t,started)
    var mu sync.Mutex
    var got []int
    for i:=0;i<cap(d.live);i++ {n:=i;d.enqueue(d.live,func(){mu.Lock();got=append(got,n);mu.Unlock()},false)}
    accepted:=make(chan struct{})
    go func(){d.enqueue(d.live,func(){close(complete)},false);close(accepted)}()
    select {case <-accepted:t.Fatal("full reliable lane did not apply backpressure");case <-time.After(20*time.Millisecond):}
    close(gate);waitCAN(t,complete)
    mu.Lock();defer mu.Unlock()
    if len(got)!=128 {t.Fatalf("reliable messages lost: %d",len(got))}
    for i,n:=range got {if n!=i {t.Fatal("reliable FIFO reordered")}}
}

func TestCANInboundShutdownDoesNotWaitForBlockedLiveCallback(t *testing.T) {
    s:=&Session{}
    d:=s.canGetInbound()
    gate,started,control:=make(chan struct{}),make(chan struct{}),make(chan struct{})
    d.enqueue(d.live,func(){close(started);<-gate},false);waitCAN(t,started)
    d.enqueue(d.control,func(){close(control)},false)
    s.canStopInbound();waitCAN(t,control)
    waitCAN(t,d.ctx.Done())
    close(gate)
    done:=make(chan struct{});go func(){d.done.Wait();close(done)}();waitCAN(t,done)
    if d.enqueue(d.live,func(){t.Error("work after shutdown")},false) {t.Fatal("closed lane accepted work")}
}

func TestCANInboundUnavailableAttachmentPreservesEnvelope(t *testing.T) {
    original:=Message{ID:"fixture-id",Body:"",Timestamp:123,Chat:Chat{JID:"fixture@example.invalid"},ReplyID:"quoted",IsHistory:true}
    raw:=&waE2E.Message{ImageMessage:&waE2E.ImageMessage{Caption:ptrTo("Fixture caption")}}
    got:=canUnavailableAttachment(original,raw)
    if got.ID!=original.ID || got.Timestamp!=123 || got.Chat!=original.Chat || got.ReplyID!="quoted" || !got.IsHistory || got.Kind!=MessagePlain || !strings.Contains(got.Body,"Fixture caption") || !strings.Contains(got.Body,"no disponible") {t.Fatal("failed attachment lost message envelope")}
}

func TestCANInboundParsersPreserveHTTP403MessageAndContext(t *testing.T) {
    server:=httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter,r *http.Request){w.WriteHeader(403)}));defer server.Close()
    own:=types.NewJID("12025550100",types.DefaultUserServer)
    database,err:=sqlstore.New(context.Background(),"sqlite3","file:"+filepath.Join(t.TempDir(),"fixture.db")+"?_foreign_keys=on",nil)
    if err!=nil {t.Fatal(err)}
    defer database.Close()
    device:=database.NewDevice();device.ID=&own
    device.SetAllStores(sqlstore.NewSQLStore(database,own))
    device.LIDs=database.LIDMap
    client:=whatsmeow.NewClient(device,nil)
    client.SetMediaHTTPClient(server.Client())
    raw:=&waE2E.Message{ImageMessage:&waE2E.ImageMessage{URL:ptrTo(server.URL),Mimetype:ptrTo("image/jpeg"),Caption:ptrTo("Fixture caption"),ContextInfo:&waE2E.ContextInfo{StanzaID:ptrTo("quoted"),QuotedMessage:&waE2E.Message{Conversation:ptrTo("Fixture quote")}}}}
    pn,lid:=types.NewJID("12025550101",types.DefaultUserServer),types.NewJID("101",types.HiddenUserServer)
    info:=types.MessageInfo{MessageSource:types.MessageSource{Chat:pn,Sender:pn,SenderAlt:lid},ID:"fixture-live",Timestamp:time.Unix(123,0)}
    kind,payload:=newMessageEvent(context.Background(),client,&events.Message{Info:info,Message:raw})
    if kind!=EventMessage || payload==nil || payload.Message.ID!="fixture-live" || payload.Message.ReplyID!="quoted" || !strings.Contains(payload.Message.Body,"Fixture caption") || !strings.Contains(payload.Message.Body,"no disponible") {t.Fatal("live media failure discarded message/context")}
    group:=types.NewJID("fixture",types.GroupServer)
    historical:=&waWeb.WebMessageInfo{Key:&waCommon.MessageKey{RemoteJID:ptrTo(group.String()),Participant:ptrTo(pn.String()),ID:ptrTo("fixture-history")},Message:raw,MessageTimestamp:ptrTo(uint64(123))}
    kind,payload=newEventFromHistory(context.Background(),client,historical)
    if kind!=EventMessage || payload==nil || !payload.Message.IsHistory || payload.Message.ID!="fixture-history" || payload.Message.ReplyID!="quoted" || !strings.Contains(payload.Message.Body,"no disponible") {t.Fatal("history media failure discarded message/context")}
}
