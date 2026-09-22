# Actual PageIndex baseline adapter

This research runner executes pinned upstream PageIndex code. It is separate from `scripts/pageindex_eval.py`, which measures GPTgrep against an upstream corpus subset without running PageIndex.

Maintainer research notes are local-only. The executable profiles, source locks, receipts and interpretation rules below are the public contract.

## Setup

Use an existing Python3.13 interpreter to create a task-local environment, then install `requirements.lock`. No global toolchain change is needed:

```sh
uv venv --python /path/to/existing/python3.13 .local/pageindex-baseline/venv
UV_CACHE_DIR=.local/pageindex-baseline/cache uv pip install \
  --python .local/pageindex-baseline/venv/bin/python \
  -r scripts/pageindex_baseline/requirements.lock
```

Provide read-only checkouts at the exact revisions in `sources.lock.json`. For judging, place the three locked MMLongBench-Doc-V2 files (`eval/judge.py`, `eval/metrics.py`, `LICENSE.md`) in a private source directory at the pinned commit. The runner reads the original judge constants through AST literals; it does not invoke the upstream key-based client or its error-to-false retry behavior. Source code and PDFs are not vendored into this package.

Every runtime verifies source files, all34 PDF hashes, question/metadata hashes, and exact dependency versions before importing PageIndex. Upstream dotenv loading and bytecode writes are disabled. API-key fallbacks are removed from the child environment.

## Role profiles

`--profile matched-luna-max` is the corrected primary matched-reader configuration: index Luna/medium, reader Luna/max, shared judge Luna/high. Upstream PageIndex omits index effort; the adapter explicitly selects the [documented Luna API default, medium](https://developers.openai.com/api/docs/models/gpt-5.6-luna). Codex transport remains an adaptation rather than native-provider omission semantics. `source-default-chat` uses index Luna/medium, reader Luna/high and judge Luna/high as a separate replay, not a historical reproduction. `legacy-luna-max-control` preserves the earlier all-max role profile; select `--index-host-concurrency 1 --reader-concurrency 1 --judge-concurrency 1` explicitly to reproduce its serial scheduling policy. Previously sealed controls remain unchanged.

Existing `--model` and `--reasoning-effort` select the reader. `--index-model` / `--index-reasoning-effort` select indexing; `--judge-model` / `--judge-reasoning-effort` select judging. The upstream index effort remains recorded as unspecified, separately from the documented API default and actual executed setting. GPTgrep's product-helper default remains max. All roles share one cumulative invocation cap and append phase/model/effort receipts. Each call snapshots its model, effort, phase and schema; concurrent indexing never temporarily changes shared reader/judge settings. Capability admission binds the reader profile; changing the judge does not claim a new reader capability.

`--service-tier fast` is the default for every role and is part of profile/cache identity. Receipts keep requested and effective tiers separate. Codex may acknowledge `priority`, which is an equivalent alias for requested `fast`; the actual acknowledgement is retained verbatim. An absent acknowledgement remains null, and an observed non-equivalent tier fails the call. This is configuration acknowledgement, not a claim about provider billing. Select the authenticated account frontend and home through generic runtime parameters; private aliases, account-cell names and machine paths do not belong in public source or documentation.

`--index-host-concurrency` is an explicit owned-host upper ceiling from1 to64, default64. The original SDK retains its summary semaphore of64, expansion semaphore of32, and parent-after-child dependencies. A dedicated executor prevents Python's smaller default thread pool from silently imposing another index limit. `--reader-concurrency` and `--judge-concurrency` each default to5 and accept1..64. All selected reader cases finish and drain before a separate judge pool starts; the stages share the same cumulative invocation budget. Each case owns its PageIndex client, Responses transport and fixed role/phase bindings while reading the completed SDK store. Documents remain indexed serially in both the source benchmark and this runner. These controls match the declared comparison schedule; they do not establish original-provider throughput parity.

The ceiling is not a claim of achieved concurrency or speedup. Receipts record actual owned-process start/end times, phase, process ID and active counts. Per-index and per-case records, final summaries and `host-concurrency.json` report measured peak overlap, interval counts and busy/summed process time. `task-intervals.json` and `task-concurrency.json` separately record actual case-task intervals, including cache checks and SDK work; they do not estimate provider concurrency. Reader and judge pools have separate task measurements. The total simultaneous host ceiling is the maximum selected role ceiling, and QA stages do not overlap. Missing or interrupted intervals remain explicitly unavailable. These measurements concern local host processes, not unobservable provider requests. Resource demand depends on the actual ready frontier and each child's memory use; per-call input/output/time bounds and the total invocation cap still apply at every configured concurrency.

Duration totals are null if any contributing invocation lacks a duration; known subtotals and missing counts are separate. Outer invocation timing and inner reported host timing are different fields. Summed concurrent durations are not elapsed benchmark wall time and are not converted into inferred speedups.

Each QA pool writes immutable start/final attempt checkpoints under `qa-stages/`. They retain that invocation's wall duration, configured limit, new/reused/unavailable task counts, task outcomes and queued work that never started. `qa-stage-timings.json` keeps all prior stages and reports cumulative known wall-time subtotals and missing final durations separately. The latest resumed stage can consist largely of cache checks; its duration is never presented as a cold full-cohort measurement. Host-process overlap remains a separate observation from case-task overlap.

The declared development membership comes only from the provenance reference manifest; there are no task-row lists in executable code. `--rows all` is the default. Explicit `--rows dev8` loads that existing development split; the remaining rows remain held out. Questions, gold answers and annotations are loaded at runtime. Only the original question and protocol-provided document scope enter the reader; gold/annotations stay with the judge and scorer.

## Stages

Run the live full benchmark directly. Qualification comes from an actual original SDK page-tool roundtrip within that run: a native model selects the declared operation, the SDK returns source-verified page text, and a subsequent completed native turn receives that exact output. Native model/effort, session identity, request/response hashes and source extraction hashes bind the proof. Process completion or an answer string alone does not qualify the transport. No additional live synthetic probe is required or recommended.

Every selected task remains in the denominator, including failures and zero-page answers before the first successful roundtrip. If the full run never demonstrates that ability, the baseline is inadmissible; raw judge results remain visible. Historical capability receipts may be recorded as supporting evidence with `--capability-receipt`, but never qualify a changed run or replace actual benchmark evidence.

All invocations require explicit source paths, a private run directory and the selected Codex account home. The acceptance run uses `--rows all` for the complete62-task,34-document scope.

```sh
.local/pageindex-baseline/venv/bin/python -B scripts/pageindex_baseline/run.py \
  --stage plan --variant full --rows all --profile matched-luna-max \
  --index-host-concurrency 64 --reader-concurrency 5 --judge-concurrency 5 \
  --upstream /path/to/PageIndex \
  --benchmark /path/to/PageIndex-OSS-Benchmark \
  --judge-source /private/path/to/judge-source \
  --run-dir .local/runs/full-benchmark \
  --service-tier fast --codex-bin /path/to/account-frontend \
  --codex-home /path/to/codex-home
```

- `plan`: verifies and records identities; no model calls.
- `index --variant raw`: actual Flash structure extraction with summaries/optimization disabled, through the SDK's local storage contract; no model calls.
- `index --variant full`: original SDK Flash index, including full optimization, summaries and document description, using the supported custom provider.
- `answer`: original SDK `chat(protocol="responses")`, instructions, tool schemas and Agents Runner, through the injected local Responses transport.
- `judge`: original judge prompt/schema and response-character cap, using the selected local completion profile.
- `run`: index, answer and judge for the selected variants.
- `--variant gptgrep-tree`: common-runtime control using actual GPTgrep parser/tree output but the same SDK reading text, tools, runner and completion transport. It excludes GPTgrep regex/lexical/Jev retrieval.

Model stages require a positive `--max-model-calls` value chosen for the run. The counter bounds local host invocations, not an unobservable count of provider-internal requests. Its default is0. The host uses `gptgrep host-complete`, preserves exact requested model/effort, and validates actual identity and output schema. The input default is256KiB and hard maximum1MiB; oversized content is rejected without truncation.

The native product comparator is `scripts/gptgrep_system_eval.py`: it builds an actual PDF-only private corpus using native LiteParse/Rust trees/tgrep, then invokes native `ask --document` with mandatory Jev routing/reranking and the same reader/judge roles. Its build is explicitly deterministic; it does not invent a Jev indexing stage. It preserves per-case raw output, provider failures, partial Jev accounting, source/citation byte integrity, page recall, tool signals and latency including Jev. Plans make no model calls; live runs require an explicit cap. The same bound `--stage run` and run directory resume retained work; failed retries require `--retry-failed`, and completed judged answers are retained even when wrong. Immutable attempts and all known cumulative usage remain available. The optional baseline summary is loaded only after native answering/judging completes.

First-release acceptance requires a verified live advantage under the declared full-cohort comparison contract. A successful process, capability probe, control smoke or development-only score cannot authorize a first experimental release. No evaluator emits an automatic universal-winner verdict.

Index, answer and judge stages share a private run ledger and cumulative invocation cap. Brief locks reserve unique ordinals, check the cap, and fsync start/final journals; no lock covers an entire model call. In-flight reservations count toward the cap. Final receipts may arrive out of order; their append history is retained, while in-memory/resumed views are ordered by invocation ID. A per-case reservation callback durably records the exact ordinal before process launch. QA attribution uses the case's immutable attempt phase and exact ordinals, never a slice of the shared completion list. Native asks use the same atomic reservation and process-cleanup boundary; their distinct output validation remains in the native runner. Recovery reconciles started requests and final receipts, including missing middle receipts, so uncertain attempts cannot become free replays. Completed caches bind source, adapter, dependency, all role profiles, concurrency/time/byte bounds and binary hashes. A later stage can increase the cumulative cap without erasing previous calls. Filesystem locking prevents two runners from mutating one run directory simultaneously. Freeze the adapter files and binary before the first live stage; changed concurrency/profile/source requires a new bound run.

Use the same arguments and run directory with `--stage index`, then `--stage answer`, then `--stage judge`, or use `--stage run` for all three. A budget-deferred item resumes without redoing completed work. `--retry-failed` explicitly retries failed/interrupted items, retaining each prior attempt under `attempts/` and every earlier host call in the ledger. Each invocation has a durable start journal; an interruption without a final receipt consumes its ordinal and budget conservatively, with unknown usage. Failed attempts are never erased or silently treated as free. Summary files are current projections; immutable stage plans, attempt files and raw call files preserve the full history.

Before a judge retry, the runner reconciles the saved invocation against its exact request/response bytes, source-case identity, full reader response, rubric, schema, model/effort, service tier and native identity. A durable completed verdict is restored without another call, including a false verdict; mismatched evidence blocks replacement. A completed reader final response without its complete SDK case checkpoint is also protected from replacement. The runner conservatively stops that case rather than reconstructing missing SDK state or claiming it recovered the answer.

Index admission preserves the original SDK retry policy. After the SDK returns its tree and pages, every failed invocation must either remain blocking or have a later validated success for the exact request bytes, schema, model, effort and tier. Recovery accepts classified service failures or fully reaped host timeouts; interruptions, identity/schema failures and input rejections remain blocking. The later owned process must start after the failed process ends in the same measurement session. All failed calls, their latency and missing usage remain in the ledger. This establishes request-level resolution plus SDK completion, not proof that each downstream node consumed a particular response or that its summary is correct.

For a frozen run rejected only by the earlier blanket failed-call guard, `reconcile_index.py` provides a zero-model preview. It verifies the original plan digest, frozen adapter/binary, source/dependency pins, exact document attempt and every request/response binding. It requires one unambiguous SDK document, valid tree/page bounds and equality between its stored pages and a fresh pinned PyPDF2 extraction of the locked original PDF. The SDK store contains no raw PDF copy. Input-limit failures, unresolved calls, redirected evidence and multiple candidate artifacts are rejected. Preview avoids both `LocalCodex` construction and SDK manifest-refresh methods.

```sh
.local/pageindex-baseline/venv/bin/python -B scripts/pageindex_baseline/reconcile_index.py \
  --run-dir /private/original-run --plan-sha256 ORIGINAL_PLAN_SHA256 \
  --source source.pdf --frozen-adapter-dir /private/seal/runner/scripts/pageindex_baseline \
  --binary /private/seal/gptgrep --upstream /path/to/PageIndex \
  --benchmark /path/to/PageIndex-OSS-Benchmark --judge-source /private/judge-source
```

After the run drains and the preview has been reviewed, the same command with `--apply --expected-preview-sha256 REVIEWED_PREVIEW_SHA256` takes both existing owner/store locks and revalidates the preview. It appends an immutable recovery receipt and a new index attempt, then updates the current index projection. It preserves the frozen source/plan, original attempts, raw calls and original index latency. Any changed evidence requires a fresh preview. The frozen runner can subsequently consume the corrected ready index without rerunning completed QA or judgments. Changing input bounds or inference settings instead requires a new declared experiment; reconciliation does not rewrite an old plan.

For a new declared experiment, `--index-origin-run`, `--index-origin-plan-sha256`, `--index-origin-frozen-adapter-dir` and `--index-origin-binary` enable preparation reuse. Select these parameters and any larger `--max-input-bytes` bound before new-run QA outcomes. The supported input ceiling is1MiB; reuse requires a nondecreasing bound and identical source/cohort, SDK/dependencies, role profiles, service tier, host binary, transport helpers, Python version, SDK turn budget, QA stage order, timeout/output limits and role concurrency. The origin must be quiescent and stays read-locked during use. Nested preparation origins require separate lineage admission and are currently rejected.

Completed origin indexes are independently revalidated against their raw calls and source-bound SDK artifacts, then copied into the new private store. The new index projection records zero new model calls, null fresh-build wall time, local import time and the original index time separately. `index-origin.json` binds one unique historical ledger lineage, including failed attempts, missing usage and unfinished start journals. Historical preparation is never presented as free work.

Incomplete documents run the original SDK index pipeline under `IndexReplayHost`. An exact request occurrence can consume one validated successful origin response; different request/schema/profile bindings and exhausted or ambiguous producers become live misses. Conflicting response values are not ranked or selected for quality. A separate durable reuse ledger records producer IDs and supports idempotent redelivery of the same occurrence after interruption. Reuse creates no new host-call receipt or billed-call claim. The original SDK remains responsible for retry scheduling and index completion; the adapter adds no model retry layer. These preparation wrappers never load or cache QA/judge responses. By default, all62 QA and judge cases execute independently in the new run.

An explicitly declared recovery can additionally select `--carry-completed-outcomes`, which defaults off and requires `--variant full`, the four origin parameters, and the pinned judge source. This retains each runtime row's first immutable completed reader-and-judge pair, including false verdicts, abstentions and zero-page answers. Selection never depends on correctness or page access. Rows without started or completed reader/judge components use the ordinary fresh path. Partial components, missing immutable checkpoints or mismatched evidence block replacement; `--retry-failed` does not bypass this boundary.

Before any consumer QA begins, the retention validator checks the exact raw-document and imported SDK document/tree/page bytes, cohort and question binding, frozen reader/judge functions, SDK/dependencies, Python, model/effort/tier, timeout/turn/concurrency controls and stage order. Only the declared nondecreasing input-admission bound may differ: every retained successful request must fit both bounds, and the native model payload remains exactly instructions/state/schema. It reconstructs the pinned SDK wire requests, binds each decision to the immutable SDK transcript, rechecks source-page qualification, and validates the original judge rubric/schema against the original reader identity. This validation constructs no origin host and invokes no model.

`outcome-origin.json` records the admitted row split and evidence hashes without question, answer or gold text. The original producer checkpoint is copied byte-for-byte into `outcome-origin/cases/`; it keeps its original native identities and ordinals. Consumer projections identify the origin separately, expose zero new reader/judge calls and null fresh-case latency, and record the changed consumer cache identity without relabelling original calls. Pools report carried tasks separately from fresh attempts. The full cohort denominator remains unchanged. `completed_outcome_carry_forward` counts the unique historical ledger once alongside current host calls, preserving failed attempts and missing usage; preparation replay and retained outcomes never multiply historical costs. Its timing is explicitly historical outcomes plus recovery, with no cold full-run elapsed-time claim.

Each completion requests a private `calls/NNNNN.trace.jsonl`. The native trace contains bounded method metadata, not prompts, credentials or configuration; its Rust limit is64KiB/256 events. The adapter records optional presence, size and digest without interpreting trace contents. Failed identity/schema checks still retain any usage actually reported. Structured host protocol failures retain only allowlisted error kinds, retry flags/counts, HTTP status and observed numeric token fields. Failed accounting stays incomplete; absent usage stays null. Server retry notifications are recorded on success too, but are not billed-request counts. Error messages and arbitrary metadata are excluded from the projected protocol receipt. Timeout, cancellation and dispatcher interruption request TERM for each owned process group, allow the existing native cleanup grace, and reap it before a forced group kill if required. No global process-name kill is used. Async cancellation drains final receipts before another document advances the phase.

## Interpretation

The transport represents tool decisions as schema JSON returned by local Codex, then lets the real SDK executor run the original tools. That preserves the original tool declarations as data and the SDK execution loop, but is an explicit model/tool-interface adaptation. It is not a native-tool parity claim. Every completion currently creates a fresh host thread, so native GPTgrep versus adapted SDK wall time includes architecture differences. Use the common-runtime tree control to isolate that confound.

Protocol v3 explicitly distinguishes internal `tool_calls` data from native execution and from user-facing `text`. The original PageIndex instructions remain byte-for-byte within the wrapper; original, adapter and combined instruction hashes are separate. The benchmark-native qualification is global to a variant and does not depend on answer correctness or gold evidence pages. Every question remains in the QA denominator, including abstentions and zero-page answers. Correct answers from supplied descriptions or summaries retain judge credit while page recall stays zero. Evidence delivery is labelled `raw_pages`, `index_summary_or_metadata`, or `none`; it never automatically verifies a citation. Missing judge verdicts remain unknown and block eligible comparison scores.

Private artifacts include original source-bound trees, response transcripts, request/response hashes, exact native host receipts and phase counts. Terminal output is bounded status metadata. SDK aggregate usage is not authoritative when the transport has no equivalent complete usage object; actual host usage remains in `host-calls.jsonl`. Account-backed monetary billing is unavailable.

Successful page access is counted from tool results, not requested page numbers. Judge failures remain unavailable. A completed raw index or a passing adapter test is not a completed paired baseline, and a paired baseline is not by itself a GPTgrep win.

## Adapter checks

```sh
.local/pageindex-baseline/venv/bin/python -B -m unittest discover \
  -s scripts/pageindex_baseline -p 'test_*.py'
```

The tests use synthetic model decisions and a real local tool invocation through the actual Agents SDK to verify plumbing. Concurrency regressions cover distinct roles/schemas, cumulative cap races, out-of-order recovery, partial-failure usage, external native asks followed by judges, and cancellation of owned children. Staged QA tests run five synthetic readers then five judges, verify per-case ordinal/phase attribution, and resume completed wrong verdicts without another inference. They also cover concurrent native reservations and exact pre-launch judge callbacks. An actual LiteLLM async dispatch test holds20 synthetic process boundaries open while the default executor is restricted to2 workers, proving that index dispatch uses the dedicated executor. These tests make zero live model calls and produce no benchmark score or real-inference speed measurement. Actual model-backed index/answer/judge results must come from separately bounded runs.
