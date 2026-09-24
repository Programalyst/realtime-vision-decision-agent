import cv2
from adbutils import adb
from ultralytics import YOLO
from ultralytics.engine.results import Results
from mysc.core.video import VideoAdapter, VideoKwargs
import time
import argparse
import os
from pathlib import Path
from datetime import datetime
from dataclasses import asdict
import json
from video_writer import H264Writer
from dotenv import load_dotenv
from deterministic_runtime import DeterministicRuntime
from timed_executor import TimedExecutor, AdbTransport
from frame_observer import FrameObserver
from choice_preprocessor import PlannerConfig, mouth_box
from jev_agent import JevAgent, DragController, detection_state

# --- 1. Initialization ---

TARGET_FPS: int = 30
DETECTION_CONFIDENCE: float = 0.25
parser = argparse.ArgumentParser(description="Live YOLO detection with timed deterministic control or optional Jev advice")
mode = parser.add_mutually_exclusive_group()
mode.add_argument("--deterministic", action="store_true", help="Use local motion planning and deterministic choices (no API)")
mode.add_argument("--jev", action="store_true", help="Send detections to Jev and display movement advice")
parser.add_argument("--control", action="store_true", help="Apply selected destinations as drags; requires --jev or --deterministic")
parser.add_argument("--jev-interval", type=float, default=0.5, help="Minimum seconds between API requests")
parser.add_argument("--drag-speed", type=float, default=3.0,
                    help="Planner estimated horizontal speed in screen widths/sec")
parser.add_argument("--input-delay", type=float, default=0.08,
                    help="Planner estimated input latency in seconds")
parser.add_argument("--jev-max-age", type=float, default=2.0, help="Outer response-age limit; remaining schedule is revalidated")
parser.add_argument("--jev-lead", type=float, default=0.65, help="Minimum lookahead reserved for API latency")
parser.add_argument("--feedback-delay", type=float, default=.12,
                    help="Deterministic assumed phone-to-decode delay in seconds; calibrate from logs")
parser.add_argument("--timing-uncertainty", type=float, default=.02,
                    help="Deterministic timing margin in seconds")
parser.add_argument("--plan-interval", type=float, default=.15,
                    help="Background deterministic route-search interval; hazards trigger immediately")
parser.add_argument("--input-transport", choices=("adb", "scrcpy"), default="adb",
                    help="Deterministic input transport; scrcpy held touch is experimental")
args = parser.parse_args()
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
if args.control and not (args.jev or args.deterministic):
    parser.error("--control requires --jev or --deterministic")
if args.jev and not (os.environ.get("JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY")):
    parser.error("Set JEV_API_KEY in .env (or TYPESAFE_API_KEY in the environment) to use --jev")
if not all(0 < value < float("inf") for value in (args.jev_interval, args.jev_max_age, args.jev_lead)):
    parser.error("--jev-interval, --jev-max-age and --jev-lead must be positive finite numbers")
try:
    planner_config = PlannerConfig(drag_speed=args.drag_speed, input_delay=args.input_delay)
    if not all(0 <= v < float('inf') for v in (args.feedback_delay, args.timing_uncertainty)):
        raise ValueError('Feedback delay and uncertainty must be finite nonnegative seconds')
    if not 0 < args.plan_interval < float('inf'):
        raise ValueError('Plan interval must be positive finite seconds')
except ValueError as error:
    parser.error(str(error))
box_color = (0, 255, 0)  # Green color for bounding boxes
box_thickness = 2
label_font_scale = 1
cv_font = cv2.FONT_HERSHEY_PLAIN

# Load the YOLO model
model = YOLO("./models/raindrops-yolo11n-v3.pt") 
print(f"Loaded classes: {model.names}")

# Find and connect to the Android device
device_list = adb.device_list()
if not device_list:
    raise RuntimeError("No ADB devices found. Ensure USB debugging is on.")
adb_device = adb.device_list()[0]

# Create a Scrcpy session
# max_size limits the resolution to improve performance.
video_config = VideoKwargs(
    video_codec=VideoKwargs.EnumVideoCodec.H264,  # or H265 / AV1 depending on device support
    max_size=1280,
    max_fps=TARGET_FPS
)

# Initialize and connect the Video Adapter
observer = FrameObserver()
video_stream = VideoAdapter(video_config, frame_update_callback=observer.publish)
video_stream.connect(adb_device)
timed_executor = None
if args.deterministic:
    transport = None
    if args.control:
        if args.input_transport == 'scrcpy':
            from scrcpy_drag import ScrcpyDragTransport
            transport = ScrcpyDragTransport(adb_device)
        else:
            transport = AdbTransport(adb_device)
    timed_executor = TimedExecutor(transport, input_delay=args.input_delay, threaded=args.control)
    jev = DeterministicRuntime(timed_executor, planner_config, decode_delay=args.feedback_delay,
                              timing_uncertainty=args.timing_uncertainty,
                              search_interval=args.plan_interval, control=args.control)
else:
    jev = JevAgent(interval=args.jev_interval, max_age=args.jev_max_age,
                   config=planner_config, lead=args.jev_lead) if args.jev else None
mode_name = "Rules" if args.deterministic else "Jev"
if jev:
    jev.set_enabled(False)
controller = DragController(adb_device) if args.control and args.jev else None
recording = None
recording_path = None
recording_started = None
last_recorded_frame = None
decision_log = None


def finish_recording():
    global recording, last_recorded_frame, decision_log
    if decision_log is not None:
        decision_log.close()
        decision_log = None
    if recording is not None:
        writer, recording = recording, None
        try:
            if last_recorded_frame is not None:
                writer.write(last_recorded_frame, time.monotonic() - recording_started)
        finally:
            writer.release()
        last_recorded_frame = None
        print(f"Saved {mode_name} session: {recording_path}")

print("Scrcpy stream connected successfully!")

print("Starting continuous inference... Press 'q' in the display window to quit.")
if jev:
    print(f"{mode_name} starts PAUSED. Navigate to the game, focus the preview, then press J to start/pause.")

# --- 2. Continuous Inference Loop ---

try:
    while True:
        start_time = time.time()
        
        decoded = observer.get(timeout=.1)
        if decoded is None:
            # The executor's lease independently stops pending input on a stall.
            key = cv2.waitKey(1) & 0xFF
            if key == ord('q'):
                break
            if jev and key in (ord('j'), ord('J')) and jev.enabled:
                jev.set_enabled(False)
                if controller:
                    controller.pause()
                finish_recording()
            continue
        captured_at = decoded.decoded_at  # decode time, NOT phone capture time
        frame = decoded.frame.to_ndarray(format='bgr24').copy()
        inference_started = time.monotonic()

        # Inspect raw detections without the tracker's additional score filtering.
        results: list[Results] = model.predict(
            frame, conf=DETECTION_CONFIDENCE, imgsz=640, verbose=False
        )
        inference_finished = time.monotonic()
        if jev:
            state = detection_state(results[0])
            movement_ready = controller.ready_for_target(state, captured_at) if controller else True
            if args.deterministic:
                jev_status = jev.update(state, decoded.sequence, captured_at)
                movement_ready = timed_executor.snapshot()['active'] is None
            else:
                jev_status = jev.update(state, captured_at, movement_ready=movement_ready)
            planning_finished = time.monotonic()
            if jev.choices is None:
                continue
            displayed_mouth = (mouth_box(jev.estimator.raw.can, planner_config)
                               if args.deterministic and jev.estimator.raw and jev.estimator.raw.can
                               else jev.choices.mouth_box)
            if displayed_mouth:
                mx1, my1, mx2, my2 = displayed_mouth
                fh, fw = frame.shape[:2]
                cv2.rectangle(frame, (round(mx1*fw), round(my1*fh)),
                              (round(mx2*fw), round(my2*fh)), (0, 0, 255), 2)
                mouth_center_x = (mx1+mx2)/2
                catch_half = (mx2-mx1)/2*planner_config.catch_center_fraction
                cv2.rectangle(frame, (round((mouth_center_x-catch_half)*fw), round(my1*fh)),
                              (round((mouth_center_x+catch_half)*fw), round(my2*fh)),
                              (0, 255, 255), 1)
            if jev.decision:
                target = jev.decision.target_x
                if args.deterministic and jev.choices.mouth_box:
                    target += (jev.choices.mouth_box[0]+jev.choices.mouth_box[2])/2-jev.choices.can['center'][0]
                target_x = round(target * frame.shape[1])
                cv2.line(frame, (target_x, 0), (target_x, frame.shape[0]-1), (255, 255, 0), 1)
            if decision_log is not None:
                executor_snapshot = timed_executor.snapshot() if timed_executor else None
                decision_log.write(json.dumps({
                        "session_time": time.monotonic()-recording_started,
                        "choices": asdict(jev.choices),
                        "selected": jev.decision.id if jev.decision else None,
                        "active_row": jev.active_track,
                        "movement_ready": movement_ready,
                        "jev_events": getattr(jev, "events", []),
                        "sequence": jev.snapshot if args.deterministic else jev.sequence.snapshot,
                        "timing": {"frame_sequence": decoded.sequence, "decoded_at": captured_at,
                                   "inference_started": inference_started, "inference_finished": inference_finished,
                                   "planning_finished": planning_finished},
                        "estimator": jev.estimator.snapshot if args.deterministic else None,
                        "executor": ({"version": executor_snapshot['version'],
                                      "active": asdict(executor_snapshot['active']) if executor_snapshot['active'] else None}
                                     if args.deterministic else None),
                        "motion": controller.motion_status if controller else None,
                        "previous_motion": controller.previous_motion if controller else None,
                }) + "\n")
            if controller and jev.enabled:
                control_state = {**state, "objects": [jev.choices.can] if jev.choices.can else []}
                controller.update_target(jev.decision, control_state, captured_at)
            cv2.putText(frame, jev_status, (20, 65), cv_font, label_font_scale, box_color, 2)
        for result in results:
            for box in result.boxes:
                # Get bounding box coordinates
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                
                # Get bounding box centers
                center_x, center_y = map(int, box.xywh[0, :2])
                
                class_id = int(box.cls.item())
                class_name = model.names[class_id]
                confidence = float(box.conf.item())
                label = f"{class_name} {confidence:.3f} x={center_x}, y={center_y}"

                # 1. Draw the bounding box rectangle on the BGR frame
                cv2.rectangle(frame, (x1, y1), (x2, y2), box_color, box_thickness)

                # 2. Calculate text size to create a background rectangle
                text_size, _ = cv2.getTextSize(label, cv_font, label_font_scale, 1)
                text_w, text_h = text_size

                # 3. Draw a filled rectangle as a background for the text
                cv2.rectangle(frame, (x1, y1 - text_h - 5), (x1 + text_w, y1), box_color, -1)

                # 4. Draw the custom text label
                cv2.putText(frame, label, (x1, y1 - 3), cv_font, label_font_scale, (0, 0, 0), 1)

        # Calculate FPS
        processing_time = time.time() - start_time
        fps = 1 / processing_time if processing_time > 0 else TARGET_FPS
        
        # Add the FPS text to the frame
        cv2.putText(frame, f"FPS: {fps:.2f}", (20, 40), cv_font, label_font_scale, box_color, 2)

        if recording is not None:
            elapsed = time.monotonic() - recording_started
            cv2.putText(frame, f"REC {elapsed:.1f}s", (20, 90), cv_font,
                        label_font_scale, (0, 0, 255), 2)
            recording.write(frame, elapsed)
            last_recorded_frame = frame.copy()

        # Display the resulting frame in an OpenCV window
        cv2.imshow("YOLO Live Inference", frame)

        # Break the loop if 'q' is pressed in the display window
        key = cv2.waitKey(1) & 0xFF
        if key == ord("q"):
            break
        if jev and key in (ord("j"), ord("J")):
            if not jev.enabled:
                recording_dir = Path("runs/deterministic" if args.deterministic else "runs/jev") / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                recording_dir.mkdir(parents=True, exist_ok=False)
                recording_path = recording_dir / "annotated.mp4"
                h, w = frame.shape[:2]
                recording = H264Writer(recording_path, TARGET_FPS, w, h, preset="ultrafast")
                recording_started = time.monotonic()
                decision_log = (recording_dir / "decisions.jsonl").open("w")
                last_recorded_frame = None
                print(f"Recording {mode_name} session: {recording_path}")
            jev.set_enabled(not jev.enabled)
            if controller and not jev.enabled:
                controller.pause()
            if not jev.enabled:
                finish_recording()
            print(f"{mode_name} RUNNING" if jev.enabled else f"{mode_name} PAUSED")

except KeyboardInterrupt:
    print("\nCtrl+C detected.")  # Catches keyboard interrupts if hit outside the inference function

finally:
    # --- Cleanup ---
    print("Closing resources.")
    if jev:
        jev.set_enabled(False)
        jev.close()
    if controller:
        controller.pause()
        controller.close()
    finish_recording()
    observer.close()
    cv2.destroyAllWindows()
    video_stream.disconnect()
