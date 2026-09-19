

# Key components:
1. Scrcpy
2. ADB -
3. MYScrcpy
4. Ultralytics YOLO
5. Roboflow - 
6. PyTorch - 
7. Typesafe AI Jev - 


# 1. Using Scrcpy for screen mirroring 

https://github.com/Genymobile/scrcpy

`brew install scrcpy`
`scrcpy`

Note this is only for screen mirror and manual control. Not script based control.


# 2. Setting up ADB

- install ADB
`brew install android-platform-tools`

- Confirm installation
`adb version`

- Check for connected phone
`adb devices`

# 3. Scripting
`python -m venv .venv`
`source .venv/bin/activate`
`deactivate`


Using MYScrcpy for scripting
https://github.com/me2sy/MYScrcpy/blob/main/README_EN.md

pip install mysc
pip install pyflac

# Recording

adb devices

```
scrcpy \
--no-audio \
--video-codec=h264 \
--video-bit-rate=16M \
--max-fps=30 \
--record=recordings/gameplay-01.mp4
```

# Replay recordings through YOLO

No phone connection is needed. Run the custom model on every decoded frame:

```bash
uv run python testYoloVideo.py recordings/gameplay-01.mp4 --show
```

The default model is `models/raindrops-yolo11n-v2.pt`, with confidence 0.25,
inference size 640, and capture resized to a maximum dimension of 1280 to
match `testYolo.py`. Use `--model` to select another checkpoint.

To investigate low-confidence droplets on the same section:

```bash
uv run python testYoloVideo.py recordings/gameplay-01.mp4 --start 10 --max-frames 150 --conf 0.05 --show
```

Space pauses/resumes the preview; Q or Escape stops. Omit `--show` to process
without a window. Each run creates a new folder under ignored `runs/video/`
containing an H.264-encoded `annotated.mp4` and a CSV of detections with confidence, source
frame/time, and box/center coordinates in the resized frame's pixel space.
The CSV includes frame dimensions; frames with no detections have no rows.
Console counts include repeated detections across frames, not unique objects.
Video encoding uses the existing PyAV dependency (`av`) with `libx264`, `yuv420p`,
and MP4 fast-start for playback compatibility. Odd frame dimensions are padded
by one black pixel on the right/bottom; CSV coordinates stay unchanged.
The output video has no audio and uses the source's average frame rate; scrcpy
recordings can have variable timing, so use the CSV source timestamps for timing
analysis. Processing never intentionally skips frames; a slow preview may run
slower than real time. Low-confidence detections are candidates to inspect, not
verified objects.
