# External re-ranking (`--rerank`, TypeSafe)

`search --rerank` and `explain --rerank` reorder the top local candidates with an external
model before the page is returned. They exist in the `0xCatalyst/codebase-index` fork only;
upstream `codebase-index` on PyPI does not have them.

Local retrieval matches words, symbols and graph links. It finds identifiers and error strings
well, but a plain-English question worded unlike the code ("stop users from spamming the chat
endpoint") can leave the answer outside the first page. The re-ranker asks
[TypeSafe System One](https://docs.typesafe.ai/introduction) one yes/no question per candidate,
"is this where the behavior asked about is implemented, configured or decided?", and reorders
the candidates by the probability it returns.

Measured on a 1,842-file Next.js/TypeScript repository, 34 questions with known answers:

| | Local | `--rerank` |
|---|---|---|
| Plain English (20): answer in top 5 | 8 | 14 |
| Broken English (14): answer in top 5 | 5 | 8 |
| Answer ranked first (34) | 5 | 10 |
| Time per search | 0.5 s | 1.8 s |

Use it for plain-English questions only. Identifier and error-string lookups are already
answered locally, faster and without sending anything.

## 1. Install this fork

The fork is not published to PyPI. Install it with [pipx](https://pipx.pypa.io/):

```bash
pipx install "git+https://github.com/0xCatalyst/codebase-index.git"
```

or from a local clone:

```bash
git clone https://github.com/0xCatalyst/codebase-index.git
pipx install ./codebase-index
```

`codebase-index --help` lists `--rerank` under `search` when the fork is installed. The
re-ranker needs no extra Python packages; it uses the standard library HTTP client.

## 2. Get a TypeSafe API key

Create an account and a key at <https://console.typesafe.ai/keys>. Usage is billed per input
token (output tokens are free; see [models and pricing](https://docs.typesafe.ai/models.md)).

## 3. Store the key

Either way works; the key is read at query time and is only ever placed in the
`Authorization` header.

**A key file** (works for agents, whose shells may not load your profile):

```bash
mkdir -p ~/.config/typesafe && chmod 700 ~/.config/typesafe
read -rsp 'TypeSafe API key: ' k; echo; printf '%s' "$k" > ~/.config/typesafe/api_key; unset k
chmod 600 ~/.config/typesafe/api_key
```

Run it in your own terminal. Do not paste the key into an agent chat (it would be stored in
the transcript) and never commit it.

**An environment variable** `TYPESAFE_API_KEY`, exported from your shell profile. When it is set
it takes precedence over the key file.

## 4. Enable it per project

Re-ranking sends repository text off the machine, so it is off until a project opts in. Add a
`rerank` section to the project's `.claude/cache/codebase-index/config.json` (the cache
directory is gitignored, so this stays on your machine):

```json
{
  "rerank": {
    "backend": "typesafe",
    "allow_external": true,
    "api_key_file": "~/.config/typesafe/api_key"
  }
}
```

Leave out `api_key_file` when you use `TYPESAFE_API_KEY`. The setting is query-time only; it
does not rebuild the index.

## 5. Verify

```bash
codebase-index doctor
# OK [medium] rerank_external: EXTERNAL RERANK ENABLED: `search --rerank` sends the top 30
#   redacted snippets of each query to https://api.typesafe.ai/v1/systemone

codebase-index search "where do we stop users from spamming the chat endpoint" --rerank --compact
# … · confidence high · reranked by typesafe
```

When something is missing, the search still runs locally and the header says why, for example
`rerank skipped: no API key: set $TYPESAFE_API_KEY or rerank.api_key_file`.

## What is sent, and what it costs

- Per query: the question, and for each of the top `top_n` candidates its path and up to
  `max_lines` lines of code, after the same secret redaction applied to all output (AWS keys,
  private-key blocks, assigned secrets). Nothing else from the repository.
- Calls go to `rerank.endpoint` (default `https://api.typesafe.ai/v1/systemone`), 12 at a time.
- Cost at TypeSafe's listed $0.042 per million input tokens: about 25,000 tokens, roughly
  $0.001, per re-ranked search.
- TypeSafe states it does not train on customer requests; zero data retention is offered to
  enterprise customers only. See [TypeSafe legal](https://docs.typesafe.ai/legal.md) and decide
  whether that fits the code you index.
- A failed call scores a neutral 0.5; if every call fails, the local order is returned.

## Settings

| Key | Default | Meaning |
|---|---|---|
| `rerank.backend` | `none` | `typesafe` enables the TypeSafe re-ranker |
| `rerank.allow_external` | `false` | must be `true`; otherwise `--rerank` is refused |
| `rerank.api_key_file` | unset | file with the key, read when `TYPESAFE_API_KEY` is unset |
| `rerank.endpoint` | `https://api.typesafe.ai/v1/systemone` | System One endpoint |
| `rerank.model` | `jev-latest` | TypeSafe model id |
| `rerank.top_n` | `30` | local candidates scored per query |
| `rerank.max_lines` | `60` | lines of each candidate that are sent |
| `rerank.workers` | `12` | concurrent calls |
| `rerank.timeout_s` | `20` | per-call timeout |
| `rerank.retries` | `3` | retries on 429/5xx with backoff |

## Turning it off

Set `"allow_external": false` (or `"backend": "none"`) in the project's config, or remove the
`rerank` section. Delete `~/.config/typesafe/api_key` to remove the key from the machine.
