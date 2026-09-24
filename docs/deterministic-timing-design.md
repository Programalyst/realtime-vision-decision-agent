# Deterministic control: observation, planning, and timed execution

Design review, 22 September 2026. This preserves the design rationale. The
[implementation and current limits](deterministic-execution.md) are documented
separately: unique-frame observation, command-aware estimation, and timed
execution are now implemented. Device timestamps, automatic latency calibration,
and live validation remain outstanding. Jev integration is outside this work's
scope. YOLO remains learned perception; the movement policy is deterministic.

## Recommendation

Use independent observation and execution loops, with a planner that retains a
valid route and replaces only its unexecuted portion when needed. The executor
owns a bounded, versioned schedule with deadlines and expiry. It must not drain
an append-only queue of commands as quickly as possible.

There is no single frequency to synchronize all three components. Observe new
frames promptly, check the current plan on each observation, and execute planned
movements at their deadlines even between observations. Replanning should be
driven by changes in feasibility, with a periodic opportunity to improve the
future route.

## What the preceding implementation did

- `testYolo.py:108`: polls the adapter's latest image and timestamps retrieval.
  The installed adapter's `get_ndarray()` converts `last_frame`; this does not
  guarantee a new decoded frame. Its `frame_update_callback(frame_n, frame)`
  can instead identify each newly decoded frame.
- The installed `mysc/core/connection.py` disables `send_frame_meta`. The raw
  decoder therefore does not preserve the protocol's per-packet presentation
  timestamps. A decode callback fixes duplicate consumption and host timing,
  but does not establish when the phone produced the image.
- `jev_agent.py:41`: considers a retrieved frame later than the returned swipe
  command to be a post-movement observation. That implication is not valid
  across the video pipeline.
- `rolling_planner.py:273`: rebuilds the retained route from observed can
  position on every frame. `update()` does this before checking whether a drag
  is still running. It does not receive the executing drag's trajectory.
- `rolling_planner.py:314`: exports only the first target as a `Candidate`.
  The executor does not receive the schedule's command or release deadlines.
  Progression still depends on the next observation and another decision.
- `rolling_planner.py:254`: a missing droplet can immediately become a completed
  row. Detection disappearance alone is not evidence of collection or passage.
- `choice_preprocessor.py:198`: estimates velocity from consecutive observations
  and smooths it. Repeated images and uneven arrival times can distort those
  estimates and move predicted contact times around.
- Each `adbutils` swipe runs `adb shell input swipe`. It is asynchronous relative
  to inference, but an individual swipe remains blocking in its worker.

The stale-observation problem is supported by the run, but is not the only
problem. Plan revisions during a committed movement and repeated corrections
need explicit treatment too.

## Evidence from the 14:26 run

Source: `runs/deterministic/20260922-142630-082328/decisions.jsonl` and its video.
The video ends at 342 ml, with one clear explosion around 28.7 seconds.
Statistics below use log rows with session times between 3 and 33 seconds:

| Measurement | Value | Interpretation |
| --- | ---: | --- |
| Observation spacing, median | 39 ms | About 26 processed observations/s; not measured image age |
| Completed swipe calls observed | 123 | Includes repeated commands for the same objective |
| Swipe call duration, median / 95th percentile | 173 / 242 ms | Host send-to-return time, not physical arrival confirmation |
| Call duration minus requested swipe duration, median | 90 ms | Aggregate command overhead; cannot all be assigned to input onset |
| Consecutive commands targeting the same row | 61 of 122 pairs | Diagnostic evidence of repeated corrections; not proof all were unnecessary |
| Estimated adjacent-row center interval, median | 411 ms | Derived from detection spacing and the existing noisy speed estimate |

At roughly 27.606 s a rightward swipe starts and returns around 27.798 s. At
27.819 s the logged can is still near its starting position. Later observations
show the rightward movement after another command has started. This supports
delayed feedback; the existing logs cannot separate device input delay, rendering,
encoding, transport, decoding, and detection delay.

## Proposed components

### 1. Observation stream and state estimator

Use the decode callback to publish an immutable frame reference, sequence number,
and host decode timestamp into a latest-frame slot. Keep that callback short;
conversion and YOLO inference belong in the consumer. Process each sequence at
most once, dropping superseded frames when inference falls behind. Keep preview
and recording work from blocking control deadlines.

Log decode, inference start/end, and planner times separately. Where practical,
preserve scrcpy packet PTS through a project-owned adapter and map that device
timeline to host monotonic time with a measured offset and uncertainty. This
requires parsing packet headers and respecting codec configuration packets; it
is not just changing `send_frame_meta` to true. Device PTS is not itself a host
capture timestamp, nor does arrival time reveal an exact clock offset.

Until that mapping is available, explicitly estimate combined feedback delay
from controlled movements and carry a conservative uncertainty interval. Do not
rename decode time to capture time or claim it proves post-command freshness.

Maintain two states: what the last image showed at its estimated scene time,
and where objects and the can are predicted to be now. Compare an observation
with command history at that observation's time, then propagate the corrected
estimate forward. A delayed view of an earlier can position must not cancel a
movement whose effects are still on their way through the video pipeline.

Fit common vertical motion over several unique observations, initially testing
a 0.2–0.4 s window, with outlier rejection and residual-based uncertainty. Track
individual row positions; do not assume perfectly uniform spacing or a fixed
game clock. Retain briefly missing objects with bounded uncertainty. Separate
confirmed observations, predicted contact, missed rows, and unknown outcomes.

### 2. Planner: validate first, revise only when warranted

On every fresh observation, cheaply check route timing, hazard clearance, and
deviation from the expected trajectory. Preserve the route if it remains safe
and feasible. Small bounding-box changes should not restart a drag or reset
all deadlines.

Replan when a new hazard invalidates the path, timing uncertainty removes the
available margin, a target becomes unreachable, or a materially better future
route appears. Use a threshold before changing an equally good route. Urgent
safety changes bypass the normal background planning interval.

Include the executing movement as a committed prefix. Plan its successor from
the predicted handoff position and time, not an older observed can position.
If the transport cannot interrupt that movement, the planner must respect that
constraint. Future unsent segments may be replaced atomically.

Represent each step with a stable action ID, row ID, target, planned start,
latest useful start, completion/hold conditions, uncertainty allowance, and
valid-until time. Store absolute monotonic deadlines. Repeated observations can
refine them, but should not unconditionally rebase them to “now.”

Use contact and collision intervals. A collection has an alignment interval;
a bomb has an earliest possible arrival and latest possible clearance. Schedule
the escape before the former and a return after the latter, accounting for the
swept horizontal path. One object per row does not imply only one motor action
between rows: a catch followed by a dodge can require two movements.

When state has already been projected to now, compute latest dispatch as:

`arrival deadline - estimated input delay - movement time - timing allowance`

Do not subtract observation delay a second time. Evaluate feasibility across
the uncertainty bounds, not only at the mean estimate.

### 3. Executor: own the timed schedule and gesture lifecycle

Use one executor as the sole owner of phone input. It receives the newest
versioned schedule and wakes for its next deadline, a plan replacement, or a
stop signal. Keep only the current command plus a bounded replaceable future
schedule. Before dispatch, verify generation, expiry, preconditions, and that
the latest state estimate still permits execution.

Advance scheduled hold/escape steps without waiting for another YOLO result
when their timing remains validated. If observations stall or uncertainty grows
beyond the route's clearance margin, invalidate the remaining schedule. Use a
previously checked escape where available; holding still is not universally safe.
Never burst overdue actions to catch up. Pause, disconnect, and game end must
invalidate pending work and release any held touch.

A separate thread alone does not eliminate ADB command overhead. Prototype the
installed scrcpy control adapter's DOWN/MOVE/UP events as a second transport.
A continuous held drag could remove repeated can reacquisition and repeated
shell calls, while allowing future pointer positions to be updated. Validate
the game's grab offset, edge clamping, and touch behavior before relying on it.
Keep the existing ADB transport for comparison.

The installed control adapter itself has an unbounded packet queue. Send only
due touch updates; do not preload the route into it. Preserve DOWN/UP ordering,
coalesce superseded MOVE targets before sending, and measure send backlog. Socket
send completion is still not physical movement confirmation.

Scrcpy exposes control messages and timestamped media packet headers in its
[official protocol documentation](https://github.com/Genymobile/scrcpy/blob/master/doc/develop.md).
The installed adapter uses server 3.3.4; implementation must match that bundled
protocol rather than assume the current upstream version is compatible.

## Starting frequencies and how to tune them

| Component | Starting experiment | Adjustment rule |
| --- | --- | --- |
| Observe / estimate | Each unique frame, up to current 30 fps | Increase only if scene-time resolution is limiting and latency does not rise |
| Validate current plan | Each accepted observation | Also react to executor failures and expired state |
| Search for a better future route | Events plus at most 5–10 Hz background search | New safety violations trigger immediately; measure search runtime first |
| Execute schedule | Deadline-driven waits | Measure actual deadline error rather than assume OS timing precision |
| Interpolate a held touch, if supported | Trial up to 60 MOVE updates/s while moving | No unnecessary updates while stationary; tune against device response/backlog |

These are experiment settings, not an identified optimum. A 60 Hz executor
does not mean 60 observations or 60 route changes per second. The target is
small enough timing error relative to the shortest useful catch/escape window.
If input/feedback delay exceeds that window, increasing a loop frequency alone
cannot fix it.

## Implementation and validation order

1. Add unique-frame callbacks and complete timing/command instrumentation.
   Preserve runtime behavior initially so measurements remain interpretable.
2. Build a deterministic test harness with a fake clock and separate physical
   state, input delay, command return, delayed image delivery, and detector noise.
   Include the observed delayed-right-move/repeated-left-correction pattern.
3. Implement command-aware state estimation and retained, versioned schedules;
   then connect a separate executor. Test queue replacement and expiry without
   making any real gestures.
4. Compare ADB and persistent-touch transports with a short controlled movement
   sequence, ideally outside the timed game if the can is movable there. Measure
   response onset, arrival, feedback delay, residual error, and timing variability.
5. Sweep observation rates such as 15/20/30 and optional route-search rates such
   as 5/10/20 in the synthetic harness. Vary delay, jitter, missed frames,
   occasional misdetections, and speed changes independently. Choose the lowest
   rates that meet the timing margins across those tests; verify on the device.
6. Run the next live deterministic trial with stable settings and the new logs.
   Compare water, visible bomb hits, missed opportunities, redundant movements,
   action deadline error, plan revisions, and time spent without reliable state.

Required tests include duplicate frames; out-of-order or burst-delivered frames;
clock-mapping resets; delayed views of a command in progress; dropped detections;
late action expiry; plan replacement during a movement; no duplicate action IDs;
pause/disconnect with a held touch; and catch/dodge/return with variable latency.

Recorded replay remains useful for perception and timing analysis, but its can
positions follow the recorded policy. It cannot measure the score a different
executor would achieve. Synthetic physics likewise needs live calibration.

## Public description

The defensible narrative is learned object detection plus explicit motion
estimation, collision prediction, and deterministic control. Current evidence
supports a 342 ml run, not perfect play. Future claims about reliability or
score improvement should come from recorded live trials of the new design.
