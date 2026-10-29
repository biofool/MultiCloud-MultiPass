# Coding Patterns & Conventions

## File size & responsibility

- **One responsibility per file.** Split when a file has multiple reasons to
  change. Each file should be independently understandable, narrow-purpose,
  and need no hidden context.
- **Keep the public surface explicit.** Obvious imports, declared types, and
  clear module boundaries. Do not rely on implicit state or dynamic wiring
  the reader cannot see.
- **Target ~150–400 lines per source file.** 400 lines is a soft refactor
  threshold — a review trigger, not a law. At 300–400 lines, ask whether the
  file should be divided. Do not mechanically fragment a coherent module at
  401 lines.
- **Optimize the task, not only the file.** Right context beats minimal
  tokens. Terse context forces extra tool calls; a file should carry what a
  reader (human or agent) needs to work on it.
- **Pair small files with strong tests and a concise architecture map.**
  Small files multiply indirection; tests and the map keep the system
  legible.
- **Prefer diff-based targeted edits over full-file rewrites** for large
  files. Rewrites lose review history and introduce silent regressions.

### Audit findings

See [../debt/filesize-audit.md](../debt/filesize-audit.md) for the dev-branch
audit of files over 400 lines, with per-file responsibility counts and
refactor priorities.
