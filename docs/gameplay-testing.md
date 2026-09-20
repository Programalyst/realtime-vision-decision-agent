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
