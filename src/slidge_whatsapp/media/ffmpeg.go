package media

import (
	// Standard library.
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os/exec"
)

var (
	// The full path and default arguments for FFmpeg, used for converting media to supported types.
	ffmpegCommand, _  = exec.LookPath("ffmpeg")
	ffmpegDefaultArgs = []string{"-v", "error", "-y"}

	// The full path and default arguments for FFprobe, as provided by FFmpeg, used for getting media
	// metadata (e.g. duration, waveforms, etc.)
	ffprobeCommand, _  = exec.LookPath("ffprobe")
	ffprobeDefaultArgs = []string{"-v", "error", "-of", "json=compact=1"}
)

// FFmpeg runs the `ffmpeg` command for the arguments provided, reading from the input file and
// writing to the output file paths given.
func ffmpeg(ctx context.Context, in, out string, args ...string) error {
	if ffmpegCommand == "" {
		return fmt.Errorf("FFmpeg command not found")
	}

	args = append(ffmpegDefaultArgs, append([]string{"-i", in}, append(args, out)...)...)
	cmd := exec.CommandContext(ctx, ffmpegCommand, args...)

	if _, err := cmd.Output(); err != nil {
		if e := new(exec.ExitError); errors.As(err, &e) {
			return fmt.Errorf("%s: %s", e.Error(), bytes.TrimSpace(e.Stderr))
		}
		return err
	}

	return nil
}

// FFprobe runs the `ffprobe` command for the arguments provided, reading from the input file given.
// Depending on arguments provided, the result may be a deeply nested set of maps with no specific
// structure; exploring the raw result of `ffprobe` commands with `-of json=compact=1` is recommended.
func ffprobe(ctx context.Context, in string, args ...string) (map[string]any, error) {
	if ffprobeCommand == "" {
		return nil, fmt.Errorf("FFprobe command not found")
	}

	args = append(ffprobeDefaultArgs, append([]string{"-i", in}, args...)...)
	cmd := exec.CommandContext(ctx, ffprobeCommand, args...)

	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, fmt.Errorf("failed to set up standard output: %w", err)
	} else if err = cmd.Start(); err != nil {
		return nil, fmt.Errorf("failed to initialize FFprobe: %w", err)
	}

	out := make(map[string]any)
	if err := json.NewDecoder(stdout).Decode(&out); err != nil {
		return nil, fmt.Errorf("failed reading FFprobe output: %w", err)
	}

	if err = cmd.Wait(); err != nil {
		return nil, fmt.Errorf("failed to wait for FFprobe command to complete: %w", err)
	}

	return out, nil
}
