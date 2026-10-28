package whatsapp

import (
    "context"
    "errors"
    "runtime"
    "strings"
    "sync"
    "time"

    "go.mau.fi/whatsmeow"
    "go.mau.fi/whatsmeow/proto/waE2E"
    "go.mau.fi/whatsmeow/types/events"
)

// CAN inbound isolation v1. Each session owns three bounded, serial lanes.
// A callback remains synchronous: its Go payload stays owned until Python finishes.
// No unbounded Python tasks, goroutines per message, or shared callback bottleneck.
type canInboundDispatcher struct {
    ctx context.Context
    cancel context.CancelFunc
    control chan func()
    live chan func()
    history chan func()
    transient chan func()
    stop chan struct{}
    stopOnce sync.Once
    done sync.WaitGroup
}

func newCANInboundDispatcher() *canInboundDispatcher {
    ctx, cancel := context.WithCancel(context.Background())
    d := &canInboundDispatcher{ctx:ctx, cancel:cancel, control:make(chan func(),16),
        live:make(chan func(),128), history:make(chan func(),2), transient:make(chan func(),64),
        stop:make(chan struct{})}
    d.done.Add(3)
    go d.runControl()
    go d.run(d.live, d.transient)
    go d.run(d.history, nil)
    return d
}

func (d *canInboundDispatcher) runControl() {
    runtime.LockOSThread()
    defer runtime.UnlockOSThread()
    defer d.done.Done()
    for {
        select {
        case <-d.ctx.Done(): return
        case work:= <-d.control: work()
        case <-d.stop:
            for {
                if d.ctx.Err()!=nil { return }
                select {case work:= <-d.control:work(); default:d.cancel();return}
            }
        }
    }
}

func (d *canInboundDispatcher) run(queue, transient chan func()) {
    // gopy acquires the GIL on entry. Keep each caller on its own fixed OS thread.
    runtime.LockOSThread()
    defer runtime.UnlockOSThread()
    defer d.done.Done()
    for {
        if d.ctx.Err()!=nil { return }
        select {
        case work:= <-queue: work(); continue
        default:
        }
        select {
        case <-d.ctx.Done(): return
        case work:= <-queue: work()
        case work:= <-transient: work()
        }
    }
}

func (d *canInboundDispatcher) enqueue(queue chan func(), work func(), transient bool) bool {
    if d.ctx.Err()!=nil { return false }
    if transient {
        select {case queue<-work:return true; default:return false}
    }
    select {case queue<-work:return true; case <-d.ctx.Done():return false}
}

func (s *Session) canStartInbound() *canInboundDispatcher {
    s.canInboundMutex.Lock()
    defer s.canInboundMutex.Unlock()
    if s.canInbound != nil { s.canInbound.cancel() }
    s.canInbound = newCANInboundDispatcher()
    return s.canInbound
}

func (s *Session) canGetInbound() *canInboundDispatcher {
    s.canInboundMutex.Lock()
    defer s.canInboundMutex.Unlock()
    if s.canInbound == nil { s.canInbound = newCANInboundDispatcher() }
    return s.canInbound
}

func (s *Session) canStopInbound() {
    s.canInboundMutex.Lock()
    d:=s.canInbound
    s.canInboundMutex.Unlock()
    if d!=nil {
        // Disconnect/LoggedOut notifications already queued in the control lane
        // must run before stopping it. Never wait from the Python event loop.
        d.stopOnce.Do(func(){close(d.stop)})
    }
}

func canInboundControl(kind EventKind) bool {
    switch kind {case EventQRCode,EventPairDeviceID,EventConnect,EventLoggedOut:return true}
    return false
}

func (s *Session) canDeliverInbound(kind EventKind, payload *EventPayload) {
    if kind==EventUnknown || s.eventHandler==nil { return }
    if payload==nil { payload=&EventPayload{} }
    if shouldDropTransientEvent(kind,payload) { return }
    start:=time.Now()
    s.eventHandler(kind,payload)
    if elapsed:=time.Since(start); elapsed>=time.Second && s.gateway!=nil && s.gateway.logger!=nil {
        s.gateway.logger.Warnf("CAN_INBOUND slow_callback kind=%d seconds=%.3f",kind,elapsed.Seconds())
    }
}

func (s *Session) canQueuePayload(kind EventKind,payload *EventPayload) {
    if kind==EventUnknown || s.eventHandler==nil { return }
    if kind==EventCall && payload!=nil { s.canObserveCall(payload) }
    d:=s.canGetInbound()
    client:=s.currentClient()
    queue:=d.live
    if canInboundControl(kind) { queue=d.control
    } else if kind==EventMessage && payload!=nil && payload.Message.IsHistory { queue=d.history
    } else if isTransientEvent(kind) { queue=d.transient }
    d.enqueue(queue,func(){
        if !canInboundControl(kind) && client!=s.currentClient() { return }
        s.canDeliverInbound(kind,payload)
    },isTransientEvent(kind))
}

func (s *Session) canQueueMessage(ctx context.Context,client *whatsmeow.Client,evt *events.Message) {
    d:=s.canGetInbound()
    d.enqueue(d.live,func(){
        if ctx.Err()!=nil || client!=s.currentClient() { return }
        var kind EventKind
        var payload *EventPayload
        if evt.Message!=nil && evt.Message.GetCallLogMesssage()!=nil {
            kind,payload=newCallLogMessageEvent(ctx,client,evt)
        } else {
            kind,payload=newMessageEvent(ctx,client,evt)
        }
        if ctx.Err()==nil && client==s.currentClient() {
            if kind==EventCall && payload!=nil { s.canObserveCall(payload) }
            s.canDeliverInbound(kind,payload)
        }
    },false)
}

// An unavailable attachment is a recoverable message error, never a logout and
// never a reason to discard its identity, caption, reply context or timestamp.
func canUnavailableAttachment(message Message,raw *waE2E.Message) Message {
    caption:=""
    label:="Adjunto"
    switch {
    case raw.GetImageMessage()!=nil:caption=raw.GetImageMessage().GetCaption();label="Imagen"
    case raw.GetVideoMessage()!=nil:caption=raw.GetVideoMessage().GetCaption();label="Video"
    case raw.GetDocumentMessage()!=nil:caption=raw.GetDocumentMessage().GetCaption();label="Archivo"
    case raw.GetAudioMessage()!=nil:label="Audio"
    case raw.GetStickerMessage()!=nil:label="Sticker"
    }
    if message.Body=="" { message.Body=caption }
    message.Body=strings.TrimSpace(message.Body+"\n["+label+" no disponible: no se pudo descargar el adjunto]")
    message.Kind=MessagePlain
    return message
}

func canAttachmentFailureClass(err error) string {
    switch {
    case errors.Is(err,whatsmeow.ErrMediaDownloadFailedWith403):return "http403"
    case errors.Is(err,whatsmeow.ErrMediaDownloadFailedWith404):return "http404"
    case errors.Is(err,whatsmeow.ErrMediaDownloadFailedWith410):return "http410"
    case errors.Is(err,context.DeadlineExceeded):return "timeout"
    case errors.Is(err,context.Canceled):return "cancelled"
    default:return "download_failed"
    }
}
