# Native no-fault window workflow (candidate)

This development source adds a single-method no-fault path without changing scheduler or candidate generation.
The historical snapshot tree is provenance metadata, not a Git commit or release; the actual source revision is the PR commit.
Existing synthetic/windows/evaluate/recheck and fault interfaces retain their defaults.

## Commands

Create the public synthetic input package:

~~~text
python -B -X utf8 scripts/make_no_fault_example.py --out outputs/synthetic_source
~~~

Keep the printed source_sha256 independently. It identifies source_manifest.json, not the result manifest.
Use that expected value in every command:

~~~text
python -B -X utf8 scripts/team3_run.py --budget-seconds 70 no-fault --source outputs/synthetic_source --source-sha256 EXPECTED_SHA --decision-s 120 --method improved --out outputs/native_run
python -B -X utf8 scripts/team3_run.py --budget-seconds 30 evaluate-no-fault --source outputs/native_run/run/source --source-sha256 EXPECTED_SHA --decision-s 120 --tasks outputs/native_run/run/selected.csv --out outputs/native_eval
python -B -X utf8 scripts/team3_run.py --budget-seconds 30 recheck --source outputs/native_run/run --source-sha256 EXPECTED_SHA --out outputs/native_recheck
~~~

no-fault supervises production, native independent evaluation and read-only recheck in that order.
Its result directory is OUT/run; OUT/workflow.json retains each child argv, original return code, timeout
and input protection evidence. Independent evaluation and recheck never invoke the scheduler.
Use a new output path each time. No cache or default TLE input is used.

decision-s is a decimal integer relative to frozen UTC start, inclusive of zero and full horizon.
Only baseline or improved is accepted. Fault arguments, external prepared state and PASS receipts are not inputs.
The original scheduler receives an independent 30-second Clock. A workflow lacking the full block becomes
BLOCKED before the solver. The solver block is 30 seconds including a 1-second save margin;
2 additional seconds are required at final admission (32 seconds total). There is no timeout-success recovery.
Parent input-protection snapshots and finalization also consume the same workflow deadline.
After a successful child, an expired parent deadline must produce BLOCKED/3 rather than workflow PASS;
retained child success codes remain diagnostic evidence, not an extension of the workflow budget.

## Input: no_fault_source v1

Exactly these files are supported:

~~~text
source_manifest.json
standard_input/targets.csv
standard_input/stations.csv
standard_input/windows.csv
standard_input/metadata.json
candidates.csv
initial_plan.csv
model.json
~~~

The manifest contains exactly schema_version=1, kind=no_fault_source, data_version, model,
origin={source_id, execution_version}, and members. Each member maps its canonical safe relative name
to {path, bytes, sha256}. The manifest excludes itself. Unknown/missing members, absolute paths, ..,
duplicate JSON keys/CSV columns, links and reparse points are rejected. The required external SHA256 is
provided by the caller; the loader never calculates its own trusted expected value.

model.json contains only schema_version=1, kind=no_fault_model, durations_seconds=[120,240],
weights={high:5,medium:3,low:2}, switch_seconds=30, frozen horizon_start_utc, horizon_end_utc,
horizon_seconds, exact station_ids, and elevation_degrees=[10,85].
Executable input/cache/reuse/fault paths are prohibited by the exact schema.

The existing four-table v1 contract remains authoritative. Complete task CSVs contain all TASK_FIELDS,
with optional matching target_id; columns are unique and no fields are recalculated to hide a false claim.
UTC uses integer-second Z format. points_int and decimal score are checked against each other,
then against actual UTC duration and authoritative priority. Original tasks must be legal success tasks
from the complete frozen candidate pool. Candidate rows are individually legal; mutual conflicts are allowed.

SHA256 establishes identity, not an official signature. A party who controls both a package and the
caller's expected hash can define a different input. The loader makes no authentication claim about that party.

## History and selection

Retained tasks preserve every original CSV value and row order except status:
end<=t is completed; strict start<t<end is locked at every station without truncation;
start>=t is new_success. There are no interrupted tasks or station blackouts.

A candidate is independently checked against original+t, the complete original candidate pool,
single matching windows, UTC duration, target uniqueness, complete occupancy and the 30-second switch.
The selector reopens and validates the package; it never accepts prepared tickets.
Even a recomputed unkeyed payload hash cannot admit a task outside the original pool.

J=(authority integer points, distinct successful targets) is lexicographically ordered.
Only strict improvement accepts the candidate; the entire tuple equal retains original fields and order.
On successful legal runs, J, points and points/5 score never decrease. Coverage may decrease only when points increase;
when points tie, coverage cannot decrease. Invalid proposals fail instead of becoming retained success.

Native evaluation directly checks completed/locked/new_success and non-degradation/tie rules.
It never projects status to success. Raw TLE geometry is a separate local acceptance supplement.

## Portable results and failure codes

run.json explicitly declares native mode/schema. run/source contains the original manifest and exact source bytes.
Business references use safe relative paths. Recheck validates the source snapshot with the original external SHA,
the result file manifest, all three plans, branch/source/objectives, physical changes and solver receipt consistency.
It never reads old absolute paths, invokes a scheduler, regenerates candidates, accesses TLE or uses the network.

0 means all requested stages PASS; 2 means malformed/illegal/tampered/conflicting input or output/path refusal;
3 means missing files, unreadable conditions, budget expiry or process timeout. Unknown exceptions preserve diagnostics
and cannot produce PASS. The workflow may map original child exits 1/7 to 2, while retaining the original codes.
A prevalidated retained file left by a failed solver is an attachment, not a successfully completed run.

## Validation provenance and current submission

The historical R11 tree is `54766909c8f539c8af3cfe733378da7e3aafc347206d904f7b5dbaed6f5a3354`.
It passed 280 historical tests (149 + 103 + 28). R12 added related 60/180-target scale cases;
R13 reused the same 60 targets on two model days. Each real matrix had 8 native workflows,
12 scheduling entry calls, 24 plan checks and 12 raw-TLE geometry reports. These are local aggregate
validation facts, not public real-data replay packages or independent-source replications.

The current submission additionally corrects the parent post-child finalization deadline check.
That change was discovered after R11–R13 and must be validated by this PR's current test/synthetic
receipts; historical 280-test or real-matrix results do not claim to have run this new revision.
The final Git commit and cloud CI result must be taken from the actual PR, not this snapshot digest.

The new guard applies to this native no-fault path; legacy/fault modes retain their own behavior.
There are no claims of continuous-time completeness, full ten-station/six-day/10000-target validation,
independent new TLE sources, historical publication availability, orbital truth, full optical/device
feasibility, field operations or official scoring. R14 is a plan, not an executed result.
