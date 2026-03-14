# Stereo Sync Stack

Audio-sync, drift-correct, align, and merge two camera videos into a single stereoscopic output.

This project takes a **left-eye** video and a **right-eye** video, automatically synchronizes them by their audio, optionally checks and corrects for drift, optionally estimates stereo alignment offsets, and renders a final output such as:

- **Side-by-side (SBS)** stereo
- **Red/Cyan Anaglyph** stereo

It is designed for fast real-world stereo workflows where two separate cameras were started at slightly different times, may drift slightly over the duration of the clip, and may need small image alignment adjustments.

---

## Why this exists

If you record stereo video with two separate cameras, you usually run into some combination of the following problems:

- The cameras do **not start at the exact same time**
- One camera may run **slightly longer or shorter** than the other
- Small **clock drift** can accumulate over several minutes
- The views may have a slight **vertical misalignment**
- The views may have a small **horizontal offset**
- The right and left views may need a tiny **roll correction**
- You may want multiple output formats from the same source pair

This project solves those problems in a single Python + FFmpeg workflow.

---

## Core features

### 1. Audio-based start synchronization
The script analyzes the beginning of both videos and determines which camera started earlier.

It then trims the correct side so the videos begin in sync.

### 2. End-of-clip drift analysis
After the initial start sync is established, the script analyzes a later section of the clip to check whether the cameras drifted apart over time.

If needed, it computes a small speed correction for the **right** stream.

### 3. Stereo alignment controls
After the videos are time-synced, the script can also apply geometric alignment to the **right-eye** view:

- Manual **X shift**
- Manual **Y shift**
- Manual **roll rotation**
- Automatic **vertical alignment estimation**
- Optional automatic **horizontal alignment estimation**

### 4. GPU-accelerated encoding
The render path uses **NVIDIA NVENC** by default for fast encoding.

### 5. Multiple stereo output modes
Current output layouts supported by the main script:

- `sbs` — side-by-side stereo
- `anaglyph` — red/cyan-style single-frame 3D

---

## High-level workflow

The overall pipeline is:

1. Probe both input files with `ffprobe`
2. Determine durations and source FPS
3. Extract low-rate mono audio for analysis
4. Find the **start offset** using the original long-window envelope correlation method
5. Trim the earlier-starting stream
6. Probe a late section to estimate **drift by the end**
7. If drift is significant, compute a tiny speed correction for the **right** stream
8. Optionally analyze a synced section for stereo alignment offsets
9. Apply the requested or detected alignment to the **right** image
10. Render the final stereo output with FFmpeg

---

## Project files

Typical layout:

```text
Stereo Sync Stack/
├── stereo_sync_stack_v9.py
├── stereo_alignment.py
├── README.md
└── left.mp4 / right.mp4
```

### `stereo_sync_stack_v9.py`
Main CLI tool.

Responsibilities:
- input probing
- audio sync
- drift analysis
- option parsing
- final FFmpeg render command construction

### `stereo_alignment.py`
Alignment helper module.

Responsibilities:
- synced frame extraction
- grayscale preprocessing
- center crop handling
- phase-correlation-based translation estimation
- alignment analysis reporting

---

## Requirements

### Python
Recommended: **Python 3.10+**

### Python packages
Install the required Python dependencies:

```bash
pip install numpy scipy opencv-python
```

### FFmpeg
You must have both:

- `ffmpeg`
- `ffprobe`

installed and available on your system `PATH`.

To verify:

```bash
ffmpeg -version
ffprobe -version
```

### NVIDIA GPU (optional but recommended)
The default render path uses:

- `av1_nvenc`

So you need:

- a compatible NVIDIA GPU
- a recent driver
- an FFmpeg build with NVENC enabled

If AV1 NVENC is unavailable on your system, you can modify the script to use:

- `h264_nvenc`
- `hevc_nvenc`

instead.

---

## Installation

1. Clone or download the repository
2. Place both Python files in the same folder
3. Install the Python dependencies
4. Make sure FFmpeg is on `PATH`

Example:

```bash
git clone <your-repo-url>
cd <your-repo-folder>
pip install numpy scipy opencv-python
```

---

## Basic usage

The script has two modes:

- `analyze`
- `render`

### Important
The default mode is:

```text
analyze
```

That means if you do **not** specify `--mode render`, the script will print analysis results but **will not create an output file**.

---

## Analyze only

Use this to inspect sync and drift without writing a video file:

```bash
python stereo_sync_stack_v9.py left.mp4 right.mp4 --mode analyze
```

You will see output like:

- left duration
- right duration
- start offset
- start confidence
- end residual after sync
- drift estimate
- trim values
- setpts factor
- whether drift correction is needed

If stereo auto-alignment is enabled, you will also see:

- suggested right-eye X shift
- suggested right-eye Y shift
- response/confidence statistics

---

## Render a side-by-side stereo file

```bash
python stereo_sync_stack_v9.py left.mp4 right.mp4 --mode render --output output.mkv
```

This will:

- sync the videos by audio
- optionally correct drift if the detected drift exceeds the built-in threshold
- render a side-by-side output
- use the left audio by default

---

## Render an anaglyph stereo file

```bash
python stereo_sync_stack_v9.py left.mp4 right.mp4 --mode render --output anaglyph3d.mkv --stereo-output anaglyph
```

This converts the internally constructed side-by-side stereo pair into an anaglyph output.

Default anaglyph mode:

```text
arcd
```

which is typically the best general-purpose red/cyan choice.

---

## Quick examples

### Standard SBS output

```bash
python stereo_sync_stack_v9.py left.mp4 right.mp4 --mode render --output sbs_output.mkv
```

### Analyze only with auto vertical alignment

```bash
python stereo_sync_stack_v9.py left.mp4 right.mp4 --mode analyze --auto-align-vertical
```

### Render with auto vertical alignment

```bash
python stereo_sync_stack_v9.py left.mp4 right.mp4 --mode render --output aligned_output.mkv --auto-align-vertical
```

### Render with auto vertical and auto horizontal alignment

```bash
python stereo_sync_stack_v9.py left.mp4 right.mp4 --mode render --output aligned_output.mkv --auto-align-vertical --auto-align-horizontal
```

### Render with manual Y offset only

```bash
python stereo_sync_stack_v9.py left.mp4 right.mp4 --mode render --output output.mkv --manual-right-shift-y -3
```

### Render with manual Y offset and roll correction

```bash
python stereo_sync_stack_v9.py left.mp4 right.mp4 --mode render --output output.mkv --manual-right-shift-y -3 --manual-right-rotate-deg 0.15
```

### Render anaglyph with auto vertical alignment

```bash
python stereo_sync_stack_v9.py left.mp4 right.mp4 --mode render --output anaglyph3d.mkv --stereo-output anaglyph --auto-align-vertical
```

### Render using only start trim and no drift correction

```bash
python stereo_sync_stack_v9.py left.mp4 right.mp4 --mode render --output output.mkv --disable-drift-correction
```

### Force a known-good trim from the right

```bash
python stereo_sync_stack_v9.py left.mp4 right.mp4 --mode render --output output.mkv --force-right-trim 2.055
```

---

## How synchronization works

### Start sync
The script deliberately reuses the original long-window start-sync approach from the earlier working version of the project.

It:

1. extracts low-rate mono audio from the beginning of both videos
2. converts the signal to an envelope-style feature
3. cross-correlates the full envelopes
4. finds the best offset

This approach was kept because it produced more reliable start-lock behavior than a shorter-window limited-lag method.

### Drift analysis
After the start trim is determined, the script analyzes a late portion of the clip using a shorter-window residual check.

It compares the already-synced streams near the end and asks:

> after trimming the beginning, are the videos still aligned near the end?

If not, it computes a small correction factor for the **right** side.

### Drift correction threshold
At present, drift correction is automatically enabled when:

```text
abs(drift_seconds) >= 0.100
```

That means the script will only apply drift correction if the detected end drift is at least about **100 ms**.

This avoids unnecessary micro-corrections when the clip is already close enough.

---

## How stereo alignment works

The stereo alignment stage happens **after** time synchronization.

This is important because stereo image alignment is only meaningful when both frames represent the same moment in time.

### Current automatic alignment method
The alignment module:

1. picks a synced time section
2. extracts matching left/right frames from that section
3. center-crops the images
4. converts them to grayscale
5. downsizes them for analysis
6. runs **phase correlation** to estimate translation
7. aggregates the results across multiple samples

### What is estimated automatically
Currently supported automatic estimation:

- vertical translation of the right eye
- optional horizontal translation of the right eye

### What is manual only
Currently manual only:

- roll rotation (`--manual-right-rotate-deg`)

### Why vertical alignment matters most
For stereo comfort, **vertical disparity** is usually the most annoying mismatch.

A small vertical mismatch can be surprisingly easy to miss while watching the scene itself, because your eyes compensate for it.

However, it often becomes obvious when:

- you pause the video
- you look at player UI text
- you look at menus overlaid on top of the stereo image

This is why the auto vertical alignment option is especially useful.

### Why auto horizontal alignment is less reliable
Horizontal disparity in stereo footage is strongly tied to **depth**.

That means there is no single perfect horizontal shift that aligns everything in the scene equally well.

For this reason:

- vertical auto-align is generally trustworthy
- horizontal auto-align can still be useful
- but horizontal auto-align should be treated more cautiously

---

## Command-line options

Below is a detailed reference for the current script options.

### Positional arguments

#### `left_video`
Path to the left-eye input video.

#### `right_video`
Path to the right-eye input video.

---

### General mode selection

#### `--mode {analyze,render}`
Select whether to only print analysis or actually write an output file.

Default:

```text
analyze
```

#### `--output OUTPUT_PATH`
Required when `--mode render` is used.

---

### Sync analysis options

#### `--start-analyze-seconds FLOAT`
How much of the beginning of the video to analyze for the original start lock.

Default:

```text
300.0
```

This is intentionally long because the original method proved more reliable when it could examine a large audio window.

#### `--drift-probe-window FLOAT`
How much audio to analyze in the late-window drift check.

Default:

```text
30.0
```

#### `--end-margin FLOAT`
How far before the nominal end of the synced overlap to place the drift analysis window.

Default:

```text
15.0
```

#### `--sample-rate INT`
Audio sample rate used for analysis-only extraction.

Default:

```text
2000
```

Low analysis sample rates are intentional and help keep the sync calculations fast.

#### `--max-lag FLOAT`
Maximum lag allowed when estimating late-window residual drift.

Default:

```text
2.0
```

---

### Output sizing and cadence

#### `--height INT`
Height for **each eye view** before stacking.

Default:

```text
1080
```

For SBS, each side is scaled to this height before horizontal stacking.

#### `--fps INT`
Optional output frame rate override.

If not provided, the script preserves the source cadence using the left input FPS.

---

### Encoding options

#### `--preset PRESET`
Encoder preset.

Current default in `v9`:

```text
slow
```

#### `--crf INT`
Present as an argument in the script, but note that the current AV1 NVENC command path is primarily controlled by:

- `-rc vbr`
- `-cq 35`
- `-b:v 0`

So `--crf` is not the main quality knob in the current AV1 NVENC path.

#### `--use-right-audio`
Mix the right audio into the output instead of using only the left audio.

Default behavior without this flag:

- left audio only

---

### Manual sync overrides

#### `--force-left-trim FLOAT`
Force the script to trim this many seconds from the **left** start.

#### `--force-right-trim FLOAT`
Force the script to trim this many seconds from the **right** start.

Only use one of these two at a time.

#### `--disable-drift-correction`
Disables the automatic drift correction and applies only the start trim.

Useful when:

- the beginning is clearly correct
- end drift is negligible
- you want to avoid any speed adjustment

#### `--force-setpts-factor FLOAT`
Advanced manual override for the right-eye video timing factor.

This directly controls the `setpts` factor applied to the **right** video stream.

---

### Stereo output options

#### `--stereo-output {sbs,anaglyph}`
Choose the final stereo layout.

- `sbs` = side-by-side stereo
- `anaglyph` = red/cyan single-image stereo

Default:

```text
sbs
```

#### `--anaglyph-mode {arcd,arcc,arch,arcg}`
Anaglyph conversion mode.

Default:

```text
arcd
```

Available values:

- `arcd`
- `arcc`
- `arch`
- `arcg`

---

### Manual image alignment options

All of these act on the **right-eye** image only.

#### `--manual-right-shift-x FLOAT`
Manual horizontal shift in source pixels.

Positive values move the right image to the **right**.

#### `--manual-right-shift-y FLOAT`
Manual vertical shift in source pixels.

Positive values move the right image **down**.

#### `--manual-right-rotate-deg FLOAT`
Manual roll correction in degrees.

Positive and negative signs determine rotation direction according to FFmpeg’s `rotate` filter.

---

### Automatic alignment options

#### `--auto-align-vertical`
Enable automatic estimation of the right-eye vertical shift.

This is the most useful and most reliable automatic alignment option in the current version.

#### `--auto-align-horizontal`
Enable automatic estimation of the right-eye horizontal shift.

This can work well on some footage, but should be used more cautiously than vertical auto-align because stereo depth inherently creates horizontal disparity.

---

### Alignment analysis window options

These options control **where** the script looks for stereo alignment after the videos are already synced.

#### `--align-section-start FLOAT`
Synced-time position where stereo alignment analysis should begin.

Default:

```text
2.0
```

#### `--align-section-duration FLOAT`
How long the synced analysis section should be.

Default:

```text
6.0
```

#### `--align-samples INT`
How many matching frame pairs to sample across that section.

Default:

```text
5
```

#### `--align-analysis-width INT`
Width used internally for alignment analysis after downscaling.

Default:

```text
640
```

#### `--align-crop-fraction FLOAT`
Center-crop fraction used during alignment analysis.

Default:

```text
0.70
```

This focuses analysis toward the center of the frame and avoids edges that may be less stable or less relevant.

---

## Example workflows

### Workflow 1: Basic SBS render

Use this when you just want fast stereo merging with automatic sync:

```bash
python stereo_sync_stack_v9.py left.mp4 right.mp4 --mode render --output z.mkv
```

### Workflow 2: Check sync first, then render

Analyze:

```bash
python stereo_sync_stack_v9.py left.mp4 right.mp4 --mode analyze
```

Render:

```bash
python stereo_sync_stack_v9.py left.mp4 right.mp4 --mode render --output z.mkv
```

### Workflow 3: Add automatic vertical alignment

Analyze:

```bash
python stereo_sync_stack_v9.py left.mp4 right.mp4 --mode analyze --auto-align-vertical
```

Render:

```bash
python stereo_sync_stack_v9.py left.mp4 right.mp4 --mode render --output z.mkv --auto-align-vertical
```

### Workflow 4: Analyze a different section for alignment

If the default section is not representative of the clip:

```bash
python stereo_sync_stack_v9.py left.mp4 right.mp4 --mode analyze --auto-align-vertical --align-section-start 20 --align-section-duration 10 --align-samples 7
```

### Workflow 5: Auto-align plus manual fine-tuning

```bash
python stereo_sync_stack_v9.py left.mp4 right.mp4 --mode render --output z.mkv --auto-align-vertical --manual-right-shift-y -2 --manual-right-rotate-deg 0.10
```

### Workflow 6: Create an anaglyph output

```bash
python stereo_sync_stack_v9.py left.mp4 right.mp4 --mode render --output z.mkv --stereo-output anaglyph --auto-align-vertical
```

---

## Reading the analysis output

### `Start offset`
The detected time offset between the two streams at the beginning.

### `Start confidence`
A rough confidence-like measure for the start match.

Higher is better, but this is not a probability.

### `Trim from left start` / `Trim from right start`
How much of the beginning should be trimmed from the corresponding stream.

### `End residual after sync`
Residual misalignment measured in the late drift-check window.

### `Estimated drift by end`
How much the cameras have drifted apart by the end of the clip after the start trim is applied.

### `Right setpts factor`
The video timing factor applied to the right stream when drift correction is enabled.

### `Right speed factor`
The corresponding audio speed factor for the right stream when right audio is included.

### `Needs drift correction`
Whether the script decided to apply drift correction automatically.

### `=== STEREO ALIGNMENT ANALYSIS ===`
Printed only when auto stereo alignment is actually enabled.

This block includes:

- section start / end
- samples used
- median response
- suggested right X shift
- suggested right Y shift

### `=== STEREO ALIGNMENT TO APPLY ===`
Shows the final alignment values that will be used during rendering.

This may include:

- manual values only
- auto values only
- a combination of auto values and manual tweaks

---

## Quality and encoder notes

The current render path uses:

```text
-c:v av1_nvenc -rc vbr -cq 35 -b:v 0 -rc-lookahead 20 -spatial-aq 1 -aq-strength 8
```

This is a good balance of:

- speed
- quality
- compression efficiency

### If output files are too large
Try increasing the effective quality value:

- `-cq 38`
- `-cq 40`

inside the script.

### If AV1 NVENC fails
On some systems, you may need to switch the encoder to:

- `h264_nvenc`

or

- `hevc_nvenc`

A known issue encountered during development was anaglyph output producing `yuv444p`, which some AV1 NVENC setups reject. The current script solves this in the filter graph by forcing:

```text
format=yuv420p
```

after the `stereo3d` conversion.

---

## Troubleshooting

### Problem: no output file is created
Most common cause:

You forgot to use:

```bash
--mode render
```

The default mode is `analyze`, which prints results but does not write a video file.

---

### Problem: auto alignment prints zeros
If you see:

```text
=== STEREO ALIGNMENT TO APPLY ===
RIGHT X shift: +0.000 px
RIGHT Y shift: +0.000 px
RIGHT roll rotation: +0.000 deg
```

that does **not necessarily** mean the script detected perfect alignment.

It may simply mean you did **not** enable:

```bash
--auto-align-vertical
```

or

```bash
--auto-align-horizontal
```

When auto analysis is actually running, you should also see:

```text
=== STEREO ALIGNMENT ANALYSIS ===
```

---

### Problem: Windows / FFmpeg complains about PNG image sequence pattern
You may see an error like:

```text
The specified filename '...left_04.png' does not contain an image sequence pattern
```

That happens in the alignment helper during single-frame extraction.

Fix:
add this to the frame extraction command in `stereo_alignment.py`:

```text
-update 1
```

This tells FFmpeg that it is writing a single image, not an image sequence.

---

### Problem: auto vertical alignment misses a visible mismatch
Possible reasons:

- the chosen section was not good for analysis
- the section had motion blur or low detail
- the mismatch is mostly **roll**, not pure vertical shift
- the scene has complicated depth/parallax

Things to try:

- use a different `--align-section-start`
- increase `--align-section-duration`
- increase `--align-samples`
- test a more detailed section of the video
- add a small manual Y adjustment
- add a small manual roll correction

---

### Problem: the script finds the start sync correctly but the image still looks uncomfortable
That usually means the problem is not time sync anymore.

It is more likely one of:

- vertical image offset
- horizontal offset
- slight roll difference

Use the stereo alignment options rather than changing sync settings.

---

### Problem: output FPS seems wrong
The script is designed to preserve the left source FPS by default unless `--fps` is explicitly set.

Note: in the current `v9` CLI, `--fps` is parsed as an integer. So if you want to force FPS manually, use whole-number values such as:

```bash
python stereo_sync_stack_v9.py left.mp4 right.mp4 --mode render --output z.mkv --fps 30
```

If you need exact fractional rates like `29.97`, the parser should be changed from `type=int` to `type=float`.

---

## Current limitations

This is already a very useful real-world stereo merge tool, but it is not a complete stereo calibration suite.

Current limitations include:

- automatic rotation estimation is not implemented yet
- alignment is global, not scene-adaptive
- output formats are currently limited to `sbs` and `anaglyph`
- over-under output is not yet included in `v9`
- horizontal auto-alignment is inherently less trustworthy than vertical alignment
- no GUI is included yet
- no per-shot or per-segment alignment curves yet
- no full camera calibration / stereo rectification workflow yet

---

## Design philosophy

The project is intentionally practical.

The focus is:

- get the videos time-synced reliably
- preserve the start-lock behavior that actually works in practice
- keep drift correction optional and conservative
- provide simple but useful stereo alignment tools
- make the final render command easy to modify

This is not trying to be a full research-grade stereo reconstruction pipeline.

It is trying to be a **fast, useful, repeatable stereo video merging tool**.

---

## Recommended future improvements

Potential next steps:

- automatic roll estimation
- `ou` / over-under output mode
- half-SBS and half-OU output options
- analyze-only report export to JSON
- preview-image generation
- GUI wrapper
- per-segment alignment refinement
- stereo calibration / rectification support for fixed camera rigs

---

## Typical command cheat sheet

### Analyze only

```bash
python stereo_sync_stack_v9.py left.mp4 right.mp4 --mode analyze
```

### Render SBS

```bash
python stereo_sync_stack_v9.py left.mp4 right.mp4 --mode render --output z.mkv
```

### Render SBS with auto vertical align

```bash
python stereo_sync_stack_v9.py left.mp4 right.mp4 --mode render --output z.mkv --auto-align-vertical
```

### Render SBS with auto vertical + horizontal align

```bash
python stereo_sync_stack_v9.py left.mp4 right.mp4 --mode render --output z.mkv --auto-align-vertical --auto-align-horizontal
```

### Render anaglyph

```bash
python stereo_sync_stack_v9.py left.mp4 right.mp4 --mode render --output z.mkv --stereo-output anaglyph
```

### Render without drift correction

```bash
python stereo_sync_stack_v9.py left.mp4 right.mp4 --mode render --output z.mkv --disable-drift-correction
```

### Force trim from the right

```bash
python stereo_sync_stack_v9.py left.mp4 right.mp4 --mode render --output z.mkv --force-right-trim 2.055
```

### Manual Y correction

```bash
python stereo_sync_stack_v9.py left.mp4 right.mp4 --mode render --output z.mkv --manual-right-shift-y -3
```

### Manual Y + roll correction

```bash
python stereo_sync_stack_v9.py left.mp4 right.mp4 --mode render --output z.mkv --manual-right-shift-y -3 --manual-right-rotate-deg 0.12
```

---

## License / usage note

Add whatever license you want for your repository here, for example:

- MIT
- Apache-2.0
- GPL-3.0
- custom personal-use / commercial-use terms

---

## Final note

This project turned into a very capable stereo merge pipeline:

- audio sync
- drift correction
- stereo alignment
- SBS output
- anaglyph output
- GPU encoding

If you are doing stereo footage from two separate cameras, this can save a huge amount of manual work.
