import argparse
import json
import os
import shutil
import subprocess
import tempfile
import wave

import numpy as np
from scipy.io import wavfile
from scipy.signal import correlate, correlation_lags

from stereo_alignment import analyze_stereo_alignment, print_alignment_analysis


FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"


def run(cmd, capture=False):
    print("\n>>>", " ".join(cmd))
    if capture:
        return subprocess.run(cmd, check=True, text=True, capture_output=True)
    return subprocess.run(cmd, check=True)


def ffprobe_json(path):
    result = run(
        [
            FFPROBE,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            path,
        ],
        capture=True,
    )
    return json.loads(result.stdout)


def parse_ffprobe_rate(rate_text):
    if not rate_text or rate_text in ("0/0", "N/A"):
        return None
    if "/" in rate_text:
        num, den = rate_text.split("/", 1)
        num = float(num)
        den = float(den)
        if den == 0:
            return None
        return num / den
    return float(rate_text)


def get_video_fps(path):
    info = ffprobe_json(path)
    video_streams = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    if not video_streams:
        raise RuntimeError(f"Could not find a video stream in: {path}")

    stream = video_streams[0]
    # Prefer avg_frame_rate for actual playback cadence, then fall back to r_frame_rate.
    fps = parse_ffprobe_rate(stream.get("avg_frame_rate"))
    if fps is None:
        fps = parse_ffprobe_rate(stream.get("r_frame_rate"))
    if fps is None:
        raise RuntimeError(f"Could not determine frame rate for: {path}")
    return fps


def get_duration_seconds(path):
    info = ffprobe_json(path)
    if "format" in info and "duration" in info["format"]:
        return float(info["format"]["duration"])
    raise RuntimeError(f"Could not determine duration for: {path}")


def extract_audio_segment(video_path, wav_path, start=0.0, duration=0.0, sample_rate=2000):
    cmd = [FFMPEG, "-y"]
    if start > 0:
        cmd += ["-ss", f"{start:.6f}"]
    cmd += ["-i", video_path]
    if duration and duration > 0:
        cmd += ["-t", f"{duration:.6f}"]
    cmd += [
        "-vn",
        "-ac", "1",
        "-ar", str(sample_rate),
        "-c:a", "pcm_s16le",
        wav_path,
    ]
    run(cmd)


# ----- Original StereoCombine-style start-sync functions -----
def load_wav_mono(path):
    sr, data = wavfile.read(path)

    original_dtype = data.dtype

    if data.ndim == 2:
        data = data.mean(axis=1)

    data = data.astype(np.float32)

    if np.issubdtype(original_dtype, np.integer):
        max_val = np.iinfo(original_dtype).max
        data /= max_val

    return sr, data


def make_envelope(audio, sr, env_rate=200):
    win = max(1, sr // env_rate)
    usable = (len(audio) // win) * win
    audio = audio[:usable]

    if usable == 0:
        raise ValueError("Audio too short to analyze.")

    env = np.mean(np.abs(audio.reshape(-1, win)), axis=1)

    env = np.diff(env, prepend=env[0])
    env -= np.mean(env)
    std = np.std(env)
    if std > 1e-9:
        env /= std

    return env, env_rate


def estimate_offset_seconds_original(left_wav, right_wav):
    sr_l, left_audio = load_wav_mono(left_wav)
    sr_r, right_audio = load_wav_mono(right_wav)

    if sr_l != sr_r:
        raise ValueError(f"Sample rates do not match: {sr_l} vs {sr_r}")

    left_env, env_rate = make_envelope(left_audio, sr_l)
    right_env, _ = make_envelope(right_audio, sr_r)

    corr = correlate(left_env, right_env, mode="full", method="fft")
    lags = correlation_lags(len(left_env), len(right_env), mode="full")
    best_lag = lags[np.argmax(corr)]

    # positive lag => LEFT started earlier, so trim LEFT
    # negative lag => RIGHT started earlier, so trim RIGHT
    offset_sec = best_lag / env_rate

    # confidence metric similar spirit to v2, but over the full-correlation array.
    top_index = int(np.argmax(corr))
    top = float(corr[top_index])
    if len(corr) > 1:
        corr_copy = np.array(corr, copy=True)
        corr_copy[top_index] = -np.inf
        second = float(np.max(corr_copy))
        confidence = 0.0 if abs(top) < 1e-9 else (top - second) / abs(top)
    else:
        confidence = 1.0

    return offset_sec, confidence


# ----- Smaller-window residual drift functions -----
def read_wav_mono_16bit(path):
    with wave.open(path, "rb") as wf:
        sr = wf.getframerate()
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        frames = wf.getnframes()
        raw = wf.readframes(frames)

    if width != 2:
        raise RuntimeError(f"Expected 16-bit PCM WAV, got sample width {width} bytes")

    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    data /= 32768.0
    return sr, data


def build_feature(audio, sr, feature_rate=200):
    if len(audio) < sr // 2:
        raise RuntimeError("Audio segment too short for reliable sync analysis.")

    audio = audio - np.mean(audio)
    peak = np.max(np.abs(audio))
    if peak > 1e-9:
        audio = audio / peak

    block = max(1, sr // feature_rate)
    usable = (len(audio) // block) * block
    audio = audio[:usable]
    if usable == 0:
        raise RuntimeError("Audio segment became empty during feature extraction.")

    shaped = audio.reshape(-1, block)
    env = np.mean(np.abs(shaped), axis=1)
    env = np.diff(env, prepend=env[0])
    env = env - np.mean(env)
    std = np.std(env)
    if std > 1e-9:
        env = env / std
    return env.astype(np.float32), feature_rate


def estimate_offset_seconds_limited(left_wav, right_wav, max_lag_seconds=2.0):
    sr_l, left_audio = read_wav_mono_16bit(left_wav)
    sr_r, right_audio = read_wav_mono_16bit(right_wav)
    if sr_l != sr_r:
        raise RuntimeError(f"Mismatched sample rates: {sr_l} vs {sr_r}")

    left_feat, feat_rate = build_feature(left_audio, sr_l)
    right_feat, _ = build_feature(right_audio, sr_r)

    corr = np.correlate(left_feat, right_feat, mode="full")
    lags = np.arange(-len(right_feat) + 1, len(left_feat))

    max_lag = int(round(max_lag_seconds * feat_rate))
    keep = np.abs(lags) <= max_lag
    corr = corr[keep]
    lags = lags[keep]

    idx = int(np.argmax(corr))
    best_lag = int(lags[idx])
    best_offset = best_lag / feat_rate

    top = float(corr[idx])
    if len(corr) > 1:
        corr_copy = np.array(corr, copy=True)
        corr_copy[idx] = -np.inf
        second = float(np.max(corr_copy))
        confidence = 0.0 if abs(top) < 1e-9 else (top - second) / abs(top)
    else:
        confidence = 1.0

    return best_offset, confidence


def estimate_offset_between_segments(left_video, right_video, left_start, right_start, duration, sample_rate=2000, max_lag_seconds=2.0, mode="limited"):
    with tempfile.TemporaryDirectory() as td:
        left_wav = os.path.join(td, "left.wav")
        right_wav = os.path.join(td, "right.wav")
        extract_audio_segment(left_video, left_wav, start=left_start, duration=duration, sample_rate=sample_rate)
        extract_audio_segment(right_video, right_wav, start=right_start, duration=duration, sample_rate=sample_rate)
        if mode == "original":
            return estimate_offset_seconds_original(left_wav, right_wav)
        return estimate_offset_seconds_limited(left_wav, right_wav, max_lag_seconds=max_lag_seconds)


def analyze_sync(left_video, right_video, start_analyze_seconds=300.0, drift_probe_window=30.0, end_margin=15.0, sample_rate=2000, max_lag=2.0):
    left_duration = get_duration_seconds(left_video)
    right_duration = get_duration_seconds(right_video)
    left_fps = get_video_fps(left_video)
    right_fps = get_video_fps(right_video)

    begin_window = min(start_analyze_seconds, left_duration, right_duration)
    if begin_window < 5:
        raise RuntimeError("Videos are too short for reliable analysis.")

    # IMPORTANT: this start lock deliberately reuses the original StereoCombine logic.
    start_offset, start_conf = estimate_offset_between_segments(
        left_video,
        right_video,
        left_start=0.0,
        right_start=0.0,
        duration=begin_window,
        sample_rate=sample_rate,
        mode="original",
    )

    left_trim = max(0.0, start_offset)
    right_trim = max(0.0, -start_offset)
    synced_duration_nominal = min(left_duration - left_trim, right_duration - right_trim)

    if synced_duration_nominal < 10:
        raise RuntimeError("Not enough overlapping material after initial sync.")

    end_window = min(drift_probe_window, max(5.0, synced_duration_nominal / 4.0))
    synced_probe_start = max(0.0, synced_duration_nominal - end_margin - end_window)

    left_end_start = left_trim + synced_probe_start
    right_end_start = right_trim + synced_probe_start

    end_residual, end_conf = estimate_offset_between_segments(
        left_video,
        right_video,
        left_start=left_end_start,
        right_start=right_end_start,
        duration=end_window,
        sample_rate=sample_rate,
        max_lag_seconds=max_lag,
        mode="limited",
    )

    end_global_offset = start_offset + end_residual
    drift_seconds = end_global_offset - start_offset

    if start_offset >= 0:
        left_p0 = start_offset
        right_p0 = 0.0
    else:
        left_p0 = 0.0
        right_p0 = -start_offset

    if end_global_offset >= 0:
        right_p1 = right_end_start
        left_p1 = right_p1 + end_global_offset
    else:
        left_p1 = left_end_start
        right_p1 = left_p1 - end_global_offset

    output_span = left_p1 - left_p0
    right_span = right_p1 - right_p0
    if output_span <= 0 or right_span <= 0:
        raise RuntimeError("Could not compute a valid drift model.")

    video_setpts_factor = output_span / right_span
    right_speed_factor = right_span / output_span

    return {
        "left_duration": left_duration,
        "right_duration": right_duration,
        "left_fps": left_fps,
        "right_fps": right_fps,
        "analysis_window_begin": begin_window,
        "analysis_window_end": end_window,
        "start_offset_seconds": start_offset,
        "start_confidence": start_conf,
        "end_residual_seconds": end_residual,
        "end_confidence": end_conf,
        "end_global_offset_seconds": end_global_offset,
        "drift_seconds": drift_seconds,
        "drift_ms": drift_seconds * 1000.0,
        "left_trim_seconds": left_trim,
        "right_trim_seconds": right_trim,
        "synced_duration_nominal": synced_duration_nominal,
        "left_point0": left_p0,
        "right_point0": right_p0,
        "left_point1": left_p1,
        "right_point1": right_p1,
        "video_setpts_factor_for_right": video_setpts_factor,
        "right_speed_factor": right_speed_factor,
        "needs_drift_correction": abs(drift_seconds) >= 0.100,
    }


def fmt_seconds(x):
    return f"{x:.6f}"


def apply_alignment_filters(vf_chain, shift_x_px=0.0, shift_y_px=0.0, rotate_deg=0.0):
    shift_x = int(round(float(shift_x_px)))
    shift_y = int(round(float(shift_y_px)))
    rotate_deg = float(rotate_deg)

    if abs(rotate_deg) > 1e-9:
        rotate_rad = rotate_deg * np.pi / 180.0
        vf_chain.append(f"rotate={rotate_rad:.12f}:ow=iw:oh=ih:c=black")

    if shift_x == 0 and shift_y == 0:
        return

    pad_x = abs(shift_x)
    pad_y = abs(shift_y)
    vf_chain.append(
        f"pad=iw+{2 * pad_x}:ih+{2 * pad_y}:{pad_x + shift_x}:{pad_y + shift_y}:black"
    )
    vf_chain.append(
        f"crop=iw-{2 * pad_x}:ih-{2 * pad_y}:{pad_x}:{pad_y}"
    )

def build_render_command(
    left_video,
    right_video,
    output,
    analysis,
    height=1080,
    crf=18,
    preset="medium",
    fps=None,
    use_right_audio=False,
    stereo_output="sbs",
    anaglyph_mode="arcd",
    alignment=None,
):
    left_trim = analysis["left_trim_seconds"]
    right_trim = analysis["right_trim_seconds"]
    setpts_factor = analysis["video_setpts_factor_for_right"] if analysis["needs_drift_correction"] else 1.0

    # Preserve the source cadence by default unless the user overrides it.
    if fps is None:
        fps = analysis.get("left_fps")

    vf_left = []
    af_left = []
    vf_right = []
    af_right = []

    if left_trim > 0:
        vf_left.append(f"trim=start={fmt_seconds(left_trim)}")
        af_left.append(f"atrim=start={fmt_seconds(left_trim)}")
    if right_trim > 0:
        vf_right.append(f"trim=start={fmt_seconds(right_trim)}")
        af_right.append(f"atrim=start={fmt_seconds(right_trim)}")

    vf_left.append("setpts=PTS-STARTPTS")
    af_left.append("asetpts=N/SR/TB")

    vf_right.append("setpts=PTS-STARTPTS")
    af_right.append("asetpts=N/SR/TB")

    if analysis["needs_drift_correction"]:
        vf_right.append(f"setpts={setpts_factor:.12f}*PTS")
        af_right.append(f"atempo={analysis['right_speed_factor']:.12f}")

    total_shift_x = float(alignment.get("shift_x_px", 0.0)) if alignment else 0.0
    total_shift_y = float(alignment.get("shift_y_px", 0.0)) if alignment else 0.0
    total_rotate_deg = float(alignment.get("rotate_deg", 0.0)) if alignment else 0.0

    left_shift_x = -0.5 * total_shift_x
    left_shift_y = -0.5 * total_shift_y
    right_shift_x = +0.5 * total_shift_x
    right_shift_y = +0.5 * total_shift_y

    left_rotate_deg = -0.5 * total_rotate_deg
    right_rotate_deg = +0.5 * total_rotate_deg

    apply_alignment_filters(
        vf_left,
        shift_x_px=left_shift_x,
        shift_y_px=left_shift_y,
        rotate_deg=left_rotate_deg,
    )
    apply_alignment_filters(
        vf_right,
        shift_x_px=right_shift_x,
        shift_y_px=right_shift_y,
        rotate_deg=right_rotate_deg,
    )

    vf_left += [f"scale=-2:{height}", "setsar=1"]
    vf_right += [f"scale=-2:{height}", "setsar=1"]
    
    if fps:
        vf_left.append(f"fps={fps}")
        vf_right.append(f"fps={fps}")

    filter_parts = [
        f"[0:v]{','.join(vf_left)}[leftv]",
        f"[0:a]{','.join(af_left)}[lefta]",
        f"[1:v]{','.join(vf_right)}[rightv]",
    ]

    if use_right_audio:
        filter_parts.append(f"[1:a]{','.join(af_right)}[righta]")
        filter_parts.append("[lefta][righta]amix=inputs=2:normalize=0[aout]")
        audio_map = "[aout]"
    else:
        audio_map = "[lefta]"

    if stereo_output == "anaglyph":
        filter_parts.append("[leftv][rightv]hstack=inputs=2:shortest=1[sbs]")
        filter_parts.append(f"[sbs]stereo3d=sbsl:{anaglyph_mode},format=yuv420p[vout]")
    elif stereo_output == "ou":
        filter_parts.append("[leftv][rightv]vstack=inputs=2:shortest=1[vout]")
    else:
        filter_parts.append("[leftv][rightv]hstack=inputs=2:shortest=1[vout]")

    filter_complex = ";".join(filter_parts)
    
    cmd = [
        FFMPEG,
        "-y",
        "-i", left_video,
        "-i", right_video,
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-map", audio_map,
    ]

    if fps:
        cmd += ["-r", f"{fps:.12f}", "-fps_mode", "cfr"]

    cmd += [
        "-c:v", "av1_nvenc", "-rc", "vbr", "-cq", "35", "-b:v", "0", "-rc-lookahead", "20", "-spatial-aq", "1", "-aq-strength", "8",      
        "-preset", preset,
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        "-shortest",
        output,
    ]

    return cmd


def apply_overrides(analysis, force_left_trim=None, force_right_trim=None, disable_drift=False, force_setpts_factor=None):
    a = dict(analysis)

    if force_left_trim is not None and force_right_trim is not None:
        raise RuntimeError("Use only one of force_left_trim or force_right_trim, not both.")

    if force_left_trim is not None:
        a["left_trim_seconds"] = max(0.0, float(force_left_trim))
        a["right_trim_seconds"] = 0.0
        a["start_offset_seconds"] = a["left_trim_seconds"]
        a["start_confidence"] = 1.0

    if force_right_trim is not None:
        a["left_trim_seconds"] = 0.0
        a["right_trim_seconds"] = max(0.0, float(force_right_trim))
        a["start_offset_seconds"] = -a["right_trim_seconds"]
        a["start_confidence"] = 1.0

    if disable_drift:
        a["needs_drift_correction"] = False
        a["video_setpts_factor_for_right"] = 1.0
        a["right_speed_factor"] = 1.0
        a["drift_seconds"] = 0.0
        a["drift_ms"] = 0.0
        a["end_residual_seconds"] = 0.0
        a["end_global_offset_seconds"] = a["start_offset_seconds"]

    if force_setpts_factor is not None:
        val = float(force_setpts_factor)
        if abs(val) < 1e-12:
            raise RuntimeError("force_setpts_factor must be non-zero.")
        a["video_setpts_factor_for_right"] = val
        a["right_speed_factor"] = 1.0 / val
        a["needs_drift_correction"] = abs(val - 1.0) >= 1e-9

    return a


def print_analysis(a):
    print("\n=== ANALYSIS ===")
    print(f"Left duration:              {a['left_duration']:.3f} s")
    print(f"Right duration:             {a['right_duration']:.3f} s")
    print(f"Start offset:               {a['start_offset_seconds']:+.6f} s")
    print(f"Start confidence:           {a['start_confidence']:.3f}")
    print(f"End residual after sync:    {a['end_residual_seconds']:+.6f} s")
    print(f"End confidence:             {a['end_confidence']:.3f}")
    print(f"End global offset:          {a['end_global_offset_seconds']:+.6f} s")
    print(f"Estimated drift by end:     {a['drift_seconds']:+.6f} s ({a['drift_ms']:+.1f} ms)")
    print(f"Trim from left start:       {a['left_trim_seconds']:.6f} s")
    print(f"Trim from right start:      {a['right_trim_seconds']:.6f} s")
    print(f"Nominal overlap duration:   {a['synced_duration_nominal']:.3f} s")
    print(f"Right setpts factor:        {a['video_setpts_factor_for_right']:.12f}")
    print(f"Right speed factor:         {a['right_speed_factor']:.12f}")
    print(f"Needs drift correction:     {a['needs_drift_correction']}")
    if a['start_offset_seconds'] > 0:
        print("Interpretation: LEFT started earlier, so LEFT is trimmed.")
    elif a['start_offset_seconds'] < 0:
        print("Interpretation: RIGHT started earlier, so RIGHT is trimmed.")
    else:
        print("Interpretation: starts appear already aligned.")


def print_manual_alignment(alignment):
    total_x = float(alignment["shift_x_px"])
    total_y = float(alignment["shift_y_px"])
    total_rot = float(alignment["rotate_deg"])

    print("\n=== STEREO ALIGNMENT TO APPLY ===")
    print(f"Total X correction:         {total_x:+.3f} px")
    print(f"Total Y correction:         {total_y:+.3f} px")
    print(f"Total roll correction:      {total_rot:+.3f} deg")
    print(f"LEFT X shift applied:       {-0.5 * total_x:+.3f} px")
    print(f"LEFT Y shift applied:       {-0.5 * total_y:+.3f} px")
    print(f"LEFT roll rotation:         {-0.5 * total_rot:+.3f} deg")
    print(f"RIGHT X shift applied:      {+0.5 * total_x:+.3f} px")
    print(f"RIGHT Y shift applied:      {+0.5 * total_y:+.3f} px")
    print(f"RIGHT roll rotation:        {+0.5 * total_rot:+.3f} deg")


def main():
    parser = argparse.ArgumentParser(description="Sync two camera videos by audio and render side-by-side.")
    parser.add_argument("left_video")
    parser.add_argument("right_video")
    parser.add_argument("--mode", choices=["analyze", "render"], default="analyze")
    parser.add_argument("--output", help="Required for render mode.")
    parser.add_argument("--start-analyze-seconds", type=float, default=300.0, help="Seconds from the beginning used for the ORIGINAL StereoCombine start lock.")
    parser.add_argument("--drift-probe-window", type=float, default=30.0, help="Seconds used for late drift analysis window.")
    parser.add_argument("--end-margin", type=float, default=15.0, help="How far before the nominal end to probe for drift.")
    parser.add_argument("--sample-rate", type=int, default=2000, help="Audio sample rate for analysis extraction.")
    parser.add_argument("--max-lag", type=float, default=2.0, help="Maximum late-window residual lag to search, in seconds.")
    parser.add_argument("--height", type=int, default=1080, help="Output height for each side before stacking.")
    parser.add_argument("--crf", type=int, default=18)
    parser.add_argument("--preset", default="slow")
    parser.add_argument("--fps", type=int, default=None, help="Optional constant frame rate for the output.")
    parser.add_argument("--use-right-audio", action="store_true", help="Mix in right audio too. Default is left audio only.")
    parser.add_argument("--force-left-trim", type=float, default=None, help="Manually force trimming this many seconds from the LEFT start.")
    parser.add_argument("--force-right-trim", type=float, default=None, help="Manually force trimming this many seconds from the RIGHT start.")
    parser.add_argument("--disable-drift-correction", action="store_true", help="Disable automatic drift correction and only apply start trim.")
    parser.add_argument("--force-setpts-factor", type=float, default=None, help="Manually force the right-video setpts factor (advanced).")
    parser.add_argument(
        "--stereo-output",
        choices=["sbs", "ou", "anaglyph"],
        default="sbs",
        help="Output stereo format. 'sbs' = side-by-side, 'ou' = over-under / top-bottom, 'anaglyph' = red/cyan style single-frame 3D."
    )
    parser.add_argument(
        "--anaglyph-mode",
        choices=["arcd", "arcc", "arch", "arcg"],
        default="arcd",
        help="Anaglyph mode when --stereo-output anaglyph is used. arcd is usually the best default."
    )
    parser.add_argument("--manual-right-shift-x", type=float, default=0.0, help="Manual X shift to apply to the RIGHT view, in source pixels. Positive moves the RIGHT image right.")
    parser.add_argument("--manual-right-shift-y", type=float, default=0.0, help="Manual Y shift to apply to the RIGHT view, in source pixels. Positive moves the RIGHT image down.")
    parser.add_argument("--manual-right-rotate-deg", type=float, default=0.0, help="Manual roll rotation to apply to the RIGHT view, in degrees.")
    parser.add_argument("--auto-align-vertical", action="store_true", help="Automatically estimate a vertical shift for the RIGHT view after sync.")
    parser.add_argument("--auto-align-horizontal", action="store_true", help="Also estimate a horizontal shift for the RIGHT view. This is less reliable than vertical auto-align.")
    parser.add_argument("--align-section-start", type=float, default=2.0, help="Synced-time start point, in seconds, for stereo alignment analysis.")
    parser.add_argument("--align-section-duration", type=float, default=6.0, help="Length of the synced section, in seconds, used for stereo alignment analysis.")
    parser.add_argument("--align-samples", type=int, default=5, help="How many synced frame pairs to sample for stereo alignment analysis.")
    parser.add_argument("--align-analysis-width", type=int, default=640, help="Downscaled width used internally for stereo alignment analysis.")
    parser.add_argument("--align-crop-fraction", type=float, default=0.70, help="Center-crop fraction used for stereo alignment analysis.")
    args = parser.parse_args()

    analysis = analyze_sync(
        args.left_video,
        args.right_video,
        start_analyze_seconds=args.start_analyze_seconds,
        drift_probe_window=args.drift_probe_window,
        end_margin=args.end_margin,
        sample_rate=args.sample_rate,
        max_lag=args.max_lag,
    )
    analysis = apply_overrides(
        analysis,
        force_left_trim=args.force_left_trim,
        force_right_trim=args.force_right_trim,
        disable_drift=args.disable_drift_correction,
        force_setpts_factor=args.force_setpts_factor,
    )
    print_analysis(analysis)

    alignment = {
        "shift_x_px": float(args.manual_right_shift_x),
        "shift_y_px": float(args.manual_right_shift_y),
        "rotate_deg": float(args.manual_right_rotate_deg),
    }

    if args.auto_align_vertical or args.auto_align_horizontal:
        auto_alignment = analyze_stereo_alignment(
            args.left_video,
            args.right_video,
            analysis,
            section_start=args.align_section_start,
            section_duration=args.align_section_duration,
            sample_count=args.align_samples,
            analysis_width=args.align_analysis_width,
            crop_fraction=args.align_crop_fraction,
            auto_horizontal=args.auto_align_horizontal,
        )
        print_alignment_analysis(auto_alignment)

        if args.auto_align_horizontal:
            alignment["shift_x_px"] += auto_alignment["suggested_right_shift_x_px"]
        if args.auto_align_vertical:
            alignment["shift_y_px"] += auto_alignment["suggested_right_shift_y_px"]

    print_manual_alignment(alignment)

    if args.mode == "render":
        if not args.output:
            raise SystemExit("--output is required in render mode.")
        cmd = build_render_command(
            args.left_video,
            args.right_video,
            args.output,
            analysis,
            height=args.height,
            crf=args.crf,
            preset=args.preset,
            fps=args.fps,
            use_right_audio=args.use_right_audio,
            stereo_output=args.stereo_output,
            anaglyph_mode=args.anaglyph_mode,
            alignment=alignment,
        )
        run(cmd)
        print(f"\nDone. Output written to: {args.output}")


if __name__ == "__main__":
    main()
