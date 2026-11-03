// Package voicecore integrates voice with an existing Slidge-owned WhatsMeow client.
// It never connects, pairs, logs out or disconnects that shared client.
package voicecore

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"math"
	"sync"
	"time"
)

const PCMBytes = 640 // 20ms, 16kHz, mono, signed little-endian PCM16

type State struct {
	Type      string `json:"type"`
	Revision  uint64 `json:"revision"`
	CallID    string `json:"call_id"`
	Peer      string `json:"peer"`
	PeerPN    string `json:"peer_pn,omitempty"`
	Direction string `json:"direction"`
	State     string `json:"state"`
	Reason    string `json:"reason,omitempty"`
}

type Command struct {
	ID     string `json:"id"`
	Action string `json:"action"`
	CallID string `json:"call_id,omitempty"`
	Peer   string `json:"peer,omitempty"`
}

type Result struct {
	Type  string `json:"type"`
	ID    string `json:"id"`
	Error string `json:"error,omitempty"`
}

// All engine calls (except its own internal callbacks) run in one actor.
type engine interface {
	Command(context.Context, Command) error
	Signal(context.Context, any, State)
	PCM([]float32)
	Close(context.Context)
}

type request struct {
	command Command
	result  chan Result
}

type session struct {
	ctx         context.Context
	cancel      context.CancelFunc
	done        chan struct{}
	engine      engine
	commands    chan request
	signals     chan any
	capture     chan []byte
	playback    chan []byte
	changed     chan struct{}
	release     chan struct{}
	factory     func(*session) engine
	mu          sync.Mutex
	state       State
	owner       string
	device      string
	closing     bool
	epoch       uint64
	leaseCancel context.CancelFunc
	metrics     *nativeMetrics
}

func newSession(parent context.Context, device string, factory func(*session) engine) *session {
	ctx, cancel := context.WithCancel(parent)
	s := &session{ctx: ctx, cancel: cancel, done: make(chan struct{}),
		commands: make(chan request, 16), signals: make(chan any, 64),
		capture: make(chan []byte, 3), playback: make(chan []byte, 3),
		changed: make(chan struct{}, 1), device: device,
		release: make(chan struct{}, 1), factory: factory,
		state: State{Type: "state", State: "idle"}}
	s.engine = factory(s)
	go s.run()
	return s
}

func (s *session) run() {
	defer close(s.done)
	defer func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		s.engine.Close(ctx)
	}()
	for {
		if s.ctx.Err() != nil {
			return
		}
		select {
		case <-s.ctx.Done():
			return
		case <-s.release:
			s.mu.Lock()
			s.epoch++
			s.mu.Unlock()
			ctx, cancel := context.WithTimeout(s.ctx, 5*time.Second)
			s.engine.Close(ctx)
			cancel()
			s.engine = s.factory(s)
			for len(s.capture) > 0 {
				<-s.capture
			}
			for len(s.playback) > 0 {
				<-s.playback
			}
			for len(s.signals) > 0 {
				<-s.signals
			}
			for len(s.commands) > 0 {
				r := <-s.commands
				r.result <- Result{Type: "result", ID: r.command.ID, Error: "session-ended"}
			}
			s.update(State{State: "idle"})
			s.mu.Lock()
			s.owner = ""
			s.mu.Unlock()
		case r := <-s.commands:
			ctx, cancel := context.WithTimeout(s.ctx, 10*time.Second)
			err := s.execute(ctx, r.command)
			cancel()
			result := Result{Type: "result", ID: r.command.ID}
			if err != nil {
				result.Error = err.Error()
			}
			r.result <- result
		case signal := <-s.signals:
			s.mu.Lock()
			enabled := s.owner != "" && !s.closing
			s.mu.Unlock()
			if !enabled {
				continue
			}
			ctx, cancel := context.WithTimeout(s.ctx, 10*time.Second)
			s.engine.Signal(ctx, signal, s.snapshot())
			cancel()
		case data := <-s.capture:
			if s.snapshot().State != "active" {
				s.captureMetric("capture_inactive")
				continue
			}
			pcm := make([]float32, len(data)/2)
			for i := range pcm {
				pcm[i] = float32(int16(binary.LittleEndian.Uint16(data[2*i:]))) / 32768
			}
			s.engine.PCM(pcm)
		}
	}
}

func (s *session) execute(ctx context.Context, c Command) error {
	if len(c.ID) == 0 || len(c.ID) > 64 || len(c.Peer) > 256 || len(c.CallID) > 128 {
		return errors.New("bad-request")
	}
	state := s.snapshot()
	switch c.Action {
	case "dial":
		if c.CallID != "" || !validPeer(c.Peer) {
			return errors.New("bad-request")
		}
		if state.State != "idle" && state.State != "ended" {
			return errors.New("busy")
		}
	case "answer", "reject", "hangup":
		if c.Peer != "" || c.CallID == "" || c.CallID != state.CallID || state.State == "ended" {
			return errors.New("item-not-found")
		}
		if c.Action != "hangup" && state.State != "incoming_ringing" {
			return errors.New("invalid-state")
		}
	default:
		return errors.New("bad-request")
	}
	if err := s.engine.Command(ctx, c); err != nil {
		return errors.New("engine-failed")
	}
	return nil
}

func (s *session) snapshot() State {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.state
}

func (s *session) update(state State) {
	s.mu.Lock()
	state.Type = "state"
	state.Revision = s.state.Revision + 1
	s.state = state
	s.mu.Unlock()
	select {
	case s.changed <- struct{}{}:
	default:
	}
}

func (s *session) audio(pcm []float32) {
	if len(pcm) == 0 || len(pcm) > 1600 {
		return
	}
	data := make([]byte, len(pcm)*2)
	for i, v := range pcm {
		if math.IsNaN(float64(v)) || math.IsInf(float64(v), 0) {
			v = 0
		}
		v = max(-1, min(v, 32767.0/32768))
		binary.LittleEndian.PutUint16(data[i*2:], uint16(int16(v*32768)))
	}
	latest(s.playback, data)
}

// Audio is bounded by latency, not accumulated during a slow connection.
func latest(queue chan []byte, data []byte) bool {
	select {
	case queue <- data:
		return false
	default:
	}
	dropped := false
	select {
	case <-queue:
		dropped = true
	default:
	}
	select {
	case queue <- data:
	default:
		dropped = true
	}
	return dropped
}

func (s *session) signal(event any) {
	select {
	case <-s.ctx.Done():
		return
	default:
	}
	select {
	case s.signals <- event:
	default:
		s.stop()
	}
}

func (s *session) stop() {
	s.mu.Lock()
	s.closing = true
	s.mu.Unlock()
	s.cancel()
}

func (s *session) acquire(owner string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closing || s.ctx.Err() != nil || owner == "" || s.owner != "" {
		return false
	}
	s.owner = owner
	return true
}

func decodeCommand(data []byte) (Command, error) {
	var command Command
	if len(data) > 2048 {
		return command, errors.New("bad-request")
	}
	err := json.Unmarshal(data, &command)
	return command, err
}
