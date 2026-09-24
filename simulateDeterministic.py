"""Closed-loop timing fixture, not a calibrated simulator of the mobile game.

Physical state, input onset, command return, frame capture, frame delivery, and
the controller's delay assumption are separate. No API calls or device input.
"""
import argparse
from collections import Counter
import heapq
import json
from pathlib import Path

from choice_preprocessor import PlannerConfig, mouth_box
from deterministic_runtime import DeterministicRuntime
from timed_executor import TimedExecutor


class Clock:
    def __init__(self): self.now = 100.
    def __call__(self): return self.now


def detection(kind, x, y, w=.04, h=.04):
    return {'class': kind, 'confidence': .99, 'center': [x, y],
            'box': [x-w/2, y-h/2, x+w/2, y+h/2]}


def run_fixture(fps=25., delay=.20, assumed_delay=None, jitter=0., search_hz=7.,
                dropout_every=0, input_delay=.08, command_overhead=.09):
    clock = Clock()
    config = PlannerConfig()
    executor = TimedExecutor(clock=clock, input_delay=config.input_delay)
    runtime = DeterministicRuntime(executor, config, clock=clock,
        decode_delay=delay if assumed_delay is None else assumed_delay,
        timing_uncertainty=.02, search_interval=1/search_hz)
    runtime.set_enabled(True)
    # Spacing permits a catch, escape, same-column return, and cross-screen catch.
    objects = [('droplet', .5072, .50), ('bomb', .5072, .32),
               ('droplet', .5072, .14), ('droplet', .74, -.04),
               ('droplet', .27, -.22), ('bomb', .27, -.40)]
    x = .5
    physics = None
    job = None
    frames = []
    sequence = 0
    next_capture = 0.
    caught, hits = set(), set()
    log = []
    events = Counter()
    for tick in range(1201):
        t = tick*.005
        clock.now = 100+t
        if physics:
            source, target, begin, end = physics
            if t >= end:
                x = target
            elif t >= begin:
                x = source+(target-source)*(t-begin)/(end-begin)
        if job and t >= job[0]:
            executor.complete(job[1]); job = None
        can = detection('watering_can', x, .85, .16, .12)
        mouth = mouth_box(can, config)
        visible = []
        for index, (kind, px, py) in enumerate(objects):
            if index in caught:
                continue
            item = detection(kind, px, py+.3*t)
            box = item['box']
            if box[3] >= mouth[1] and box[1] <= mouth[3]:
                if kind == 'bomb' and box[2] >= mouth[0] and box[0] <= mouth[2]:
                    hits.add(index)
                if kind == 'droplet' and abs(px-(mouth[0]+mouth[2])/2) <= (mouth[2]-mouth[0])/2*config.catch_center_fraction:
                    caught.add(index)
                    continue
            if box[3] > .1 and box[1] < 1.:
                visible.append(item)
        if t >= next_capture:
            sequence += 1
            next_capture += 1/fps
            noise = jitter*((sequence % 3)-1)
            observed = visible[1:] if dropout_every and sequence % dropout_every == 0 else visible
            heapq.heappush(frames, (t+max(0., delay+noise), sequence,
                                   {'objects': [can, *observed]}))
        while frames and frames[0][0] <= t:
            decoded, seq, state = heapq.heappop(frames)
            runtime.update(state, seq, 100+decoded)
            events.update(e['type'] for e in runtime.events)
            log.append(dict(t=t, reason=runtime.reason, version=runtime.version,
                            x=x, target=runtime.decision.target_x if runtime.decision else None))
        command = executor.poll()
        if command:
            a = command.action
            physics = (x, a.target_x, t+input_delay, t+input_delay+a.duration)
            job = (t+a.duration+command_overhead, command)
    dispatches = len(executor.history)
    return dict(fps=fps, feedback_delay=delay, assumed_delay=delay if assumed_delay is None else assumed_delay,
                jitter=jitter, dropout_every=dropout_every, input_delay=input_delay,
                command_overhead=command_overhead, search_hz=search_hz,
                caught=sorted(caught), bomb_hits=sorted(hits), dispatches=dispatches,
                plan_versions=runtime.version, events=dict(events), trace=log)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='New JSON output path; existing files are preserved')
    args = parser.parse_args()
    results = [run_fixture(fps=fps, delay=delay, jitter=.015, search_hz=hz, dropout_every=13)
               for fps in (15., 20., 30.) for delay in (.08, .20, .30) for hz in (5., 10.)]
    summary = {'mode': 'synthetic physics; not a live-game score prediction',
               'results': [{k: v for k, v in r.items() if k != 'trace'} for r in results]}
    print(json.dumps(summary, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x') as handle:
            json.dump({**summary, 'traces': [r['trace'] for r in results]}, handle, indent=2)


if __name__ == '__main__':
    main()
