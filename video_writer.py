"""H.264 output shared by live sessions and recorded-video replay."""
from fractions import Fraction

import av
import cv2


class H264Writer:
    """Encode BGR frames as H.264 MP4 using the existing PyAV dependency."""

    def __init__(self, path, fps, width, height, preset="medium"):
        self.container = av.open(str(path), mode="w", options={"movflags": "+faststart"})
        try:
            self.stream = self.container.add_stream("libx264", rate=Fraction(str(fps)).limit_denominator(100000))
            # yuv420p requires even dimensions; pad rather than distort the image.
            self.stream.width = width + width % 2
            self.stream.height = height + height % 2
            self.stream.pix_fmt = "yuv420p"
            self.stream.time_base = Fraction(1, 1000)
            self.stream.codec_context.time_base = Fraction(1, 1000)
            self.stream.options = {"crf": "20", "preset": preset}
        except Exception:
            self.container.close()
            raise
        self.width, self.height = width, height
        self.last_pts = -1
        self.frame_index = 0
        self.fps = fps

    def write(self, frame, elapsed=None):
        if frame.shape[:2] != (self.height, self.width):
            raise ValueError("Video frame dimensions changed during recording")
        frame = cv2.copyMakeBorder(frame, 0, self.height % 2, 0, self.width % 2,
                                   cv2.BORDER_CONSTANT, value=(0, 0, 0))
        video_frame = av.VideoFrame.from_ndarray(frame, format="bgr24")
        if elapsed is None:
            elapsed = self.frame_index / self.fps
        self.frame_index += 1
        if elapsed is not None:
            video_frame.time_base = Fraction(1, 1000)
            video_frame.pts = max(self.last_pts + 1, round(elapsed * 1000))
            self.last_pts = video_frame.pts
        for packet in self.stream.encode(video_frame):
            self.container.mux(packet)

    def release(self):
        try:
            # Flush delayed frames before writing the MP4 trailer.
            for packet in self.stream.encode():
                self.container.mux(packet)
        finally:
            self.container.close()

