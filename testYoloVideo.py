"""Replay recorded gameplay through YOLO; save annotated video and detections."""

import argparse
import csv
import math
from collections import Counter
from datetime import datetime
from fractions import Fraction
from pathlib import Path
import time

import av
import cv2
from ultralytics import YOLO


class H264Writer:
    """Encode BGR frames as H.264 MP4 using the existing PyAV dependency."""

    def __init__(self, path, fps, width, height):
        self.container = av.open(str(path), mode="w", options={"movflags": "+faststart"})
        try:
            self.stream = self.container.add_stream("libx264", rate=Fraction(str(fps)).limit_denominator(100000))
            # yuv420p requires even dimensions; pad rather than distort the image.
            self.stream.width = width + width % 2
            self.stream.height = height + height % 2
            self.stream.pix_fmt = "yuv420p"
            self.stream.options = {"crf": "20", "preset": "medium"}
        except Exception:
            self.container.close()
            raise
        self.width, self.height = width, height

    def write(self, frame):
        if frame.shape[:2] != (self.height, self.width):
            raise ValueError("Video frame dimensions changed during recording")
        frame = cv2.copyMakeBorder(frame, 0, self.height % 2, 0, self.width % 2,
                                   cv2.BORDER_CONSTANT, value=(0, 0, 0))
        for packet in self.stream.encode(av.VideoFrame.from_ndarray(frame, format="bgr24")):
            self.container.mux(packet)

    def release(self):
        try:
            # Flush delayed frames before writing the MP4 trailer.
            for packet in self.stream.encode():
                self.container.mux(packet)
        finally:
            self.container.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", nargs="?", type=Path, default=Path("recordings/gameplay-01.mp4"))
    parser.add_argument("--model", type=Path, default=Path("models/raindrops-yolo11n-v2.pt"))
    parser.add_argument("--conf", type=float, default=0.25)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--max-size", type=int, default=1280,
                        help="Resize longest side to match live capture; 0 keeps source size")
    parser.add_argument("--device", default=None, help="cpu, mps, or CUDA device index")
    parser.add_argument("--start", type=float, default=0, help="Start time in seconds")
    parser.add_argument("--max-frames", type=int, default=0, help="0 processes to end")
    parser.add_argument("--show", action="store_true", help="Preview; space pauses, q quits")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="New directory for annotated.mp4 and detections.csv")
    args = parser.parse_args()
    if not 0 < args.conf <= 1 or args.imgsz <= 0 or args.max_size < 0:
        parser.error("conf must be in (0, 1], imgsz positive, and max-size nonnegative")
    if not math.isfinite(args.start) or args.start < 0 or args.max_frames < 0:
        parser.error("start and max-frames must be nonnegative")
    for path in (args.video, args.model):
        if not path.is_file():
            parser.error(f"File not found: {path}")

    model = YOLO(str(args.model))
    print(f"Model: {args.model}; classes: {model.names}; confidence: {args.conf}")
    cap = cv2.VideoCapture(str(args.video))
    writer = None
    counts = Counter()
    frames_with_class = Counter()
    processed = 0
    output_dir = args.output_dir or Path("runs/video") / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    try:
        if not cap.isOpened():
            raise RuntimeError(f"Could not open {args.video}")
        fps = cap.get(cv2.CAP_PROP_FPS)
        if not math.isfinite(fps) or fps <= 0:
            raise RuntimeError("Video has no usable frame rate")
        if args.start and not cap.set(cv2.CAP_PROP_POS_MSEC, args.start * 1000):
            raise RuntimeError("Could not seek to requested start time")
        ok, frame = cap.read()
        if not ok:
            raise RuntimeError("No readable frame at the requested start time")
        output_dir.mkdir(parents=True, exist_ok=False)
        print(f"Output: {output_dir}")
        with (output_dir / "detections.csv").open("w", newline="") as handle:
            rows = csv.writer(handle)
            rows.writerow(["source_frame", "source_time_ms", "width", "height", "class",
                           "confidence", "x1", "y1", "x2", "y2", "center_x", "center_y"])
            while ok:
                started = time.perf_counter()
                source_frame = int(cap.get(cv2.CAP_PROP_POS_FRAMES)) - 1
                timestamp = cap.get(cv2.CAP_PROP_POS_MSEC)
                h, w = frame.shape[:2]
                if args.max_size and max(h, w) > args.max_size:
                    scale = args.max_size / max(h, w)
                    frame = cv2.resize(frame, (round(w * scale), round(h * scale)))
                h, w = frame.shape[:2]
                # Raw predictions: tracking would introduce additional filtering.
                result = model.predict(frame, conf=args.conf, imgsz=args.imgsz,
                                       device=args.device, verbose=False)[0]
                seen = set()
                for box in result.boxes:
                    name = model.names[int(box.cls.item())]
                    confidence = float(box.conf.item())
                    x1, y1, x2, y2 = box.xyxy[0].tolist()
                    rows.writerow([source_frame, timestamp, w, h, name, confidence,
                                   x1, y1, x2, y2, (x1 + x2) / 2, (y1 + y2) / 2])
                    counts[name] += 1
                    seen.add(name)
                frames_with_class.update(seen)
                annotated = result.plot()
                cv2.putText(annotated, f"Frame {source_frame} | conf >= {args.conf:g}",
                            (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
                if writer is None:
                    writer = H264Writer(output_dir / "annotated.mp4", fps, w, h)
                writer.write(annotated)
                processed += 1
                if args.show:
                    cv2.imshow("YOLO recording replay", annotated)
                    delay = max(1, round(1000 * (1 / fps - (time.perf_counter() - started))))
                    key = cv2.waitKey(delay) & 0xFF
                    if key == ord(" "):
                        key = cv2.waitKey(0) & 0xFF
                    if key in (ord("q"), 27):
                        break
                if processed % 100 == 0:
                    print(f"Processed {processed} frames", flush=True)
                if args.max_frames and processed >= args.max_frames:
                    break
                ok, frame = cap.read()
    finally:
        cap.release()
        if writer is not None:
            writer.release()
        if args.show:
            cv2.destroyAllWindows()
    print(f"Processed {processed} frames. Counts below are detections, not unique objects:")
    for name in model.names.values():
        print(f"  {name}: {counts[name]} detections in {frames_with_class[name]} frames")
    print(f"Saved {output_dir / 'annotated.mp4'} and {output_dir / 'detections.csv'}")


if __name__ == "__main__":
    main()
