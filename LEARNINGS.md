# Learnings

Append-only log of corrections and rules accumulated during Claude sessions.
Newest entries at top. Read at session start.

Format:
## YYYY-MM-DD — [area, e.g. schema, deploy, testing, ux]
**Mistake:** what I did
**Correction:** what should have happened
**Rule:** generalized rule going forward

---

## 2026-05-04 — tooling
**Mistake:** `chore/learnings-protocol` was branched off `feat/foundation-quote-v1` instead of `main`. PR #7 therefore fan-out-merged Foundation Quote v1 + B2.3 Phase 3 + migrations 017–021 into main alongside the 2 chore commits — 35 commits total instead of 2.
**Correction:** Branch chore/docs/tooling work off `main`, never off a feature branch. The chore work has nothing to do with the feature, so the PR scope should not include feature work either.
**Rule:** For any chore/docs/tooling branch, verify base resolves to `origin/main` before opening the PR. Diagnostic: `git log --oneline origin/main..HEAD` should show only the chore commits. If feature commits appear, abort and rebase onto `origin/main` before continuing.

## 2026-05-04 — tooling
**Mistake:** Commit message was passed through with paste artifacts (autolinked filenames as `[X.md](http://X.md)`) despite explicit instructions to use plain text. Reached production main on legalai-api.
**Correction:** When relaying commit messages to Mac CC, the message must be plain text only — no markdown, no backticks, no filename autolinks. Commit subjects render in raw text in git log and GitHub UI.
**Rule:** Commit messages: plain text always. Verify the literal string that will be passed to `git commit -m` before executing.
