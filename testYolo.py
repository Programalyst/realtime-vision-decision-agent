import cv2
from adbutils import adb
from ultralytics import YOLO
from ultralytics.engine.results import Results
from mysc.core.video import VideoAdapter, VideoKwargs
import threading 
from queue import Queue
import time

# --- 1. Initialization ---

TARGET_FPS: int = 30
# Diagnostic threshold: this checkpoint assigns very low scores to bombs/droplets.
# Inspect the displayed scores before choosing a threshold for gameplay decisions.
DETECTION_CONFIDENCE: float = 0.25
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

print("Scrcpy stream connected successfully!")

# --- Frame Capture Thread --- 
def frame_producer(video_stream: VideoAdapter, frame_queue: Queue): 
    while True: 
        # Grab screen frame as BGR NumPy array (suitable for OpenCV and YOLO without conversion)
        frame = video_stream.get_ndarray(frame_format='bgr24') 
        if frame is not None:
            # If the processing queue is backed up, drop old frames to keep it real-time
            if frame_queue.full():
                try:
                    frame_queue.get_nowait()
                except:
                    pass
            frame_queue.put(frame)

# Create a thread-safe queue to hold frames 
frame_queue = Queue(maxsize=1) # maxsize=1 ensures we are always processing the latest frame 

# Create and start the producer thread 
producer_thread = threading.Thread(target=frame_producer, args=(video_stream, frame_queue)) 
producer_thread.daemon = True 
producer_thread.start() 

print("Starting continuous inference... Press 'q' in the display window to quit.")

# --- 2. Continuous Inference Loop ---

try:
    while True:
        start_time = time.time()
        
        # Get a frame from the queue (blocks until a frame is available)
        frame = frame_queue.get(block=True)

        # Inspect raw detections without the tracker's additional score filtering.
        results: list[Results] = model.predict(
            frame, conf=DETECTION_CONFIDENCE, imgsz=640, verbose=False
        )
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

        # Display the resulting frame in an OpenCV window
        cv2.imshow("YOLO Live Inference", frame)

        # Break the loop if 'q' is pressed in the display window
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

except KeyboardInterrupt:
    print("\nCtrl+C detected.")  # Catches keyboard interrupts if hit outside the inference function

finally:
    # --- Cleanup ---
    print("Closing resources.")
    cv2.destroyAllWindows()
    video_stream.disconnect()
