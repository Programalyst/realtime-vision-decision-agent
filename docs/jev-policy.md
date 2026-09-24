# Jev-assisted control on the shared timed runtime

As of 24 September, live `--jev` uses `jev_timed_runtime.py`, built on the same
`DeterministicRuntime`, `MotionEstimator`, route-search builder, and `TimedExecutor`
as `--deterministic`. Both use the same mouth geometry, collision checks, input
transport, 20 ms catch settling, 20 ms timing uncertainty, and 120 ms assumed
feedback delay. The deterministic milestone is the reported perfect 456 ml run
`20260924-122015-816905`. The first live run of this integration reached 396 ml
with no observed bomb hits and two missed droplets; see the review below.

## Run

The SDK is already installed. The ignored `.env` supplies `JEV_API_KEY` (or use
`TYPESAFE_API_KEY` in the environment).

```bash
uv run python testYolo.py --jev --control
```

Focus the preview and press **J** to start control and an H.264 recording. Press
**J** again to stop control and finalize the video/JSONL; Q exits. There is no
fixed session timeout. Records remain under `runs/jev/<timestamp>/`.
Without `--control`, the script makes API requests after J but sends no gestures.
No live API calls or phone gestures are needed for the automated tests.

## What Jev decides

Each independent `system_one` call asks one `Choice` question, `plan`, with up to
four dynamic IDs such as `plan_1`. Python generates collision-checked future
routes; Jev chooses among them. Single and double droplets remain one detector
class and count as one predicted collectible each. No images or conversation
history are sent. Python retains tracks, movement history, and the accepted plan.

The state includes the request ID, visible row IDs/classes/x positions, current
plan source, response lead time, and timing assumptions. Choice criteria include:

| Field | Meaning |
| --- | --- |
| `expected_catches` | Predicted sprite collections in the remaining route, including its shared near-term prefix |
| `travel` | Remaining planned horizontal travel, in screen widths |
| `predicted_safe` | The supplied path passed collision checks within its prediction horizon |
| `horizon_seconds` | How far the path was checked from the request observation |
| `row`, `action` | Tracked row and collect/skip/dodge intention |
| `target_x`, `mouth_target_x` | Body and mouth destination as fractions of screen width |
| `command_in` | Scheduled command start relative to the request observation |
| `event_in` | Estimated collection or bomb-clearance time |
| `release_in` | Earliest next command in this route |

The prompt defines these fields and asks Jev to maximize catches, avoid bombs,
and favor shorter travel when collections are equal. Completed rows are excluded
from choice scoring. Python performs trajectory and timing calculations. These
are predictions, not measurements of collected water.

## Request and execution lifecycle

1. Fresh decoded frames feed the shared estimator and local safety checks.
2. The local controller starts playing immediately and continues during API calls.
3. At most one request is in flight. Alternatives share a forecast prefix long
   enough to cover the current commitment and expected API latency. If fewer than
   two distinct future routes remain, no API call is made.
4. Replies must name an offered choice and carry finite confidence in `[0, 1]`.
   Pre-pause/reset replies, expired replies, and replies without a fresh active
   route are discarded. API errors keep local control and back off for five seconds.
5. At the activation boundary, Python rebuilds the chosen remaining intentions
   against the latest estimated geometry, including every currently known bomb.
   Old movement timestamps are not replayed. Already dispatched movement and its
   required handoff are preserved; completed fallback waits can be interrupted.
6. The replacement queue is published against the executor generation and revision.
   A concurrent dispatch causes a retry on a fresh observation, not an overwrite.
   Infeasible or fully passed choices are rejected. Acceptance requires a remaining
   chosen collection action, not just an already committed local prefix.
7. A valid Jev route persists. Local catch-count optimization does not immediately
   override a valid selection, even if Jev chose a lower-scoring alternative.
   Unsafe, unreachable, or finished routes return to local planning. New rows still
   act as collision hazards even if absent from the requested collection choices.

Defaults: `--jev-interval 0.5`, `--jev-lead 0.65`, `--jev-max-age 2`. Recent API
latency can increase the lead; choices whose commitment already exceeds the reply
lifetime are not sent. Requests may overlap drags. The input worker does not wait
for the API, and pause/stall/error protections remain independent of response time.

## Interpreting the comparison

This is **deterministic vs Jev-assisted control**. Local startup, repairs, and
safety behavior remain active. A successful hybrid run does not establish that
Jev chose every movement or improved the local policy.

Both modes log frame timing, state estimates, versioned plans, and executor
state. Jev additionally logs `request`, `request_skipped`, `response`, and
`plan_activation` events with request IDs and rejection reasons. Requests include
exact state and criteria; responses include confidence and latency. No API key
is written to the log.

- `sequence.source` / `sequence.plan.source`: current route provenance.
- `sequence.jev`: pending request, queued choice, and response lead.
- `sequence.actions[].source` and `dispatch.action.source`: provenance of the
  actual scheduled/dispatched action (`jev`, `deterministic_timed`, or
  `deterministic_safety`). A committed local action keeps its original source.
- `plan_activation.accepted`: a route was installed; it does not prove every
  future action executed or every predicted catch happened.

Compare complete recorded rounds: final water score, visible bomb hits, request
latency, accepted/rejected choices, and the fraction of drags actually attributed
to Jev. Score and hits require reviewing the video; the planner's catch count is
not a substitute. Layouts vary between games, so a single pair is illustrative,
not a controlled benchmark. Preserve the deterministic baseline recording.

## Live review: 24 September 2026

Run `20260924-140155-042584` ended at **396 ml**, with no disabled-can detections
in 1,083 logged observations. Footage review identified two missed droplets.
All 27 Jev responses chose an option tied for the highest predicted catch count;
23 selections were installed. Of 64 dispatched drags, 22 were attributed to Jev
and 42 to local control. Median API latency was 588 ms (range 415–1,278 ms).

Read-only reconstruction used the recorded geometry, completed rows, command
history, and movement commitments with the current planner. These are snapshot
counterfactuals, not a simulation of alternative phone movements:

| Miss | Finding |
| --- | --- |
| About 16 s, row 27 | Catch routes appeared intermittently, but local planning reverted to skips as estimates changed. A deterministic catch is not established. |
| About 28 s, row 60 | Ordinary search found catch routes in 52 snapshots from 25.69–27.48 s. Changing only the retained plan's source to deterministic, with a search due, made the runtime publish a collision-checked catch route at representative snapshots. |

The second miss exposes a restriction in
[`deterministic_runtime.py`](../deterministic_runtime.py): valid Jev plans suppress
periodic local optimization. At the 26.509 s request, a 650 ms forecast prefix
allowed a catch option; the actual adaptive lead of 1.015 s produced only skip
options. Jev answered in 516 ms, but activation waited another approximately
490 ms. The integration's latency allowance and plan retention therefore
restricted opportunities; classification failure is not supported by this run.

Deterministic replanning would **schedule** the second catch from those states.
Successful physical execution still needs live validation. The 456 ml and 396 ml
runs have different layouts, so they do not establish a controlled performance
ranking. The defensible finding is that local geometry and timing were sufficient
for a manually reviewed perfect run, while adding Jev has not demonstrated an
improvement. No controller changes were made during this review.

## Historical code

`jev_policy.py`, its re-export in `jev_agent.py`, and `replayJevTiming.py` retain
the earlier observation-driven integration for historical tests and replay.
They are not the live `--jev` runner. See [rolling schedules](rolling-schedules.md)
for that design and [timed execution](deterministic-execution.md) for the shared
current controller. Earlier low-scoring Jev runs used different controllers and
should not be presented as results of this implementation.
