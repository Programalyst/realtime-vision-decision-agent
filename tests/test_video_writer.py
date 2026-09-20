import tempfile
import unittest
from pathlib import Path
import av
import numpy as np
from video_writer import H264Writer


class VideoWriterTests(unittest.TestCase):
    def test_live_timestamps_and_separate_sessions(self):
        with tempfile.TemporaryDirectory() as folder:
            for session in range(2):
                path = Path(folder) / f'{session}.mp4'
                writer = H264Writer(path, 30, 321, 241, preset='ultrafast')
                for timestamp in [0, .1, .35, 1.0]:
                    writer.write(np.zeros((241, 321, 3), dtype=np.uint8), timestamp)
                writer.release()
                with av.open(str(path)) as container:
                    stream = container.streams.video[0]
                    frames = list(container.decode(video=0))
                    self.assertEqual(stream.codec_context.name, 'h264')
                    self.assertEqual((stream.width, stream.height), (322, 242))
                    self.assertEqual(len(frames), 4)
                    for frame, expected in zip(frames, [0, .1, .35, 1.0]):
                        self.assertAlmostEqual(float(frame.pts * frame.time_base), expected, places=2)

    def test_replay_frame_rate(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'replay.mp4'
            writer = H264Writer(path, 30, 320, 240)
            for _ in range(30):
                writer.write(np.zeros((240, 320, 3), dtype=np.uint8))
            writer.release()
            with av.open(str(path)) as container:
                frames = list(container.decode(video=0))
                self.assertEqual(len(frames), 30)
                self.assertAlmostEqual(float(frames[-1].pts * frames[-1].time_base), 29/30, places=2)
