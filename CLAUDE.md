# sixdegrees / hooconnex — project memory

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

Rules:
- The API token lives in `scripts/plane.env` (gitignored). Read it from there;
  never print it and never commit it.
- Dedupe before create: `read` the issue by name first; update it, don't spawn a
  duplicate.
- Post findings as a comment on the parent issue, not as new issues.
- Mark the issue `completed` only after the work is done and verified.
