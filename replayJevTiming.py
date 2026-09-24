"""Offline timing replay with synthetic schedule selections, never live Jev calls.

Feeds recorded detections and drag availability back to the hybrid controller.
This tests response handling, not model quality, alternate gameplay, or score.
"""
import argparse
from collections import Counter
from concurrent.futures import Future
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jev_policy import JevAgent


class SimulatedExecutor:
    def __init__(self, delays):
        self.delays=delays
        self.index=0
        self.now=0.
        self.job=None

    def submit(self, fn, state, question):
        if self.job is not None:raise AssertionError('More than one in-flight request')
        future=Future()
        criteria=question.criteria
        selected=min(criteria,key=lambda key:(-criteria[key]['expected_catches'],criteria[key]['travel']))
        due=self.now+self.delays[self.index%len(self.delays)]
        self.index+=1
        self.job=(due,future,selected)
        return future

    def advance(self, now):
        self.now=now
        if self.job and now>=self.job[0]:
            due,future,selected=self.job
            response=SimpleNamespace(choices={'plan':SimpleNamespace(choice=selected,confidence=1.)})
            future.set_result((response,due));self.job=None

    def shutdown(self, **kwargs):
        if self.job:self.job[1].cancel()


class NoNetworkClient:
    def system_one(self, **kwargs):
        raise AssertionError('Offline replay must not call an API')
    def close(self):pass


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('decisions',type=Path)
    parser.add_argument('--delay-ms',type=float,default=417.)
    parser.add_argument('--latencies-from',type=Path,help='Use recorded response latencies in order, repeating if needed')
    parser.add_argument('--output-dir',type=Path)
    args=parser.parse_args()
    delays=[args.delay_ms/1000]
    if args.latencies_from:
        delays=[e['latency_ms']/1000 for line in args.latencies_from.open()
                for e in json.loads(line).get('jev_events',[]) if e['type']=='response']
    import math
    if not delays or any(not math.isfinite(d) or d<0 for d in delays):
        parser.error('Require finite nonnegative delays')
    output=args.output_dir or Path('runs/replay')/('jev-timing-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    output.mkdir(parents=True,exist_ok=False)
    agent=JevAgent(client=NoNetworkClient())
    agent.executor.shutdown(wait=True)
    executor=SimulatedExecutor(delays);agent.executor=executor
    counts=Counter();sources=Counter()
    try:
        with (output/'decisions.jsonl').open('w') as log:
            for line in args.decisions.open():
                recorded=json.loads(line);c=recorded['choices'];now=c['timestamp']
                objects=[c['can']] if c['can'] else []
                for t in c['tracks']:
                    if t['seen']==now:
                        box=t['box'];objects.append({'class':t['kind'],'confidence':1.,'box':box,
                            'center':[(box[0]+box[2])/2,(box[1]+box[3])/2]})
                executor.advance(now)
                with patch('jev_policy.time.monotonic',return_value=now):
                    agent.update({'objects':objects},now,movement_ready=recorded.get('movement_ready',True))
                counts['frames']+=1
                sources[agent.sequence.source]+=1
                for event in agent.events:
                    counts[event['type']]+=1
                    if event['type']=='plan_activation':
                        counts['activated' if event['accepted'] else 'activation_rejected']+=1
                    if event['type']=='response' and event['discard_reason']:
                        counts['discarded_response']+=1
                log.write(json.dumps({'session_time':recorded['session_time'],
                    'status':agent.status,'source':agent.sequence.source,
                    'selected':agent.decision.id if agent.decision else None,
                    'events':agent.events,'schedule':agent.sequence.snapshot})+'\n')
        summary={'mode':'offline synthetic selector; NOT Jev quality or gameplay outcomes',
                 'source':str(args.decisions),'latencies_from':str(args.latencies_from) if args.latencies_from else None,
                 'fixed_delay_ms':args.delay_ms if not args.latencies_from else None,
                 'counts':dict(counts),'frame_sources':dict(sources),
                 'limitations':['Selects highest predicted catch count locally in place of Jev.',
                    'Uses original can positions and original drag availability, not counterfactual motion.',
                    'No network requests or Android gestures.']}
        (output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
        print(json.dumps(summary,indent=2));print('Saved',output)
    finally:
        agent.close()


if __name__=='__main__':main()
