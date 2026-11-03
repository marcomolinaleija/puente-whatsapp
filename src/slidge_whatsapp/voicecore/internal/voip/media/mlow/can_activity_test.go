package mlow

import (
	"math"
	"testing"
)

// Synthetic speech-like tone, quiet floor, then the same tone: no recordings.
func canTalkspurts() [][]float32 {
	var frames [][]float32
	seed := uint32(0x12345678)
	for f := 0; f < 84; f++ {
		pcm := make([]float32, 960)
		for i := range pcm {
			if f < 18 || f >= 66 {
				t := float64(f*960+i) / 16000
				pcm[i] = float32(.25*math.Sin(2*math.Pi*150*t) + .05*math.Sin(2*math.Pi*1700*t))
			} else {
				seed = seed*1664525 + 1013904223
				pcm[i] = .001 * (float32(seed>>8)/8388608 - 1)
			}
		}
		frames = append(frames, pcm)
	}
	return frames
}

func TestCANActivityAndPostPauseContinuity(t *testing.T) {
	enc, dec := NewMlowEncoder(), NewMlowDecoder()
	counts := map[byte]int{}
	var before, after float64
	for f, pcm := range canTalkspurts() {
		wire, err := enc.Encode(pcm)
		if err != nil || len(wire) < 2 {
			t.Fatalf("frame %d: encoding failed: %v", f, err)
		}
		counts[wire[0]]++
		if wire[0] != 0x50 && wire[0] != 0x12 && wire[0] != 0x10 {
			t.Fatalf("invalid TOC %02x", wire[0])
		}
		decoded := dec.Decode(wire)
		if len(decoded) != 960 {
			t.Fatalf("frame %d: %d samples", f, len(decoded))
		}
		var energy float64
		for _, v := range decoded {
			if math.IsNaN(float64(v)) || math.IsInf(float64(v), 0) {
				t.Fatal("nonfinite decode")
			}
			energy += float64(v) * float64(v)
		}
		if f >= 6 && f < 16 {
			before += energy
		}
		if f >= 68 && f < 78 {
			after += energy
		}
		// As in reference PR #1616, allow VAD adaptation/hangover through frame 35.
		if f >= 36 && f < 66 && wire[0] != 0x10 {
			t.Fatalf("settled quiet floor labelled voice at %d", f)
		}
		if f >= 69 && wire[0] != 0x50 {
			t.Fatalf("resumed tone not speech at %d", f)
		}
	}
	for _, toc := range []byte{0x50, 0x12, 0x10} {
		if counts[toc] == 0 {
			t.Fatalf("missing activity class %02x: %v", toc, counts)
		}
	}
	if before == 0 || after/before < .25 || after/before > 4 {
		t.Fatalf("post-pause energy changed >6dB: %g -> %g", before, after)
	}
}

func TestCANDigitalSilenceIsNotSpeech(t *testing.T) {
	enc := NewMlowEncoder()
	for i := 0; i < 10; i++ {
		wire, err := enc.Encode(make([]float32, 960))
		if err != nil {
			t.Fatal(err)
		}
		if i >= 2 && wire[0] != 0x10 {
			t.Fatalf("digital silence labelled speech: %02x", wire[0])
		}
	}
}

func TestCANInactiveBodyAdvancesSynthesis(t *testing.T) {
	enc, dec := NewMlowEncoder(), NewMlowDecoder()
	frames := canTalkspurts()
	for i, pcm := range frames {
		wire, err := enc.Encode(pcm)
		if err != nil {
			t.Fatal(err)
		}
		out := dec.Decode(wire)
		if i == 50 {
			if wire[0] != 0x10 {
				t.Fatal("fixture did not reach inactive state")
			}
			var energy float64
			for _, v := range out {
				energy += float64(v) * float64(v)
			}
			if energy == 0 {
				t.Fatal("coded inactive body replaced with zeros")
			}
			return
		}
	}
	t.Fatal("missing inactive fixture")
}
