---
name: codebase-index
description: Use before answering repository questions about architecture, implementation, symbols, references, dependencies, refactoring impact, data flow, or bugs. Query the local hybrid index first so the agent reads only evidence-bearing file:line ranges instead of scanning the repository, and verify evidence gathered earlier before relying on it.
allowed-tools: Bash(codebase-index search *), Bash(codebase-index explain *), Bash(codebase-index architecture *), Bash(codebase-index symbol *), Bash(codebase-index refs *), Bash(codebase-index impact *), Bash(codebase-index diff-impact *), Bash(codebase-index path *), Bash(codebase-index describe *), Bash(codebase-index verify *), Bash(codebase-index graph *), Bash(codebase-index stats *), Bash(codebase-index doctor *), Bash(codebase-index update *), Bash(codebase-index index *), Bash(cbx *), Read, Grep, Glob
---

# Codebase Index

Use the local index before reading repository files. It answers with ranked,
numbered lines, so most answers need no Read and no Grep at all. For JS/TS,
`refs` and `impact` resolve imports like the TypeScript compiler: act on them,
since re-checking with grep or scripts only repeats the work.

## Route the question — always `--compact`

| Intent | Command |
|---|---|
| Where / how does X work? | `codebase-index search "X" --compact --session <tag>` |
| A plain-English question worded unlike the code ("stop users spamming the chat") | `codebase-index search "…" --compact --rerank --session <tag>` |
| Overview of a feature | `codebase-index explain "X" --compact --session <tag>` |
| Every caller / call site of X | `codebase-index refs "Owner.member" --compact` |
| What breaks if X changes, all hops (incl. other modules) | `codebase-index impact "<path or Owner.member>" --depth 20 --compact` |
| Find a definition | `codebase-index symbol "X"` |
| A class: members, who uses it | `codebase-index describe "Class" --json` |
| What does my diff affect? | `codebase-index diff-impact --json` |
| Is what I read earlier still true? | `codebase-index verify --session <tag> --json` |

`--rerank` sends the top candidates (redacted) to the project's configured external
re-ranker and reorders them; use it only for plain-English questions, never for
identifiers or error strings. Without a configured re-ranker the header says
`rerank skipped` and the local order stands.

`--compact` prints `path:start-end symbol` per result with the matching lines
numbered underneath (`  24| public static final String FILE_ID = ...`). Cite
those lines as `path:24` directly. Read a range only when the lines shown do not
answer the question — and then only that range, never the whole file.

Name members as `Owner.member` (`TownService.refresh`, `activation::place`,
`CoreError::ObjectDamaged`) so same-named methods of other types are excluded.
`refs --compact` lines are `path:line kind caller -> target  | source line`: the
call sites, their calling function and the line itself are usually the whole answer. Add
`--exclude-tests` for production-only lists and `--path <prefix>` for one module.
`kind reference` is a non-call use, e.g. an enum variant matched in a `match` arm.
For a type-shape change (a new required field), `refs` lists named uses only;
values built through a containing type surface when `tsc` runs after the edit,
so do not crawl `refs` across every container.

`impact --compact` starts with `# module <m>: build files depending on it: ...`
— the answer to "does another module depend on this", with no build-file grep —
then lists each affected file once (`d<hops> path (symbol:line, ...)`).
`# incomplete:` means `--depth` cut the walk short; rerun deeper.

## Protocol

1. Pick one session tag per conversation (e.g. `auth-fix-1`); pass it to
   `search`/`explain`. Results already sent in this session print as
   `(already sent)` — use your earlier copy.
2. Reads refresh files changed since the last update before answering (a
   stderr line says so). A header saying `index stale` means that was off
   (`CBX_NO_AUTO_REFRESH=1`) or another process held the index lock → run
   `codebase-index update` once; `NO INDEX` → `codebase-index index`.
3. Batch independent questions into one Bash call (`cmd1; echo ---; cmd2`).
4. `# partial:` on refs/impact means the list may be incomplete. For
   `Owner.member` it lists `possible_call` sites (calls on a variable the index
   cannot type), nearest first: check those few lines, do not grep the repo.
5. Before relying on evidence from earlier in a long task, run
   `codebase-index verify --session <tag> --json`.
6. Grep only when the index returns nothing relevant or for non-code text.

Edge confidence: `extracted` exact, `inferred` heuristic (receiver matched a
type), `ambiguous` unresolved. Never present an inferred chain as certain.

## Answer

Lead with the answer, then the minimum `file:line` evidence. State uncertainty
only when evidence is partial, inferred or stale.

Options and JSON fields: [references/commands.md](references/commands.md),
[references/response-contract.md](references/response-contract.md),
session memory: [references/memory.md](references/memory.md).
