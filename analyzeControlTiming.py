"""Fit combined input-onset plus visual-feedback lag from local decision logs.

This is an exploratory constant-delay fit, not a measurement of either delay
in isolation. No device connection, API call, or automatic configuration change.
"""
import argparse
from bisect import bisect_right
import json
from pathlib import Path
from statistics import median


def fit(rows, start=0., end=float('inf'), input_delay=.08):
    motions = {}
    observations = []
    for row in rows:
        for key in ('motion', 'previous_motion'):
            motion = row.get(key)
            if motion:
                motions[motion['started_at']] = motion
        for event in row.get('jev_events', []):
            if event['type'] == 'dispatch':
                motions[event['at']] = event['action']
        if not start <= row['session_time'] <= end:
            continue
        estimate = row.get('estimator') or {}
        can = estimate.get('observed_can', row['choices']['can'])
        stamp = (row.get('timing') or {}).get('decoded_at', row['choices']['timestamp'])
        if can and can['class'] == 'watering_can':
            observations.append((stamp, can['center'][0]))
    starts = sorted(motions)
    scores = []
    for index in range(101):
        lag = index*.005
        errors = []
        for stamp, x in observations:
            pos = bisect_right(starts, stamp-lag)-1
            if pos < 0:
                continue
            motion = motions[starts[pos]]
            if abs(motion['target_x']-motion['source_x']) < .05 or motion['duration'] <= 0:
                continue
            fraction = max(0., min(1., (stamp-lag-starts[pos])/motion['duration']))
            predicted = motion['source_x']+(motion['target_x']-motion['source_x'])*fraction
            errors.append(abs(x-predicted))
        if len(errors) < 20:
            continue
        errors.sort()
        trimmed = errors[:int(len(errors)*.8)]
        scores.append(dict(combined_lag_seconds=round(lag, 3),
                           trimmed_mean_error=sum(trimmed)/len(trimmed),
                           median_error=median(errors), samples=len(errors)))
    if not scores:
        raise ValueError('Need at least 20 observations associated with nontrivial movements')
    scores.sort(key=lambda item: item['trimmed_mean_error'])
    best = scores[0]
    return dict(best=best, assumed_input_delay=input_delay,
                suggested_feedback_delay=round(max(0., best['combined_lag_seconds']-input_delay), 3),
                nearby_fits=scores[:5],
                limitations=['Cannot identify input onset and feedback delay separately.',
                    'Assumes linear commanded movement and a constant combined delay.',
                    'Trims the largest 20% of errors; misses, clipping, and grab offsets can bias the fit.',
                    'Use gameplay before disabled-can animation; verify on a separate live run.'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('decisions', type=Path)
    parser.add_argument('--start', type=float, default=0.)
    parser.add_argument('--end', type=float, default=float('inf'))
    parser.add_argument('--input-delay', type=float, default=.08)
    args = parser.parse_args()
    with args.decisions.open() as stream:
        result = fit([json.loads(line) for line in stream], args.start, args.end, args.input_delay)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
