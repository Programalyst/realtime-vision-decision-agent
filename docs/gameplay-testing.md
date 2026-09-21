# Gameplay testing and results

Use this log for the next available daily attempts and for a subsequent public
results post. Preserve the distinction between measured scores, manual
observations, and behavior only covered by offline tests.

## Observations so far

These are user-reported observations from separate rounds. Exact water totals,
miss counts, and comparable spawn sequences have not been recorded here.

| Controller version | Bomb hits reported | Observation |
| --- | ---: | --- |
| Initial Jev snapshot policy | 3 | Basic left/right/hold decisions with fixed-distance drags |
| Initial deterministic planner | 1 | Better than the first Jev run, but missed some droplets |
| Mouth-only collision geometry | 0 | Avoided bombs in that round, but still missed droplets |
| Full-width moves and catch-and-escape planning | 1 | Collected more droplets; fewer misses than before |
| Shallower mouth + centered catches (current) | Not yet tested live | 27 offline tests pass; awaiting refreshed attempts |

The zero-hit run does not establish that the earlier controller was better:
rounds and controller settings differed, and collection totals are unavailable.

## Changes to assess next

The latest deterministic implementation:

- Keeps the mouth's top at 6% of the whole-can box height and moves its bottom
  from 30% to 18%, halving the mouth height.
- Uses a separate vertical bomb margin of 0.004 screen heights, retaining the
  horizontal margin of 0.015 screen widths.
- Counts a predicted catch only when the droplet center enters the central 25%
  of mouth width while the sprite vertically overlaps the mouth.
- Aims at droplet centers and replans from observed can positions to correct
  movements that stop short.

Full-width moves and catch-and-escape sequences remain enabled. A deliberately
scheduled **wait for a leading bomb to pass, then cross** maneuver is not yet an
explicit candidate type. Do not attribute that capability to this version.

## Next-session procedure

1. Record the commit being tested with `git rev-parse --short HEAD`, and note any
   uncommitted changes with `git status --short`. Confirm the model is v3.
2. Connect the phone, keep its orientation fixed, and open the game. Keep the
   model, confidence threshold, device, and capture setup consistent across runs.
3. Run the deterministic controller first:

   ```bash
   uv run python testYolo.py --deterministic --control
   ```

   Defaults are estimated drag speed 3 screen widths/second and input delay
   0.08 seconds. Record any overrides. These values are not measured physics.
4. Focus the YOLO preview and press **J** at the game screen. Check the red mouth
   rectangle and yellow center-alignment band. Play to the result screen, note
   the final water amount, then press **J** to stop and finalize the recording.
5. Save the printed session path. Deterministic outputs are
   `runs/deterministic/<timestamp>/annotated.mp4` and `decisions.jsonl`.
6. With another attempt available, run the existing Jev controller:

   ```bash
   uv run python testYolo.py --jev --control
   ```

   It reads `JEV_API_KEY` from ignored `.env`. Use J in the same way. Recordings
   are saved under `runs/jev/<timestamp>/annotated.mp4`; Jev mode does not produce
   the deterministic candidate log.
7. Review the videos before changing parameters. Mark the timestamps of bomb
   hits, missed droplets, delayed escapes, and movements that stop short.

Q or Ctrl+C also finalizes an active recording. No new API calls or drags are
submitted while paused; an already executing drag can finish. Recordings contain
processed annotated frames, not every phone frame, and have no audio.

## Results template

Copy one row per round; leave unknown values blank rather than estimating them.
A double-droplet sprite is one collectible, but its water value differs, so the
final in-game water total is the primary collection measure.

| Run / session path | Commit + local changes | Mode | Settings overrides | Final water (ml) | Bomb hits | Missed collectibles (manual) | Notes |
| --- | --- | --- | --- | ---: | ---: | ---: | --- |
| | | Deterministic | None | | | | |
| | | Jev | None | | | | |

For each noteworthy event:

| Video time | What happened | Detection present? | Mouth/center alignment | Chosen action / observed movement | Hypothesis |
| --- | --- | --- | --- | --- | --- |
| | | | | | |

The deterministic log contains proposed choices and predicted outcomes, not
confirmed Android commands or catches. It does not measure actual drag completion
times. Use the video to distinguish what was proposed from what visibly happened.
Model confidence and predicted safety are not game outcomes.

## Interpreting the comparison

Both modes share YOLO v3, but currently have different decision inputs and controls:

| | Deterministic | Current Jev |
| --- | --- | --- |
| Input | Tracked detections, motion estimates, candidate trajectories | One snapshot of normalized detections |
| Movement | Variable-distance targets and scheduled escape | Left/right/hold with 8%-width steps |
| Decision cadence | Each processed detection frame; one drag at a time | Minimum 0.5 seconds between requests; one request in flight |
| Collision geometry | Calibrated mouth approximation and safety margins | Snapshot policy instructions; no candidate-path collision calculation |

This is a comparison of complete pipelines, not evidence isolating the quality of
Jev's decision-making. To isolate that later, give Jev and the deterministic policy
the same candidate set and execution layer. Additional runs are needed because
spawn patterns and detection/control delays can vary.

## Preparing a public results post

Include the model version and dataset size (130 annotated images), the tested
code revision, per-round water totals and bomb hits, and short timestamped clips
showing both successful maneuvers and remaining misses. Describe the progression
from snapshot decisions to motion-aware planning and mouth-centered geometry.

Clearly label the number of trials and the differing controller inputs. Separate
YOLO validation metrics from live gameplay performance. Claim only observed
results; the current changes await live testing. Keep API keys out of screenshots
and shared notebook/code outputs. Videos and session logs remain local under the
Git-ignored `runs/` directory; publish selected artifacts separately if desired.

## Row-based refinement after the 20260921-092451-330568 run

Review found two bomb hits, around 12.5 s and 21.3 s. Decision-log rows arrived
at a median interval of about 39 ms; requested swipes had a median duration of
80 ms, while submission-to-observed-completion was about 227 ms. These logs
included polling and do not establish exact physical travel time.

The new policy retains the next incoming row: align for droplets; for bombs,
stay clear or dodge toward the next droplet. It replaces live multi-step escape
scheduling. Tracking continues during drags, but new decisions wait for command
completion and two fresh, sufficiently stable can-position observations. Logs
now capture busy/settling frames as well as proposals. `completed_at` for new
runs is measured when the ADB call returns, unlike older polling timestamps.

This version passed 33 offline tests but regressed in the next live run below.
The former rows above remain historical results, not results for this policy.


## Regression review: 20260921-094536-204705

The user reported more bomb hits and missed droplets. At 4.744 s, the log
selected bomb track 7 above droplet track 5. Independent velocity estimates
incorrectly let a higher row preempt the lower one. Footage around 5 s shows
the resulting premature dodge away from the droplet. Median command
submission-to-ADB-return time was about 191 ms; the additional two-observation
settling gate further delayed opportunities to correct or dodge.

Corrections order rows by vertical center, use a common median measured falling
speed for row-mode predictions, and allow replanning on the first observation
after ADB completion. Tracking and safety checks continue on every processed
frame. Reacquired lower objects take priority. Jev's policy remains unchanged.
Replaying the recorded detections at 4.744–5.131 s now selects droplet track 5
and its center-aligned target instead of bomb track 7. This validates that
selection change, not counterfactual gameplay outcomes.

**Next action:** test the corrected deterministic controller before changing Jev.
Device capture latency and physical drag arrival time remain unmeasured.


## Screenshot review: images/troubleshoot_1

Reviewed only the five supplied screenshots, not the recording. They show a
bomb collision and consecutive missed droplets. Still frames cannot establish
exact command latency, velocity, or the complete sequence of requested moves.
The cyan destination lines in screenshots 1–2 lie left of the detected can.

Code review exposed three additional policy defects: bomb handling preferred a
minimal dodge/hold over safe preparation for a following droplet; missing
caught/missed droplets could retain priority for the tracking TTL; safety checks
could end at row clearance while a drag was still in progress.

The controller now prefers safe alignment toward the next reachable droplet,
excludes missing and kinematically unreachable droplets from row objectives,
and checks collisions through the entire drag plus the replanning allowance.
Bombs retain their tracking grace period. A missing droplet can become an
objective again if redetected and reachable. Velocity and tick-rate settings
were not changed. Forty offline tests pass, including approximate screenshot-1
geometry, missing/unreachable droplet handoff, and a collision after the previous
safety cutoff. These are policy regressions, not a simulation of the actual run.
Live validation remains pending; Jev's policy is unchanged.


## Offline perfect-run reference

`replayDeterministic.py` replays the matching cached detections alongside the
human video, with no phone or Jev connection. Initial reference:
`runs/video/20260919-235017-767116/annotated-h264.mp4`, source 5–35.5 s.
Output: `runs/replay/perfect-run-reference/` (ignored by Git).

915 frames were processed, all with a detected can. Ten frames had no
predicted-safe choice, grouped at 8.541 s, 15.702–15.803 s, and
16.447–16.565 s. These are useful collision/latency calibration cases against
the user's successful run, not ten measured or simulated hits. The planner
assumes an initially stationary can during input delay; the human can may
already be moving. Detector boxes and conservative collision margins also need
checking before interpreting these flags as controller mistakes.

The shadow video displays actual human mouth geometry and proposed targets;
JSONL captures the full candidate states, and CSV lists row/safety transitions.
The replay does not feed proposed positions back into gameplay, and cannot
measure the controller's hypothetical score or verify ADB responsiveness.
Forty-two tests pass, including missing-frame timestamp interpolation and
rejection of invalid timestamp order. No live policy tuning was done from this
reference run alone.
