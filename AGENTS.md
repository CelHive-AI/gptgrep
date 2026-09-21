# GPTgrep

Own the authorized task through implementation, proportionate validation and delivery.
Use SPEC.md for the project contract and aicatlog-manifest.json for resource routing.
Read only applicable workflow topics. Preserve unrelated work.

Use workspace/harness-config for portable specs and templates, and workspace/harness-tooling for execution entrypoints.
Task results and handoffs belong in their declared local workspace locations.
Do not treat a checkpoint, successful process or generated artifact as acceptance.

The project mandate authorizes routine development, tests, CI, releases and delivery.
Reserve major requirement, product/domain and research changes for Human discussion.
Use the smallest useful team; independent review is required before release.
The first experimental release additionally requires the accepted live PageIndex
comparison gate in SPEC.md; source delivery and CI alone do not authorize its tag.
Read docs/architecture.md for module boundaries and SPEC.md for acceptance.
Never modify the upstream reference checkouts. Pin reused source and retain licenses.
Never hardcode benchmark questions, answers, document IDs, evidence pages or
task-specific routing rules in product code or adapters. Load evaluation tasks at
runtime; gold answers and evidence annotations belong only to judging/scoring.
Do not encode benchmark-specific document/question patterns, answer-location
priors or few-shot task examples in prompts, indexes, caches or selection rules.
Build indexes from raw documents alone; keep judge state out of reader sessions.
Keep credentials in the process environment; never commit credentials or private corpora.
Public artifacts must not contain private session identities or machine paths.
Private evidence and exact native provenance belong under .local/ or workspace/handoff/.
Keep docs/research/ local-only, ignored and untracked; preserve its files locally.
Keep workspace/exec-plans/ and workspace/issues/ local-only, ignored and untracked too.
Every completed code round requires a commit and verified refs/notes/commits PoUW.
Back up Git history and notes to a local verified bundle under .local/backups/;
push source and tags to the authorized origin when release checks pass. Do not push
private PoUW notes or runtime evidence. No global configuration, toolchain or service
changes are implied by project ownership.

Run cargo fmt --all -- --check, cargo test --workspace, cargo clippy --workspace
--all-targets -- -D warnings, the offline eval harness, and the applicable wrapper
tests before release. Live provider evaluation is explicit and records actual model,
request count, available usage/cost, latency and failures without storing credentials.
