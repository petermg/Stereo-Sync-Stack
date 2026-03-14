import math
import os
import shutil
import subprocess
import tempfile

import cv2
import numpy as np


FFMPEG = shutil.which("ffmpeg") or "ffmpeg"


def run(cmd, capture=False):
    print("\n>>>", " ".join(cmd))
    if capture:
        return subprocess.run(cmd, check=True, text=True, capture_output=True)
    return subprocess.run(cmd, check=True)


def extract_frame(video_path, time_sec, out_path):
    cmd = [
        FFMPEG,
        "-y",
        "-ss",
        f"{time_sec:.6f}",
        "-i",
        video_path,
        "-frames:v",
        "1",
        "-update",
        "1",
        "-an",
        out_path,
    ]    
    run(cmd)


def load_preprocessed_gray(image_path, analysis_width=640, crop_fraction=0.70):
    img = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise RuntimeError(f"Could not read frame image: {image_path}")

    src_h, src_w = img.shape[:2]
    if src_h < 16 or src_w < 16:
        raise RuntimeError("Extracted frame is too small for alignment analysis.")

    crop_fraction = float(crop_fraction)
    crop_fraction = max(0.10, min(1.0, crop_fraction))

    crop_w = max(16, int(round(src_w * crop_fraction)))
    crop_h = max(16, int(round(src_h * crop_fraction)))
    x0 = (src_w - crop_w) // 2
    y0 = (src_h - crop_h) // 2
    img = img[y0:y0 + crop_h, x0:x0 + crop_w]

    if analysis_width and img.shape[1] > analysis_width:
        scale = float(analysis_width) / float(img.shape[1])
        new_h = max(16, int(round(img.shape[0] * scale)))
        img = cv2.resize(img, (int(analysis_width), new_h), interpolation=cv2.INTER_AREA)
    else:
        scale = 1.0

    img = cv2.GaussianBlur(img, (5, 5), 0)
    img = img.astype(np.float32)
    img -= np.mean(img)
    std = np.std(img)
    if std > 1e-9:
        img /= std

    return img, {
        "source_width": src_w,
        "source_height": src_h,
        "cropped_width": crop_w,
        "cropped_height": crop_h,
        "analysis_width": int(img.shape[1]),
        "analysis_height": int(img.shape[0]),
        "crop_x": x0,
        "crop_y": y0,
        "downscale": scale,
    }


def estimate_translation_pixels(left_gray, right_gray):
    if left_gray.shape != right_gray.shape:
        raise RuntimeError(f"Shape mismatch for phase correlation: {left_gray.shape} vs {right_gray.shape}")

    h, w = left_gray.shape[:2]
    window = cv2.createHanningWindow((w, h), cv2.CV_32F)
    (dx, dy), response = cv2.phaseCorrelate(left_gray, right_gray, window)

    # phaseCorrelate(left, right) returns how far RIGHT is shifted relative to LEFT.
    # To align RIGHT back onto LEFT, apply the negative of that shift.
    return {
        "measured_right_relative_dx": float(dx),
        "measured_right_relative_dy": float(dy),
        "suggested_right_shift_x": float(-dx),
        "suggested_right_shift_y": float(-dy),
        "response": float(response),
    }


def robust_center(values):
    arr = np.array(values, dtype=np.float64)
    if arr.size == 0:
        return 0.0
    return float(np.median(arr))


def analyze_stereo_alignment(
    left_video,
    right_video,
    sync_analysis,
    section_start=2.0,
    section_duration=6.0,
    sample_count=5,
    analysis_width=640,
    crop_fraction=0.70,
    auto_horizontal=False,
    min_response=0.05,
):
    synced_duration = float(sync_analysis["synced_duration_nominal"])
    left_trim = float(sync_analysis["left_trim_seconds"])
    right_trim = float(sync_analysis["right_trim_seconds"])

    if synced_duration < 2.0:
        raise RuntimeError("Not enough synced duration for stereo alignment analysis.")

    section_start = max(0.0, float(section_start))
    section_duration = max(0.5, float(section_duration))
    sample_count = max(1, int(sample_count))

    latest_safe = max(0.0, synced_duration - 0.25)
    if section_start > latest_safe:
        section_start = max(0.0, latest_safe - min(section_duration, 1.0))

    section_end = min(section_start + section_duration, latest_safe)
    if section_end <= section_start:
        raise RuntimeError("Stereo alignment section is empty after bounds checking.")

    if sample_count == 1:
        times = [0.5 * (section_start + section_end)]
    else:
        times = np.linspace(section_start, section_end, sample_count).tolist()

    samples = []
    with tempfile.TemporaryDirectory() as td:
        for idx, synced_t in enumerate(times):
            left_t = left_trim + synced_t
            right_t = right_trim + synced_t
            left_png = os.path.join(td, f"left_{idx:02d}.png")
            right_png = os.path.join(td, f"right_{idx:02d}.png")

            extract_frame(left_video, left_t, left_png)
            extract_frame(right_video, right_t, right_png)

            left_gray, meta = load_preprocessed_gray(left_png, analysis_width=analysis_width, crop_fraction=crop_fraction)
            right_gray, _ = load_preprocessed_gray(right_png, analysis_width=analysis_width, crop_fraction=crop_fraction)
            shift = estimate_translation_pixels(left_gray, right_gray)

            source_per_analysis_x = meta["cropped_width"] / float(meta["analysis_width"])
            source_per_analysis_y = meta["cropped_height"] / float(meta["analysis_height"])

            sample = {
                "synced_time": float(synced_t),
                "left_time": float(left_t),
                "right_time": float(right_t),
                "response": float(shift["response"]),
                "suggested_right_shift_x_px_analysis": float(shift["suggested_right_shift_x"]),
                "suggested_right_shift_y_px_analysis": float(shift["suggested_right_shift_y"]),
                "suggested_right_shift_x_px_source": float(shift["suggested_right_shift_x"] * source_per_analysis_x),
                "suggested_right_shift_y_px_source": float(shift["suggested_right_shift_y"] * source_per_analysis_y),
            }
            samples.append(sample)

    good = [s for s in samples if s["response"] >= min_response]
    chosen = good if good else samples
    if not chosen:
        raise RuntimeError("Stereo alignment analysis produced no valid frame samples.")

    suggested_right_shift_y = robust_center([s["suggested_right_shift_y_px_source"] for s in chosen])
    if auto_horizontal:
        suggested_right_shift_x = robust_center([s["suggested_right_shift_x_px_source"] for s in chosen])
    else:
        suggested_right_shift_x = 0.0

    return {
        "section_start": section_start,
        "section_end": section_end,
        "sample_count_requested": sample_count,
        "sample_count_used": len(chosen),
        "analysis_width": int(analysis_width),
        "crop_fraction": float(crop_fraction),
        "auto_horizontal": bool(auto_horizontal),
        "min_response": float(min_response),
        "median_response": robust_center([s["response"] for s in chosen]),
        "suggested_right_shift_x_px": float(suggested_right_shift_x),
        "suggested_right_shift_y_px": float(suggested_right_shift_y),
        "samples": samples,
    }


def print_alignment_analysis(alignment_result):
    print("\n=== STEREO ALIGNMENT ANALYSIS ===")
    print(f"Section start (synced):     {alignment_result['section_start']:.3f} s")
    print(f"Section end (synced):       {alignment_result['section_end']:.3f} s")
    print(f"Samples used:               {alignment_result['sample_count_used']} / {alignment_result['sample_count_requested']}")
    print(f"Median response:            {alignment_result['median_response']:.3f}")
    print(f"Suggested RIGHT X shift:    {alignment_result['suggested_right_shift_x_px']:+.3f} px")
    print(f"Suggested RIGHT Y shift:    {alignment_result['suggested_right_shift_y_px']:+.3f} px")
    if not alignment_result["auto_horizontal"]:
        print("Horizontal auto-align:      disabled (X suggestion forced to 0)")


