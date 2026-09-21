import cv2
from adbutils import adb
from ultralytics import YOLO
from ultralytics.engine.results import Results
from mysc.core.video import VideoAdapter, VideoKwargs
import threading 
from queue import Queue
import time
import argparse
import os
from pathlib import Path
from datetime import datetime
from dataclasses import asdict
import json
from video_writer import H264Writer
from dotenv import load_dotenv
from deterministic_controller import DeterministicController
from choice_preprocessor import PlannerConfig
from jev_agent import JevAgent, DragController, detection_state

# --- 1. Initialization ---

TARGET_FPS: int = 30
DETECTION_CONFIDENCE: float = 0.25
parser = argparse.ArgumentParser(description="Live YOLO detection with optional Jev advice")
mode = parser.add_mutually_exclusive_group()
mode.add_argument("--deterministic", action="store_true", help="Use local motion planning and deterministic choices (no API)")
mode.add_argument("--jev", action="store_true", help="Send detections to Jev and display movement advice")
parser.add_argument("--control", action="store_true", help="Apply Jev advice as short drags; requires --jev or --deterministic")
parser.add_argument("--jev-interval", type=float, default=0.5, help="Minimum seconds between API requests")
parser.add_argument("--drag-speed", type=float, default=3.0,
                    help="Deterministic estimated horizontal speed in screen widths/sec")
parser.add_argument("--input-delay", type=float, default=0.08,
                    help="Deterministic estimated input latency in seconds")
args = parser.parse_args()
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
if args.control and not (args.jev or args.deterministic):
    parser.error("--control requires --jev or --deterministic")
if args.jev and not (os.environ.get("JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY")):
    parser.error("Set JEV_API_KEY in .env (or TYPESAFE_API_KEY in the environment) to use --jev")
if not 0 < args.jev_interval < float("inf"):
    parser.error("--jev-interval must be a positive finite number")
try:
    planner_config = PlannerConfig(drag_speed=args.drag_speed, input_delay=args.input_delay)
except ValueError as error:
    parser.error(str(error))
box_color = (0, 255, 0)  # Green color for bounding boxes
box_thickness = 2
label_font_scale = 1
cv_font = cv2.FONT_HERSHEY_PLAIN
frame_queue = Queue(maxsize=5)  # Limit queue size to avoid lag accumulation

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
video_stream = VideoAdapter(video_config)
video_stream.connect(adb_device)
jev = (DeterministicController(planner_config) if args.deterministic else
       JevAgent(interval=args.jev_interval) if args.jev else None)
mode_name = "Rules" if args.deterministic else "Jev"
if jev:
    jev.set_enabled(False)
controller = DragController(adb_device) if args.control else None
stop_capture = threading.Event()
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

# --- Frame Capture Thread --- 
def frame_producer(video_stream: VideoAdapter, frame_queue: Queue): 
    while not stop_capture.is_set():
        # Grab screen frame as BGR NumPy array (suitable for OpenCV and YOLO without conversion)
        frame = video_stream.get_ndarray(frame_format='bgr24') 
        if frame is not None:
            # If the processing queue is backed up, drop old frames to keep it real-time
            if frame_queue.full():
                try:
                    frame_queue.get_nowait()
                except:
                    pass
            frame_queue.put((time.monotonic(), frame.copy()))
        stop_capture.wait(1 / TARGET_FPS)

# Create a thread-safe queue to hold frames 
frame_queue = Queue(maxsize=1) # maxsize=1 ensures we are always processing the latest frame 

# Create and start the producer thread 
producer_thread = threading.Thread(target=frame_producer, args=(video_stream, frame_queue)) 
producer_thread.daemon = True 
producer_thread.start() 

print("Starting continuous inference... Press 'q' in the display window to quit.")
if jev:
    print(f"{mode_name} starts PAUSED. Navigate to the game, focus the preview, then press J to start/pause.")

# --- 2. Continuous Inference Loop ---

try:
    while True:
        start_time = time.time()
        
        # Get a frame from the queue (blocks until a frame is available)
        captured_at, frame = frame_queue.get(block=True)

        # Inspect raw detections without the tracker's additional score filtering.
        results: list[Results] = model.predict(
            frame, conf=DETECTION_CONFIDENCE, imgsz=640, verbose=False
        )
        if jev:
            state = detection_state(results[0])
            if args.deterministic:
                movement_ready = controller.ready_for_target(state, captured_at) if controller else True
                jev_status = jev.update(state, captured_at, movement_ready=movement_ready)
            else:
                jev_status = jev.update(state, captured_at)
            if args.deterministic and jev.choices.mouth_box:
                mx1, my1, mx2, my2 = jev.choices.mouth_box
                fh, fw = frame.shape[:2]
                cv2.rectangle(frame, (round(mx1*fw), round(my1*fh)),
                              (round(mx2*fw), round(my2*fh)), (0, 0, 255), 2)
                mouth_center_x = (mx1+mx2)/2
                catch_half = (mx2-mx1)/2*planner_config.catch_center_fraction
                cv2.rectangle(frame, (round((mouth_center_x-catch_half)*fw), round(my1*fh)),
                              (round((mouth_center_x+catch_half)*fw), round(my2*fh)),
                              (0, 255, 255), 1)
            if args.deterministic and jev.decision:
                target_x = round(jev.decision.target_x * frame.shape[1])
                cv2.line(frame, (target_x, 0), (target_x, frame.shape[0]-1), (255, 255, 0), 1)
            if args.deterministic and decision_log is not None:
                decision_log.write(json.dumps({
                        "session_time": time.monotonic()-recording_started,
                        "choices": asdict(jev.choices),
                        "selected": jev.decision.id if jev.decision else None,
                        "active_row": jev.active_track,
                        "movement_ready": movement_ready,
                        "motion": controller.motion_status if controller else None,
                        "previous_motion": controller.previous_motion if controller else None,
                }) + "\n")
            if controller and jev.enabled:
                if args.deterministic:
                    control_state = {**state, "objects": [jev.choices.can] if jev.choices.can else []}
                    controller.update_target(jev.decision, control_state, captured_at)
                else:
                    controller.update(jev.decision, state, captured_at)
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
                if args.deterministic:
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
    finish_recording()
    stop_capture.set()
    producer_thread.join(timeout=1)
    cv2.destroyAllWindows()
    video_stream.disconnect()
    if jev:
        jev.close()
    if controller:
        controller.close()
