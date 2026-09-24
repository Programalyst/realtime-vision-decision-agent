# Timed deterministic execution

Implemented on 22 September after reviewing the 342 ml run. The next live
deterministic run reached 372 ml with no reported bomb hits; timing assumptions
still need calibration. The 24 September milestone run reached 456 ml with no
bomb-hit indication. Live Jev mode now uses this same runtime through
`jev_timed_runtime.py`; see [Jev policy](jev-policy.md) for its asynchronous route
selection and provenance logging. The new Jev integration awaits a live test.

## Run

```bash
uv run python testYolo.py --deterministic --control
```

Start on the game screen with J; pause with J and quit with Q. Recording still
starts and stops with J and uses H.264. The default input transport remains ADB.
Without `--control`, the runtime shows proposals and sends no input.

The new files separate responsibilities:

| File | Responsibility |
| --- | --- |
| `frame_observer.py` | Decode callback, sequence IDs, latest-frame mailbox |
| `motion_estimator.py` | Delayed observations, command-history projection, robust vertical-speed estimate |
| `deterministic_runtime.py` | Validate retained routes, revise future steps, publish versioned schedules |
| `jev_timed_runtime.py` | Optional asynchronous Jev route selection on the same runtime |
| `timed_executor.py` | Independent deadline worker, command lifecycle, expiry and cancellation |
| `scrcpy_drag.py` | Optional persistent-touch transport |
| `simulateDeterministic.py` | Synthetic closed-loop timing fixture and parameter sweep |

`rolling_planner.py` still provides route search and `choice_preprocessor.py`
provides tracking and shared collision geometry. The previous
`DeterministicController` remains for historical tests and shadow replay.

## What changed

The live script consumes each decoded frame at most once. The callback only
publishes a frame reference and timestamp; image conversion, inference, and
drawing happen outside the decode thread. Frames superseded while inference is
busy are dropped. Decode time is explicitly different from device capture time.

The estimator keeps the observed can and a prediction of its present position.
It adds movement implied by commands issued since the image's estimated scene
time. A delayed view of an earlier can position therefore does not automatically
trigger another swipe. While a frame is too early to confirm arrival, residuals
within the configured timing uncertainty retain the command prediction; a later
observation can correct an endpoint that was not reached. Vertical speed uses a 350 ms window of track samples and
median slopes measured over at least 80 ms. Lost bombs remain hazards until
projected clearance; a missing droplet remains an estimated objective for up to
150 ms instead of immediately being marked collected.

Bomb collision windows expand by timing uncertainty; collectible windows shrink
to the overlap shared by early and late arrival estimates. Physical hitbox and
constant-speed assumptions still need calibration.

### Unpadded bomb-hold experiment (23 September)

The 22 September run reached 372 ml with no reported bomb hits. To investigate
missed droplets, bomb holds now end when the **unpadded detector box's top** is
predicted to pass the mouth's bottom, plus the existing 5 ms clearance buffer.
This excludes the estimator's vertical expansion and `vertical_margin` from
hold timing. Movement-end turnaround can still delay the next command.

The estimator records its bomb expansion as `Track.timing_padding`, allowing
the planner to recover the projected detector edge. This does not trim a fuse
or empty space already inside the detected bounding box. Padded collision
checks still apply to the entire next movement, including bombs whose holds
have ended. Consequently, an earlier release does not guarantee an earlier
crossing. Catch settling, input-delay assumptions, and skip choices are unchanged.
The first live test exposed the queue-ordering bug described below.
Older logs lack the new padding field;
reconstructing their padded tracks with its default of zero cannot recover the
original detector edge without estimator provenance.

The first live experiment (`20260923-120809-581168`) stopped around 21 seconds
with `Actions must be in deadline order`. Rebuilding a saved state at 17.954 s
reproduced the same invalid ordering: an overlapping bomb's no-movement hold
released at +95 ms even though the previous dodge occupied the branch until
+280 ms. The planner now bounds bomb release by movement completion even when
the next step requires no movement, so later steps cannot rewind the timeline.
Executor validation also rejects release-before-start actions. If a malformed
queue reaches the runtime, it logs `schedule_rejected`, clears pending actions,
and replans on the next observation instead of crashing; it never sorts actions
whose paths depend on their original order. A saved-state regression fixture
and recovery tests cover this failure. The subsequent 402 ml run completed.

### Shorter post-catch hold (23 September)

The subsequent run (`20260923-123003-772596`) reached a reported 402 ml with no
bomb hits. Reviewing its skipped droplet near 29 seconds showed that reducing
`catch_settle` from 50 to 20 ms permitted a predicted catch-and-left-dodge route
under the existing collision checks. The default is now **20 ms**; collision
margins and input-delay assumptions are unchanged. This setting awaits live
validation. The separate skip before the bomb chain near 26 seconds was not
resolved by this change in saved-state checks.

Each fresh observation validates the retained plan. Background search runs at
most every 150 ms by default, while invalid routes trigger immediately. Equal
routes retain their command IDs and absolute deadlines. A better route must
add a reachable droplet; small apparent travel improvements do not cause a
switch. Already dispatched steps and their hold intervals form a committed
prefix. No feasible full route triggers a short checked escape/hold where one
exists; there is no guarantee of recovering an already unavoidable collision.

Fallback waits (negative row IDs) are interruptible as of 24 September. Their
350 ms horizon describes how long staying there was checked as safe, not a
mandatory pause. Once any fallback input has returned and its modeled movement
plus the existing turnaround allowance have finished, each fresh observation
can search without committing the remaining fallback wait. A validated route
with at least as many predicted catches can replace it, including an equal-count
route that starts collecting earlier. If no qualifying route is found, a still
valid fallback remains in place. Actual bomb-row and catch holds retain their
existing commitment rules, and collision checks are unchanged.

Saved-state regressions from `20260924-112028-540975` cover the late attempt
near 10 s and skipped droplet near 26.5 s, along with in-flight input, turnaround,
and no-replacement cases. This change awaits live validation.

The executor runs independently of inference, preview, and recording. It
dispatches due steps, holds, and escapes without requiring another YOLO result,
provided the observation lease remains valid. A new schedule atomically
replaces the unsent suffix. Consumed action IDs cannot execute again. Publishing
against an outdated executor revision fails, preventing a search/dispatch race
from overwriting a newly executing movement.

Dispatch deadlines have 35 ms slack. Late actions expire instead of being sent
in a burst. The observation lease lasts 200 ms after validation. A stalled
observer expires pending work and releases persistent touch; an ADB swipe
already inside Android must finish. Missing-can observations invalidate the
route. No detected scrolling for 700 ms suppresses control. There is no fixed
session timeout: press J to start control and recording, then J again to pause
control and finish the video/JSONL. Q also stops control and closes the session.
The previous 35-second limit was removed after it cancelled a planned catch in
`20260923-132238-690196`, because waiting before gameplay consumed the allowance.
The remaining guards are not a general-purpose game-screen classifier: start
at the game screen and pause at the result screen.

## Calibration settings

| Setting | Default | Meaning |
| --- | ---: | --- |
| `--feedback-delay` | 0.12 s | Assumed phone-scene-to-decode delay |
| `--timing-uncertainty` | 0.02 s | Margin around predicted vertical timing |
| `--plan-interval` | 0.15 s | Background route-search interval |
| `--input-delay` | 0.08 s | Estimated command-to-movement onset |
| `--drag-speed` | 3 widths/s | Desired movement-speed model |

**The 120 ms feedback delay is an initial fitted assumption, not a separate
measurement of video latency.** An exploratory fit to the pre-hit portion
(4–28.5 s) of `20260922-142630-082328` found about 195 ms combined input-onset
and visual-feedback lag. Subtracting the existing 80 ms input assumption gives
115 ms, rounded to a 120 ms initial feedback setting. The fit uses 542 position
samples and trims the largest 20% of errors; grab offsets, clipping, and failed
commands can bias it. It cannot identify the two delays separately.

Repeat the analysis without sending phone input:

```bash
uv run python analyzeControlTiming.py \
  runs/deterministic/20260922-142630-082328/decisions.jsonl \
  --start 4 --end 28.5 --input-delay 0.08
```

The installed video adapter disables media timestamp headers. This implementation
does not recover device PTS or synchronize the phone and host clocks. A wrong
delay estimate can still cause misses. Increasing inference frequency alone
does not fix that mismatch. Validate timing on a separate live run.

The existing 80 ms minimum swipe duration and 120 ms command-turnaround allowance
remain. The runtime does not automatically attribute all ADB overhead to input
onset, and command return is never labeled physical arrival confirmation.

For a separately calibrated transport experiment:

```bash
uv run python testYolo.py --deterministic --control --input-transport scrcpy
```

This holds one touch across movements and sends due MOVE positions up to 60 Hz,
with UP on pause, expiry, or cleanup. It uses the installed library's packet
encoder and a control-only connection. Writes bypass its unbounded packet queue
and use `sendall` with a socket timeout; future movements are not preloaded into
the socket. The game's grab offset, sustained-touch response, input onset, and
edge clamping have not been tested live. Keep this opt-in until calibrated.

## Logs and overlays

`decisions.jsonl` includes:

- `timing`: frame sequence, decode time, inference start/end, and planning end.
- `estimator`: assumed scene time, raw can, projected can, speed/spread, and
  whether each track was projected after a missed detection.
- `sequence`: retained plan, version, action IDs/deadlines, and predicted step
  completions. These are not confirmed catches or score measurements.
- `executor`: current dispatched action and plan version.
- `jev_events`: retained field name for compatibility; in deterministic mode
  these are local dispatch, command-return, hold, expiry, and plan-change events.
  They include dispatch error and command timing, not Jev requests.

The red mouth/yellow catch band are drawn at the observed can in the displayed
image. Cyan shows the proposed mouth-center destination. The estimator's
projected present position is in the log, not substituted into the image's
observed mouth overlay.

## Offline verification

```bash
uv run python -m unittest discover -s tests -p 'test_*.py'
uv run python simulateDeterministic.py --output runs/replay/timed-fixture.json
```

The fixture separates physical movement, input onset, command return, image
capture, and delayed delivery. It includes a same-column catch/dodge/return and
cross-screen catches. The standard sweep covers 15/20/30 observation fps,
80/200/300 ms feedback delay, 5/10 Hz background planning, 15 ms arrival jitter,
and periodic missing detections. Its physical rules are deliberately simple;
successful simulated catches are not predictions of live-game scores.

All 82 tests pass. The new tests also cover duplicate/out-of-order observations, schedule
replacement, stale publication races, action expiry, pause generations,
transport failures, partial cancellation, and persistent-touch packet lifecycle.
The old `replayDeterministic.py` remains a shadow replay of the historical
observation-driven policy. It does not exercise this executor or simulate the
can's response to its commands.

Next live validation should inspect delay calibration, duplicate corrections,
dispatch error, plan churn, missed droplets, and bomb hits. No phone gestures or
Jev API calls were used while implementing this change.

The final sweep artifact is `runs/replay/timed-executor-final-validation.json`.
All 18 standard scenarios collected four synthetic droplets with zero collisions.
The matched mean delay is supplied to each scenario; incorrect calibration is
a separate limitation, and an exploratory +40 ms delay mismatch missed one
droplet in this fixture. No perfect-play claim follows from these results.
