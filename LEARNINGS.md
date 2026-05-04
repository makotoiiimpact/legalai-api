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
**Mistake:** Commit message was passed through with paste artifacts (autolinked filenames as `[X.md](http://X.md)`) despite explicit instructions to use plain text. Reached production main on legalai-api.
**Correction:** When relaying commit messages to Mac CC, the message must be plain text only — no markdown, no backticks, no filename autolinks. Commit subjects render in raw text in git log and GitHub UI.
**Rule:** Commit messages: plain text always. Verify the literal string that will be passed to `git commit -m` before executing.
