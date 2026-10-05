"""Opt-in external re-ranking of the top local candidates (TypeSafe System One).

Local retrieval matches words, symbols and graph links, so a plain-English question
worded unlike the code ("stop users from spamming the chat endpoint") can rank the
answer low. `search --rerank` asks a calibrated yes/no model, once per candidate,
whether that code is where the question is answered, and reorders the top candidates
by the probability it returns.

This is the second code path that can send repository text off the machine
(SECURITY.md), so it is refused unless the config opts in
(`rerank.allow_external = true`) and an API key is present. Every snippet passes
through the output redactor before it is sent, and any failure keeps the local order.
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from ..output.redact import redact_snippet

API_KEY_ENV = "TYPESAFE_API_KEY"
_RETRY_STATUS = frozenset({429, 500, 502, 503, 529})

# One Noul question per candidate (https://docs.typesafe.ai/primitives/noul): a single
# yes/no, phrased so that a high value means "this is the code to read".
QUESTION = {
    "answers_question": {
        "type": "noul",
        "instructions": (
            "A developer asked the question in state.question about this codebase. Is the "
            "code in state.code the place they are looking for: where the behavior they ask "
            "about is implemented, configured or decided?"
        ),
        "criteria": {
            "true": "The code implements, configures or directly decides what the question "
                    "asks about.",
            "false": "The code is unrelated, only mentions the topic in passing, or merely "
                     "uses or tests it.",
        },
    }
}

Post = Callable[[str, dict, dict, float], dict]


def _http_post(url: str, headers: dict, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST", headers=headers
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https endpoint
        return json.loads(resp.read())


def api_key(cfg) -> Optional[str]:
    """$TYPESAFE_API_KEY, else the first line of `rerank.api_key_file`."""
    key = os.environ.get(API_KEY_ENV, "").strip()
    if key:
        return key
    path = cfg.rerank.api_key_file
    if not path:
        return None
    try:
        return Path(path).expanduser().read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def refusal(cfg) -> Optional[str]:
    """Why `--rerank` cannot run with this config, or None when it can."""
    rr = cfg.rerank
    if rr.backend == "none":
        return "rerank.backend is 'none'"
    if not rr.allow_external:
        return "rerank.allow_external is false: snippets would leave the machine (SECURITY.md)"
    if api_key(cfg) is None:
        return f"no API key: set ${API_KEY_ENV} or rerank.api_key_file"
    return None


class TypeSafeReranker:
    name = "typesafe"

    def __init__(self, cfg, key: str, post: Optional[Post] = None) -> None:
        self._cfg = cfg.rerank
        self._key = key
        self._post = post
        self.usage = {"calls": 0, "failed": 0, "input_tokens": 0}

    @property
    def endpoint(self) -> str:
        return self._cfg.endpoint

    @property
    def top_n(self) -> int:
        return max(1, int(self._cfg.top_n))

    @property
    def max_lines(self) -> int:
        return max(1, int(self._cfg.max_lines))

    def score(self, query: str, items: Sequence[tuple[str, str]]) -> list[Optional[float]]:
        """Probability that each (path, code) answers `query`; None where a call failed."""
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}

        def one(item: tuple[str, str]) -> Optional[float]:
            path, code = item
            body = {
                "model": self._cfg.model,
                "state": {"question": query, "path": path, "code": code},
                "questions": QUESTION,
            }
            for attempt in range(self._cfg.retries + 1):
                try:
                    post = self._post or _http_post
                    data = post(self._cfg.endpoint, headers, body, self._cfg.timeout_s)
                    self.usage["calls"] += 1
                    self.usage["input_tokens"] += int((data.get("usage") or {}).get("input_tokens", 0))
                    return float(data["answers"]["answers_question"]["noul"])
                except urllib.error.HTTPError as exc:
                    if exc.code not in _RETRY_STATUS or attempt == self._cfg.retries:
                        break
                    time.sleep(_backoff(exc, attempt))
                except (urllib.error.URLError, TimeoutError, OSError, KeyError, ValueError, TypeError):
                    if attempt == self._cfg.retries:
                        break
                    time.sleep(_backoff(None, attempt))
            self.usage["failed"] += 1
            return None

        workers = max(1, min(self._cfg.workers, len(items)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(one, items))


def _backoff(exc: Optional[urllib.error.HTTPError], attempt: int) -> float:
    retry_after = exc.headers.get("Retry-After") if exc is not None and exc.headers else None
    if retry_after:
        try:
            return min(float(retry_after), 10.0)
        except ValueError:
            pass
    return min(0.5 * (2 ** attempt), 8.0) + random.random() * 0.25


def make_reranker(
    cfg, post: Optional[Post] = None
) -> tuple[Optional[TypeSafeReranker], Optional[str]]:
    """(reranker, None) when the config allows it, else (None, the reason it is refused)."""
    why = refusal(cfg)
    if why is not None:
        return None, why
    key = api_key(cfg)
    assert key is not None
    return TypeSafeReranker(cfg, key, post), None


def rerank_top(
    reranker: TypeSafeReranker,
    query: str,
    ranked: list,
    *,
    top_n: int,
    text_of: Callable[[Any], str],
) -> tuple[list, dict[str, Any]]:
    """Reorder the first `top_n` of `ranked` by the model's probability; keep the rest.

    A candidate whose call failed keeps a neutral 0.5. When every call fails the local
    order is returned unchanged and the outcome says so.
    """
    head, tail = ranked[:top_n], ranked[top_n:]
    if not head:
        return ranked, {"applied": False, "reason": "no candidates"}
    items = [(c.path, redact_snippet(text_of(c))) for c in head]
    started = time.monotonic()
    scores = reranker.score(query, items)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    meta: dict[str, Any] = {
        "backend": reranker.name,
        "endpoint": reranker.endpoint,
        "candidates": len(head),
        "failed": reranker.usage["failed"],
        "input_tokens": reranker.usage["input_tokens"],
        "latency_ms": elapsed_ms,
    }
    if all(s is None for s in scores):
        return ranked, {"applied": False, "reason": "every rerank call failed", **meta}
    neutral = [0.5 if s is None else s for s in scores]
    order = sorted(range(len(head)), key=lambda i: (-neutral[i], i))
    reordered = []
    for i in order:
        cand = head[i]
        noul = scores[i]
        cand.rerank_score = noul
        if noul is not None:
            cand.reason = f"{cand.reason}; {reranker.name} {noul:.2f}" if cand.reason else (
                f"{reranker.name} {noul:.2f}"
            )
        reordered.append(cand)
    meta["top_score"] = scores[order[0]]
    return reordered + tail, {"applied": True, **meta}
