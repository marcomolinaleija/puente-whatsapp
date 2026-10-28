package whatsapp

import (
	// Standard library.
	"context"
	"runtime"

	// Internal packages.
	"codeberg.org/slidge/slidge-whatsapp/slidge_whatsapp/media"

	// Third-party libraries.
	_ "github.com/mattn/go-sqlite3"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	walog "go.mau.fi/whatsmeow/util/log"
)

const (
	// Reliable callbacks retain the original capacity and apply backpressure rather than dropping.
	maxReliableGatewayCalls = 1024
	// Transient callbacks are best-effort and cannot consume reliable queue capacity.
	maxTransientGatewayCalls = 64
)

type gatewayCallDispatcher struct {
	reliableChan  chan func()
	transientChan chan func()
}

func newGatewayCallDispatcher() *gatewayCallDispatcher {
	return &gatewayCallDispatcher{
		reliableChan:  make(chan func(), maxReliableGatewayCalls),
		transientChan: make(chan func(), maxTransientGatewayCalls),
	}
}

func isTransientEvent(kind EventKind) bool {
	return kind == EventPresence || kind == EventChatState
}

func (d *gatewayCallDispatcher) enqueue(kind EventKind, callback func()) bool {
	if isTransientEvent(kind) {
		select {
		case d.transientChan <- callback:
			return true
		default:
			return false
		}
	}

	d.reliableChan <- callback
	return true
}

func (d *gatewayCallDispatcher) run(ctx context.Context) {
	// All callbacks still execute serially on one OS thread because they enter Python and the GIL.
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()

	var pendingTransient func()
	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		if pendingTransient == nil {
			select {
			case <-ctx.Done():
				return
			case callback := <-d.reliableChan:
				callback()
				continue
			case pendingTransient = <-d.transientChan:
			}
		}

		// A transient selected concurrently with a reliable callback remains pending while the
		// reliable FIFO drains. At most the transient already executing can delay new reliable work.
		select {
		case callback := <-d.reliableChan:
			callback()
		default:
			pendingTransient()
			pendingTransient = nil
		}
	}
}

// A LinkedDevice represents a unique pairing session between the gateway and WhatsApp. It is not
// unique to the underlying "main" device (or phone number), as multiple linked devices may be paired
// with any main device.
type LinkedDevice struct {
	// ID is an opaque string identifying this LinkedDevice to the Session. Noted that this string
	// is currently equivalent to a password, and needs to be protected accordingly.
	ID string
}

// JID returns the WhatsApp JID corresponding to the LinkedDevice ID. Empty or invalid device IDs
// may return invalid JIDs, and this function does not handle errors.
func (d LinkedDevice) JID() types.JID {
	jid, _ := types.ParseJID(d.ID)
	return jid
}

// IsAnonymousJID returns true if the JID given is not addressible, that is, if it's actually a LID.
func IsAnonymousJID(id string) bool {
	jid, _ := types.ParseJID(id)
	return jid.Server == types.HiddenUserServer
}

// A Gateway represents a persistent process for establishing individual sessions between linked
// devices and WhatsApp.
type Gateway struct {
	DBPath   string // The filesystem path for the client database.
	Name     string // The name to display when linking devices on WhatsApp.
	LogLevel string // The verbosity level to use when logging messages.
	TempDir  string // The directory to create temporary files under.

	// Internal variables.
	container      *sqlstore.Container
	callDispatcher *gatewayCallDispatcher
	logger         walog.Logger
}

// NewGateway returns a new, un-initialized Gateway. This function should always be followed by calls
// to [Gateway.Init], assuming a valid [Gateway.DBPath] is set.
func NewGateway() *Gateway {
	return &Gateway{}
}

// Init performs initialization procedures for the Gateway, and is expected to be run before any
// calls to [Gateway.Session].
func (w *Gateway) Init() error {
	w.logger = walog.Stdout("slidge", w.LogLevel, false)
	container, err := sqlstore.New(context.Background(), "sqlite3", w.DBPath, w.logger)
	if err != nil {
		return err
	}

	if w.Name != "" {
		store.SetOSInfo(w.Name, [...]uint32{1, 0, 0})
	}

	if w.TempDir != "" {
		if err := media.SetTempDirectory(w.TempDir); err != nil {
			return err
		}
	}

	w.callDispatcher = newGatewayCallDispatcher()
	w.container = container
	go w.callDispatcher.run(context.Background())

	return nil
}

// NewSession returns a new [Session] for the LinkedDevice given. If the linked device does not have
// a valid ID, a pair operation will be required, as described in [Session.Login].
func (w *Gateway) NewSession(device LinkedDevice) *Session {
	return &Session{device: device, gateway: w}
}

// CleanupSession will remove all invalid and obsolete references to the given device, and should be
// used when pairing a new device or unregistering from the Gateway.
func (w *Gateway) CleanupSession(device LinkedDevice) error {
	ctx := context.Background()
	devices, err := w.container.GetAllDevices(ctx)
	if err != nil {
		return err
	}

	for _, d := range devices {
		if d.ID == nil {
			w.logger.Infof("Removing invalid device %s from database", d.ID.String())
			_ = d.Delete(ctx)
		} else if device.ID != "" {
			if jid := device.JID(); d.ID.ToNonAD() == jid.ToNonAD() && *d.ID != jid {
				w.logger.Infof("Removing obsolete device %s from database", d.ID.String())
				_ = d.Delete(ctx)
			}
		}
	}

	return nil
}
