"""Shadow-test the historical observation-driven controller on video/YOLO CSV.

The recorded can always follows the human. Proposals do not change the scene;
this is policy inspection, not a simulated score or bomb-hit measurement.
The new live timed executor is tested by simulateDeterministic.py instead.
"""
import argparse
from bisect import bisect_left
from collections import Counter
import csv
from dataclasses import asdict
from datetime import datetime
import json
import math
from pathlib import Path

from deterministic_controller import DeterministicController


def load_detections(path):
    """Read normalized states, including original source timestamps."""
    states, times = {}, {}
    with path.open(newline='') as handle:
        for row in csv.DictReader(handle):
            frame = int(row['source_frame'])
            timestamp = float(row['source_time_ms']) / 1000
            width, height = float(row['width']), float(row['height'])
            values = [float(row[k]) for k in ('x1', 'y1', 'x2', 'y2', 'confidence')]
            if (frame < 0 or width <= 0 or height <= 0 or timestamp < 0
                    or not all(math.isfinite(v) for v in [width, height, timestamp, *values])):
                raise ValueError('Invalid CSV coordinates or timestamp')
            if frame in times and times[frame] != timestamp:
                raise ValueError('Conflicting timestamps within a frame')
            times[frame] = timestamp
            x1, y1, x2, y2, confidence = values
            states.setdefault(frame, {'objects': []})['objects'].append({
                'class': row['class'], 'confidence': confidence,
                'box': [x1/width, y1/height, x2/width, y2/height],
                'center': [(x1+x2)/2/width, (y1+y2)/2/height]})
    indices = sorted(times)
    if not indices or any(times[b] <= times[a] for a, b in zip(indices, indices[1:])):
        raise ValueError('CSV must contain strictly increasing frame timestamps')
    return states, times


def frame_time(frame, times, indices, fps):
    """Interpolate frames without detections; never reuse their previous state."""
    if frame in times:
        return times[frame]
    pos = bisect_left(indices, frame)
    if 0 < pos < len(indices):
        a, b = indices[pos-1], indices[pos]
        return times[a] + (times[b]-times[a])*(frame-a)/(b-a)
    anchor = indices[0] if pos == 0 else indices[-1]
    return times[anchor] + (frame-anchor)/fps


def main():
    import cv2
    from video_writer import H264Writer

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video', type=Path)
    parser.add_argument('--detections', type=Path, help='Default: detections.csv beside video')
    parser.add_argument('--start', type=float, default=0)
    parser.add_argument('--end', type=float, default=None)
    parser.add_argument('--output-dir', type=Path)
    args = parser.parse_args()
    if (not math.isfinite(args.start) or args.start < 0 or args.end is not None
            and (not math.isfinite(args.end) or args.end <= args.start)):
        parser.error('Require 0 <= start < end')
    csv_path = args.detections or args.video.with_name('detections.csv')
    states, times = load_detections(csv_path)
    indices = sorted(times)
    cap = cv2.VideoCapture(str(args.video))
    writer = None
    output = args.output_dir or Path('runs/replay') / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    counts = Counter()
    policy = DeterministicController()
    policy.set_enabled(True)
    first_time = last_time = None
    try:
        if not cap.isOpened():
            raise ValueError(f'Cannot open {args.video}')
        fps, frames = cap.get(cv2.CAP_PROP_FPS), int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError('Video has no usable frame rate')
        # This mode requires the full, untrimmed annotated export, preserving
        # source frame indices. Matching counts cannot prove matching content.
        if indices[0] != 0 or indices[-1] >= frames or frames-indices[-1] > 3:
            raise ValueError('CSV/video frame ranges differ; use the matching full export')
        output.mkdir(parents=True, exist_ok=False)
        with (output/'decisions.jsonl').open('w') as log, (output/'events.csv').open('w', newline='') as event_file:
            events = csv.writer(event_file)
            events.writerow(['time_s', 'source_frame', 'active_row', 'kind', 'target_x',
                             'human_body_x', 'predicted_safe', 'reason'])
            previous = None
            frame_index = 0
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                index = frame_index
                frame_index += 1
                timestamp = frame_time(index, times, indices, fps)
                if timestamp < args.start:
                    continue
                if args.end is not None and timestamp > args.end:
                    break
                state = states.get(index, {'objects': []})
                status = policy.update(state, timestamp)
                choices, decision = policy.choices, policy.decision
                active = next((t for t in choices.tracks if t.id == policy.active_track), None)
                counts['frames'] += 1
                counts['missing_can_frames'] += choices.can is None
                counts['proposal_frames'] += decision is not None
                counts['unsafe_proposal_frames'] += decision is not None and not decision.safe
                log.write(json.dumps({'source_frame': index, 'source_time': timestamp,
                                      'status': status, 'active_row': policy.active_track,
                                      'sequence': policy.sequence.snapshot,
                                      'selected': asdict(decision) if decision else None,
                                      'choices': asdict(choices)})+'\n')
                signature = (policy.active_track, decision.safe if decision else None)
                if signature != previous:
                    events.writerow([round(timestamp, 3), index, policy.active_track,
                                     active.kind if active else '', decision.target_x if decision else '',
                                     choices.can['center'][0] if choices.can else '',
                                     decision.safe if decision else '', status])
                    previous = signature
                    counts['row_or_safety_transitions'] += 1
                h, w = frame.shape[:2]
                if choices.mouth_box:
                    x1, y1, x2, y2 = choices.mouth_box
                    cv2.rectangle(frame, (round(x1*w), round(y1*h)),
                                  (round(x2*w), round(y2*h)), (0, 0, 255), 2)
                if decision and choices.can:
                    mouth = choices.mouth_box
                    offset = (mouth[0]+mouth[2])/2-choices.can['center'][0]
                    x = round((decision.target_x+offset)*w)
                    cv2.line(frame, (x, 100), (x, h-1), (255, 255, 0), 2)
                # Header labels the counterfactual limitation on every frame.
                cv2.rectangle(frame, (0, 0), (w, 100), (25, 25, 25), -1)
                lines = ['SHADOW REPLAY - recorded human motion',
                         'Red: human mouth | Cyan: proposed mouth target',
                         f'{timestamp:.2f}s  {status}']
                for line_no, text in enumerate(lines):
                    cv2.putText(frame, text, (8, 25+line_no*29), cv2.FONT_HERSHEY_SIMPLEX,
                                min(.48, w/1200), (255, 255, 255), 1, cv2.LINE_AA)
                if writer is None:
                    writer = H264Writer(output/'shadow.mp4', fps, w, h)
                    first_time = timestamp
                writer.write(frame, timestamp-first_time)
                last_time = timestamp
                if counts['frames'] % 300 == 0:
                    print(f"Replayed {counts['frames']} frames", flush=True)
        if not counts['frames']:
            raise ValueError('No frames in the requested interval')
        summary = {'mode': 'shadow; no simulated movement or measured game outcomes',
                   'video': str(args.video), 'detections': str(csv_path),
                   'start': first_time, 'end': last_time, 'counts': dict(counts),
                   'config': asdict(policy.preprocessor.config),
                   'limitations': ['Human movement is fed back on every frame.',
                       'No ADB blocking, capture latency, or alternate-path game physics simulated.',
                       'Cached YOLO detections are not manually verified ground truth.',
                       'Unsafe proposal counts are frames, not bomb hits.']}
        (output/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
        print(json.dumps(summary, indent=2))
        print(f'Saved {output}')
    finally:
        cap.release()
        if writer is not None:
            writer.release()


if __name__ == '__main__':
    main()
