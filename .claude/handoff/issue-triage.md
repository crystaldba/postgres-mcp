# Issue triage handoff (crystaldba/postgres-mcp)

Snapshot taken 2026-11-25 from GitHub. 40 open issues at the time (newest #199, filed 2026-08-14). Re-fetch before acting; state may have changed.

## Goal

Build a validation-first flow for issues:
1. **Validate** a report by reproducing it against the real code. Do not trust the reporter's claim.
2. **Fix** only validated issues.
3. Treat community submissions, including PRs, as **fix requests**, not as fixes. A submitted PR is a proposed patch that goes through validation like anything else.
4. Multi-model fix stage: several independent subagents each write a candidate patch blind to each other. Independent verifiers then judge the candidates against the validated repro and check for regressions. The winner is picked or synthesized.

Pipeline:

```
report/PR -> VALIDATE -> (real) -> FIX x N independent agents -> JUDGE/COMPARE -> synthesized patch
                |
                +-> (not reproducible) -> request info / close, never reaches fix stage
```

## Priority field

The repo has an org-level `Priority` issue field (Urgent/High/Medium/Low) and `Effort`. None of the 40 open issues has a value set. Only #138 (`enhancement`) and #136 (`bug`) carry labels.

## Validation status

**Independently validated (reproducible, strong evidence)**
- #178: restricted-mode bypass. `SELECT * FROM pg_read_file('/etc/passwd')` passes because `RangeFunction` nodes in `postgres_mcp/sql/safe_sql.py` are never checked against `ALLOWED_FUNCTIONS`. Has a PoC and line references. **PR #200 is already open and claims to fix it. Under the policy above it must be validated, not trusted.**
- #194: stdio transport does not exit on stdin EOF, so sessions leak. Repro: pipe a 3-line jsonl probe into `docker run -i ... --access-mode=restricted`. Reporter measured 159 leaked processes in 12h. A different root cause from #175.
- #175: Docker orphaned containers on macOS. Two independent confirmations (macOS, Ubuntu). May share a root cause with #194.
- #109: `analyze_db_health` mis-parses quoted identifiers. Reporter located `_parse_sequence_name` in `sequence_health_calc.py` and proposed a fix (strip quotes before `Identifier()`).
- #91: "unhandled errors in a TaskGroup" on stdio. Full traceback, also reproduces on SSE.

**Maintainer-acknowledged, not confirmed**
- #136: connection lost after network change. jssmith said "will investigate".

**Stalled / not validated**
- #127: maintainer asked for repro steps, none given.
- #63: clarifying questions unanswered.
- #65: Cursor remote-SSH client limitation, not a project bug.

**Feature/design threads (validation = consensus, not repro)**
- #71 table/column comments: maintainer approved, a community member offered to prototype.
- #138 access-modes RFC: commenter suggests per-request constraints instead of fixed modes.
- #164 / #105 default-UNRESTRICTED posture: policy question, partly answered (restricted mode already blocks writes).
- #99, #141, #162, #171, #174, #125: config/packaging requests. #141 and #162 are stale-image/release pressure.

**24 issues have zero comments**: untriaged, neither validated nor invalidated.

## Suggested priority (my read, not applied anywhere)
- Urgent: #178 (and #164, which is related)
- High: #194, #175, #181, #98
- Medium: #162/#141/#174 (stale image and package), #192 (project status), #138, #153, #167, #71
- Low: docs and environment-specific connection errors

## Open decisions (the question was never answered)
1. **Scope**: a reusable Workflow script under `.claude/workflows/` plus a first run on #178, #194, #175, #109, #91 (recommended), or a one-off pilot only?
2. **Fix output**: diff/proposal only, shown for review (recommended), or push winning patches as draft PRs?

## Next steps
- Get answers to the two decisions above.
- Load the `workflow-authoring` skill, then write the validate -> fix xN -> judge script.
- Run validation on #200 first. It is the clearest test of the "fix request, not a fix" rule.
- Check #175 and #194 for a shared root cause before fixing them separately.
- Nothing has been changed in the repo, on GitHub issues, or in PRs beyond this note.
