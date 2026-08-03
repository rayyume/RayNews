# Security Hardening Plan Rewrite Design

## Goal

Rewrite `docs/plans/security-hardening-fix-plan.md` in place so an agent can execute T1–T22 without reproducing the contradictions, security gaps, and deployment failures found during review.

The rewrite changes documentation only. It does not implement any application or deployment fix.

## Document Shape

The rewritten plan remains a single document and preserves the existing T1–T22 identifiers so review findings and future commits remain traceable. Task execution order may differ from numeric order when a prerequisite must land first; the summary section will state the authoritative order.

Each task will contain:

1. the verified current-code problem;
2. exact files and symbol names rather than fragile fixed line ranges;
3. prerequisites and interfaces consumed from earlier tasks;
4. a failing-test step;
5. the minimum implementation needed to pass;
6. targeted verification and full-suite verification;
7. one intentional commit boundary.

## Required Corrections

### Application security

- T1 will preflight duplicate email as well as invitation and nickname, retain transactional revalidation, and define cleanup for stale registration-attempt rows.
- T2 will use `safe_get(..., stream=True)`, bound the streamed response before decoding, close every acquired response, and align tests with the fetch functions' `None`-on-failure contract.
- T3 will reuse the existing notification Markdown sanitizer and test the complete daily-summary send path.
- T4 will redact the exact configured API key before generic pattern redaction and use the safe message for health state, logs, notifications, and persisted AI error fields.
- T5 will combine request-body limits with post-parse type and field-length validation.
- T8 will be described as defense in depth because the current nginx surface does not expose `/scheduler/status`.
- T9 will be described as input validation, not as prevention of delivery to arbitrary valid third-party addresses.
- T15 will assign CORS ownership to one layer, neutralize Flask-CORS headers when nginx owns policy, account for nginx `add_header` inheritance, and use fully anchored Origin matching.
- T16 will remain optional unless a supported deployment actually places a non-loopback trusted proxy directly in front of Flask.
- T17 will not claim role-demotion protection because current authorization already reads the role from the database; token versioning must have a real rotation trigger or be deferred.
- T22 will inspect at least 12 bytes from `raw_bytes`, allowing correct WebP validation.

### Data and resource reliability

- T6 will use an atomic conditional UPDATE for access throttling, a module-level prune lock, the DELETE cursor's affected-row count, and update its throttle timestamp only after successful commit.
- T7 will convert only one-shot `news.db` connections; `_get_news_db`'s thread-local persistent connection is explicitly excluded.
- T10 will preserve explicit global and per-user alias choices instead of ambiguously deleting alias rows without a status field.
- T11 will parse `link_preview_url` as a URL instead of interpolating untrusted text into synthetic HTML.
- T12 will detect the actual allowed image type from magic bytes and reject non-image bodies; known-invalid bodies will never be returned with HTTP 200.
- T13 will preserve `original_body_html` with conflict-update semantics and test that omitted columns are not reset by replacement.
- T14 will avoid full-row image scans and account for the snapshots already performed by `_run_refresh_job`.
- T21 will precede T12, close streamed responses with a safe finally/closing pattern, and key initialization state by cache database path with recovery after deletion.

### Deployment

- T18 will forward supervisor child stdout/stderr to container logs and define health behavior when a child enters a fatal state.
- T19 will use one coherent privilege model: supervisor may start as root, Python children run as `raynews`, nginx retains only the privileges it needs, and bind-mounted data-directory ownership is handled at runtime. It will not combine Dockerfile `USER raynews` with `su raynews`.
- T20 will use a complete reproducible lock artifact, including transitive dependencies, rather than presenting seven pinned top-level packages as a full lock.

## Ordering

The authoritative execution order will place shared prerequisites before their consumers:

1. request and outbound-network controls;
2. sanitization and error redaction;
3. resource-lifecycle primitives such as streamed-response closing;
4. image validation;
5. database reliability tasks;
6. CORS and reverse-proxy policy;
7. process supervision and privilege separation;
8. dependency locking and remaining low-risk validation.

T identifiers remain unchanged even when execution order differs.

## Testing Policy

The current verified baseline is 707 passing tests. New tests increase the total, so acceptance will require at least 707 collected tests and zero failures, not an exact fixed count.

Every task follows this sequence:

1. add the focused failing test;
2. run it and confirm failure for the intended reason;
3. implement the smallest fix;
4. run focused tests;
5. run `python3 -m pytest tests/ -q`;
6. commit only that task's files.

Deployment tasks additionally require image build, configuration syntax checks, process-user inspection, restart behavior, response-header checks on both static and proxied routes, and writable bind-mount verification.

## Scope Controls

- Fixed line numbers are advisory only; symbols, routes, SQL tables, and configuration blocks define scope.
- Tests and documentation required by a task are included in that task's file scope.
- The rewrite will not silently expand deferred product decisions such as restricting notification recipients to the account email, implementing complete CSP, or designing shared-AI content provenance.
- No application code is changed while rewriting the plan.

## Completion Criteria

The rewritten plan is complete when:

- T1–T22 are all present exactly once;
- no task contains mutually exclusive implementation instructions;
- all task dependencies agree with the final execution-order summary;
- no code example references an undefined variable or an interface contradicted by current code;
- tests cover the stated security property rather than only implementation details;
- placeholder and ambiguity scans find no unfinished markers, delegated design decisions, or unresolved alternatives.
