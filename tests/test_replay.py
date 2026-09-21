import csv
from pathlib import Path
import tempfile
import unittest

from replayDeterministic import frame_time, load_detections


class ReplayTests(unittest.TestCase):
    def test_empty_frames_keep_time_but_not_previous_objects(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'detections.csv'
            with path.open('w', newline='') as handle:
                writer = csv.writer(handle)
                writer.writerow(['source_frame','source_time_ms','width','height','class',
                                 'confidence','x1','y1','x2','y2'])
                writer.writerow([0,0,100,200,'droplet',.9,20,40,40,80])
                writer.writerow([2,100,100,200,'bomb',.8,60,100,80,120])
            states, times = load_detections(path)
            self.assertEqual(states[0]['objects'][0]['box'], [.2,.2,.4,.4])
            self.assertNotIn(1, states)
            self.assertEqual(frame_time(1,times,sorted(times),30),.05)
            self.assertEqual(frame_time(2,times,sorted(times),30),.1)

    def test_rejects_repeated_timestamp_across_frames(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'detections.csv'
            path.write_text('source_frame,source_time_ms,width,height,class,confidence,x1,y1,x2,y2\n'
                            '0,0,100,200,bomb,.9,1,2,3,4\n'
                            '1,0,100,200,bomb,.9,1,2,3,4\n')
            with self.assertRaisesRegex(ValueError, 'strictly increasing'):
                load_detections(path)


if __name__ == '__main__':
    unittest.main()
