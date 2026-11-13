# Kuro pick speech synchronization

The NovaZone UI-owned MCP endpoint now streams structured progress for `pick_item`.
Optional arguments `operation_id` (unique mission ID, maximum 64 alphanumeric,
dash or underscore characters) and `speech_required` (default false) preserve
ordinary terminal/UI picking. The existing item argument and result are unchanged.

Events contain operation_id, stage and sequence. `motion_started` is emitted once
when the first arm trajectory goal is accepted; gripper goals do not trigger it.
`at_right` follows measured/validated completion of the named right trajectory.
These events are sent through MCP progress callbacks, not parsed from log text.

With speech_required=true, at_right starts a 20-second acknowledgment deadline.
`acknowledge_pick_announcement(operation_id, stage="at_right", played=true)` must
confirm actual completed speech playback. The original minimum three-second
pause still applies. Failure, timeout, cancellation, transport disconnect, or a
closed speech gate prevents release and preserves the existing recovery handling.
The acknowledgment tool only acknowledges speech; it never directly opens the
hand. Wrong IDs/stages and closed/expired gates are rejected. Duplicate identical
acknowledgments are idempotent while the gate is active.

Kuro's Robot_ai planner waits independently for initial speech before calling
pick_item. The face sends speech_required=true on SubmitMission; non-voice callers
default to false. AcknowledgePickSpeech.srv carries mission_id, stage, played and
returns success/message. ROS endpoint: /robot_ai/acknowledge_pick_speech.

The face speaks exactly:
- initial: I will pick up the [validated item] for you.
- motion_started: I am picking it up now.
- at_right: I will drop the [validated item] here.

Speech is independent of wake/input timeout, uses the existing microphone hold
and confirmed display playback, and restores the previous input/sleep state.
Completed missions are consumed silently; stale/duplicate events are discarded.
Errors and clarification still produce short spoken feedback.

Offline checks: test/test_pick_announcements.py and test/test_sam3_mcp.py use fake
controllers/pipelines and localhost HTTP; no physical motion is submitted.

## Validation on 2026-11-05

- NovaZone existing offline suite: 178 passed.
- Speech gate and real localhost HTTP MCP tests: 11 passed, including correlated
  progress, wrong-ID rejection, three-second minimum delay, failed/missing speech,
  cancellation and gripper release ordering.
- New planner speech tests: 3 passed; the existing planner tests passed as well
  (the two previously documented navigation fixture failures remain excluded).
- Face: 7 announcement tests, 4 mission-client tests, 7 global-state tests,
  20 chat tests. Tests cover asleep playback, exact text, no model invocation,
  microphone hold, actual playback acknowledgment order, failed playback,
  stale/duplicate events, silent success and suppressed generated acknowledgment.
- Supervised physical sequence completed, mission
  79f95d084953423ca1c88a3fbc954bd1, request f27f512a23684b8f8d63f07c027c6469.
  The typed input used Kuro's normal model/tool/speech loop with live ROS/MCP,
  actual browser playback acknowledgments, and existing arm controllers. All three
  exact sentences were spoken and completion produced no success speech. The arm
  released at right and returned to zero. Pickup itself remains unverified by the
  existing contact/vision checks, which are disabled in this hardware profile.
- An initial test-driver configuration used domain 0 and failed its initial speech
  gate before any motion. The corrected driver used ROS_DOMAIN_ID=122 and passed.
  Production voice and mission bridge already use domain 122. The temporary
  domain-0 status manager was stopped.

Rollout: both ROS interfaces rebuilt, planner and voice process reloaded, and
NovaZone reopened through the original main UI. The original UI continues to own
NovaZone and its camera. Arm and MoveIt services were not restarted.

## Failed-plan retry correction

A later voice pickup failed with no complete reachable plan (IK code -31).
The client had incorrectly latched uncertain motion on this known terminal result,
then hid the admission rejection behind a generic sentence. This is now separated:

- The fresh execution pipeline returns error code NO_REACHABLE_PLAN with
  motion_termination_confirmed=true and recovery_required=false only when no
  candidate was selected and BaseMotion.close() succeeds. No grasp is executed.
- The UI/MCP retains this structured error and reports readiness for a later
  explicit retry. No request is replayed automatically.
- Robot_ai clears pickup ownership only for that confirmed terminal planning
  failure. Transport failures, unconfirmed motion and recovery cases stay blocked.
- Kuro asks the user to reposition the object after an unreachable grasp, and
  distinguishes busy, unavailable-planner and motion-safety admission rejections.

The existing stale latch was cleared through cancel_mission, which confirmed
idle/ready before the service reload. No markers or motion checks were bypassed.
Validation: 245 planner tests passed (two existing unrelated fixture failures
excluded), 178 NovaZone offline tests, 8 MCP/UI tests, 9 announcement tests and
4 mission-client tests. No physical pick was submitted for this correction.

## Complete cached pickup speech

Pickup announcements now use the localhost /api/pick-announcement endpoint instead
of /api/say stream=true, in both ROS-status and legacy chat modes. The endpoint
returns a complete WAV from cached_announcement, keyed by speech model, configured
voice and exact normalized text. Missing clips are generated completely before
publishing playback. Prepare requests warm the cache without speaking. Normal
conversation streaming is unchanged. Robot status revision checks, microphone
holding and actual playback completion acknowledgments are retained.

Can/bottle initial and drop phrases, plus the shared movement phrase, were silently
prepared after reloading the display/voice components. Cached reads measured
2.0–2.5 ms. A new object/voice/model may require first-use generation; playback then
uses the complete clip. Generation failures still withhold the motion/release ack.

Validation: 3 cache/HTTP tests, 9 announcement tests, 1 cached startup test and
20 existing chat tests passed (33 total). Tests verify complete audio/wav delivery,
persistence across restart, voice/model invalidation, no repeated cloud generation,
no playback publication on errors, and retained success/failure ack ordering.
No audible speaker test or physical robot motion was submitted for this change.
The audible stutter itself still needs confirmation by the operator.

## Pending-mission wake state and clarified targets

The ordinary five-second idle sleep is now suspended while this client's accepted
or outcome-unknown mission remains pending. Mission-state notifications interrupt
the input wait so completion/failure/clarification restores the ordinary idle timer,
without announcing success. Explicit goodbye/sleep and external robot-state
commands retain precedence. No pending mission is automatically replayed.

Confirmed object replacements are expressed as standalone commands, e.g. "Pick up
the soda can" after a yes confirmation. The planner also accepts a single explicit
"pick up X instead of Y" selection while retaining the original command, exact
positive-target matching, negation, compound-action and location checks.

A per-turn submission result is cleared before reasoning so an interrupted older
accepted turn cannot silence a later answer. Voice input dequeued during an
interrupted metadata fetch is requeued; background state updates cannot consume
it silently. Temporary terminal readers also stop when their input wait is cancelled.

Validation: 254 planner tests passed, with the two previously documented unrelated
navigation fixture tests excluded. Face tests passed: 13 announcement/lifetime,
4 mission client, 7 global status, 20 chat and 18 voice input. A real cloud-model
confirmation test, with pickup submission mocked, converted a prior black-can
comparison and "Yes" into exactly "Pick up the soda can". No physical pickup was
submitted for this correction. Planner and voice process reloaded while idle.

## Pickup versus conversational camera routing

The observed "cannot see the can" responses came from look_at_camera on
/head_camera/color/image_raw/compressed. Those requests never reached Robot_ai
or NovaZone; the last NovaZone failure concerned a misinterpreted "left hand"
request. Head-camera visibility cannot establish pickup-camera visibility.

Explicit pickup/grasp commands now go directly from Chatbot.reply to the existing
MissionClient.submit, preserving the complete action/constraints for backend
validation and removing an optional conversational camera/model preflight. A
leading configured-name wake greeting is removed. Noncanonical conversational
requests and confirmations retain the language agent, with explicit instructions
that NovaZone owns pickup perception and directions/body parts are not object
identifiers. Visual questions still use the head camera. No gripper-camera,
segmentation, collision, motion or release checks are bypassed or changed.

Validation: 6 routing tests, 13 announcement/lifetime tests and 20 chat tests passed.
A real cloud-model confirmation test with mocked pickup submission and forbidden
head-camera access produced "Pick up the can", zero head-camera calls and no
extra spoken acknowledgment. Only the voice process was reloaded; no physical
pick was submitted and actual gripper-camera object visibility was not tested.

## Planning-stage announcement and verified stop recovery

“I am picking it up now” now follows successful grasp selection, all four
pregrasp/approach/target/retreat plans, trajectory validation and a final safety
check. Initial positioning at zero no longer emits this announcement. Loaded
retreat/right/return planning still occurs at the existing execution stages.

The old generic “Base must remain stopped” error combined stale odometry,
invalid velocity and measured movement. They now have separate diagnostics.
Only while idle, with an owned motion lease, fresh arm feedback, previously
stopped velocity and no movement latch, stale odometry gets a bounded 0.5-second
opportunity for a new callback. It must become fresh before any next command.
Active motion retains the existing one-second freshness limit and movement latch.
The original failure cannot conclusively distinguish a callback gap from motion;
a subsequent read-only capture showed fresh feedback and near-zero velocity.

New MCP tool `confirm_pick_stopped(object_removed=False)` and terminal command
`mcp_client.py recover [--object-removed]` send no motion. Recovery requires fresh
base/readiness/arm feedback, observed terminal action states, at least 0.3 seconds
of stable new arm samples, and no unresolved attachment. The explicit flag is an
operator acknowledgment of inspection/removal; it may clear only NovaZone-owned
scene objects and markers. Foreign attachments/markers, active controllers,
missing feedback and cancellation remain blocking. A failure after closing the
gripper records an object-may-be-held marker for operator recovery.

A new explicit mission may verify an uncertain previous pick before admission;
it does not replay that pick. A planner restart also requires this verification.
Cancellation must obtain the same proof instead of treating an idle UI as proof.
Face status retains the last failure across submission rejection, and can show
last-observed retained planner feedback without speaking or owning that mission.
No received feedback still means unknown; a new request never inherits an old
mission's successful outcome.

Validation and rollout (2026-11-06): 178 existing NovaZone checks plus 26
speech/MCP/recovery/CLI checks passed; 256 planner checks passed with the same
two unrelated navigation-contract fixture tests excluded; face checks passed
(6 mission client, 13 announcements/lifetime, 20 chat, 6 routing). Both ROS builds
succeeded. Reloaded NovaZone through its owning robot_control UI, the planner,
and the face service. MoveIt PID 7429 and the existing arm service stayed up.
The face service was already stopped; it was started and reached voice-ready.

After the operator confirmed an empty gripper and stationary base, live
`recover --object-removed` succeeded, followed by an ordinary `recover` that also
succeeded with no motion. A new face-to-planner two-object request was admitted
after the startup stop check, then rejected by the single-object restriction.
No pick tool or motor command was sent by these live checks. The actual
pickup/announcement timing remains to be checked with operator supervision.

## Controller startup without action history (2026-11-06)

Fresh action servers may not publish any status array until a goal is accepted.
Missing history is unknown, not an active previous pick. Ordinary recovery still
blocks it and names the missing controllers. After physical inspection confirms
an empty gripper and stopped arm/base, `mcp_client.py recover --object-removed`
can establish startup-idle evidence without commanding motion. It requires a
single matched live status publisher per controller, two seconds of stable fresh
physical arm feedback, stopped fresh base/readiness feedback, and the existing
attachment/marker checks. An observed active controller still blocks this path.

The evidence is held only in the live NovaZone process and tied to the exact DDS
publisher IDs. Later planner recovery probes recheck the same endpoints and all
physical checks. Active status, observed disconnection or changed publishers
invalidates the startup evidence. Restarting NovaZone also discards it; if there
is still no action history, operator inspection/confirmation is required again.
Normal terminal-status recovery and Pi behavior retain their existing checks.

Validation: 15 recovery tests passed, including missing/duplicate publishers,
active status, controller identity change, attachment ownership and cancellation.
The existing 178-check offline suite and ROS build also passed. Reloaded only
NovaZone through robot_control; arm/MoveIt and planner stayed running. Live
explicit recovery followed by ordinary recovery both succeeded without motion.
The planner stop/recovery service then returned `Motion termination confirmed`,
clearing its admission block; NovaZone reports ready. No pickup was submitted.

## Repeated recovery and stale conversational errors (2026-11-06)

Logs showed one successful soda-can sequence followed by contradictory
conversational recovery claims, and a later genuine admission failure after the
planner restarted. The missing history was specifically move_action, unused by
the NovaZone ExecuteTrajectory path. Active arm/gripper status had invalidated
all inspected-controller evidence. Evidence now invalidates per controller;
automatic recovery requires matching inspected publisher IDs only for controllers
whose history is missing, and terminal states for every controller with history.
Active motion, endpoint changes/disconnection and recovery markers still block.
A regression test simulates arm/gripper execution followed by repeated recovery
while move_action stays unused, and rejects a changed move_action publisher.

Face-agent instructions now receive a fresh mission-status snapshot each turn.
It supersedes historical conversational errors; unrelated/incomplete speech must
not introduce pickup claims. Explicit status questions use the existing status
tool. Fixed pickup announcements and silent successful completion are unchanged.

Validation: 16 recovery tests and the existing 178-check offline suite passed;
face tests passed (7 mission client/current-instruction checks, 6 pickup routing,
13 announcement/lifetime). No physical pickup was submitted for these tests.

## Priority fixes: startup verification, MCP lifetime, incomplete speech

Startup verification is now distinguished from interrupted-pick recovery with
`STARTUP_VERIFICATION_REQUIRED`. A cold controller with missing action history,
live matched publishers, fresh stationary feedback and no unresolved marker
requests one explicit empty-gripper/stopped-robot confirmation. NovaZone exposes
**Verify stopped robot (no motion)**, with a confirmation dialog defaulting to No.
This uses the existing `confirm_pick_stopped(object_removed=True)` path; it never
commands a pose or gripper movement. Markers and possible attachments retain
`PICK_RECOVERY_REQUIRED`. Busy/warming NovaZone reports `PICKING_NOT_READY`;
initial feedback checks not ready report `STARTUP_NOT_READY`. Kuro uses short,
distinct wording for each instead of calling all startup conditions a failed pick.
The MCP get_status response adds `error_code`; existing fields/tools remain.

Each planner MCPClient now owns all its async contexts in a dedicated lifetime
task. Partial initialization unwinds locally in reverse order. It retries only
connection setup (1, 2, 4, 8, then 10 seconds), pings every 5 seconds with a
3-second deadline, and reconnects lost transports. Initial RPC initialization
has a 10-second deadline. Transport retries never replay tool calls, including
pick_item, cancellation or release speech acknowledgments. Context cleanup no
longer depends on the caller's nested AnyIO cancel scopes. Planner main handles
ROS external shutdown and uses try_shutdown to avoid double-shutdown errors.

Incomplete explicit pickup phrases, including "pick up the" and "grasp it",
return OBJECT_REQUIRED and "Which object would you like me to pick up?" in the
face mission client before ROS submission/recovery checks. Complete commands,
constraints and negation retain their existing validation paths.

Validation: 178 existing NovaZone offline checks and 29 recovery/MCP checks passed.
The planner suite passed 260 tests with the same two unrelated navigation fixture
checks excluded. Three lifecycle tests also passed, including real HTTP server
late startup and restart, partial initialization cleanup, cancelled startup, and
no automatic replay of a failed picking call. Face tests passed: 8 mission client,
7 routing and 13 announcement/lifetime checks. ROS package builds succeeded.
These changes implement the three prioritized corrections; the broader durable
journaling and concurrent face-bridge refinement plan remains separate work.

Live rollout: arm/MoveIt stayed running (MoveIt PID 4665). The planner was started
while NovaZone was closed, retried without process restarts, then connected when
NovaZone reopened (same planner PID 20152, NRestarts=0). A live face-client
incomplete request returned OBJECT_REQUIRED without backend submission. A
compound request, which cannot execute a pickup, confirmed that admission
returned STARTUP_VERIFICATION_REQUIRED before interpretation. After the operator
confirmed the gripper empty and arm/base stopped, explicit and ordinary stop
verification both succeeded, and the planner stop-check service cleared its
startup block. No picking request capable of execution or motor command was
sent by these rollout checks. A physical pickup remains an operator test.

## Pickup planning pose (2026-11-06)

NovaZone emits the silent progress stage `planning_started` after the initial
zero positioning and gripper opening, immediately before live capture/perception.
The planner forwards that stage with its mission identity and no announcement.
`motion_started` still follows validated grasp/path planning and ends the pose.
The face MissionClient owns a separate cosmetic sender, shared by voice and words
runtime modes, and accepts visual stages only for its active request. Motion,
terminal outcomes, planner reset, bridge loss and runtime cleanup clear it;
late planning events cannot reactivate a finished mission. Successful completion
remains silent and no visual stage requires a speech acknowledgment.

Local-only POST `/api/pick-visual` accepts `mission_id` (1–64 alphanumeric,
underscore or dash characters) and boolean `active`. It publishes an independent
`pick-visual` SSE event and `pickVisual` status metadata; it does not modify the
conversation command, /robot/status, listening text, speech playback/acks or
microphone state. A sender refreshes every 3 seconds; the display lease expires
after 10 seconds without refresh. Stale clears cannot affect another mission,
and explicitly retired missions cannot reactivate. Visual HTTP failures are
logged separately and never block picking or speech.

Both native and pre-rendered displays select the existing Thinking/light-bulb
activity only over idle/listening. Speech, sleeping, errors and explicit commands
win; the underlying current state is restored when the visual ends. Lease
refreshes do not restart animation playback. Kuro's existing saved animation
pack already contains the thinking clip, so no rebake or artwork change is needed.

Validation: 262 planner checks passed (two previously documented unrelated
navigation fixture checks excluded), 178 NovaZone offline checks passed, plus
40 focused execution/speech-stage checks including initial-positioning failure.
Face visual/API lifecycle checks passed (9), along with mission client (8),
announcement/lifetime (13), chat (20), routing (7), global status and service
regressions. All 34 JavaScript tests and the display build passed. An isolated
browser preview visibly showed the light bulb, speaking override, planning
resumption, and clear-to-idle. No robot commands were used in the preview.
Node 22.12 was used from a temporary, official-source checksum-verified runtime
on the face device for the build; no system Node installation was changed.

Live rollout and supervised observation: the operator reopened NovaZone through
robot_control and the face display/runtime and planner were reloaded while idle;
arm/MoveIt stayed running. The can mission emitted planning_started at 01:50:13
and motion_started at 01:50:31 (Asia/Kuala_Lumpur); the display lease was active
over that interval and cleared before execution speech. The operator confirmed
that the planning pose works. That can sequence later reported a separate
Base-moved safety failure during return to zero after release. No automatic
replay or forced recovery was performed. A subsequent operator-initiated bottle
pickup was left running without service restarts. Both display modes also treat
the global LISTENING state's needs-input animation as eligible for the visual.
The latest tested static bundle was saved without restarting active processes.
