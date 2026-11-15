# AGENTS.md — sixdegrees / hooconnex

## Plane board (source of truth for work items)

Work items live on John's self-hosted Plane board — workspace `my-projects`,
project **Hooconnex** (identifier `HOO`). Read and update it through the repo
helper, never raw curl:

```bash
python3 scripts/plane_board.py list
python3 scripts/plane_board.py read <substring-of-issue-name>
python3 scripts/plane_board.py create --name "..." --description "..." --priority high|medium|low
python3 scripts/plane_board.py comment <substring> --body "..."
python3 scripts/plane_board.py update <substring> --state unstarted|started|completed
```

## How tasks are assigned to you (Codex)

An issue whose title starts with `Codex:` (or whose description says "Assigned
to: Codex") is your work. On start, move it to `started`. Post progress and
findings as comments on the issue. When done and verified, move it to
`completed`.

## Rules

- The API token lives in `scripts/plane.env` (gitignored). Read it from there;
  never print it and never commit it.
- Dedupe before create: `read` the issue by name first; update it, don't spawn a
  duplicate.
- Post findings as a comment on the parent issue, not as new issues.
- Every factual claim in a deliverable carries a cited source; mark unknowns
  explicitly rather than guessing.

## Sandbox note

`codex exec --sandbox workspace-write` blocks all network including localhost, so
board writes fail silently. When a run must read or write the board (or fetch
live data), launch with `--sandbox danger-full-access`.
