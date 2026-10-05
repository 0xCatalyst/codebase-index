"""`search --rerank`: opt-in external re-ranking (no network in tests: HTTP is faked)."""

from __future__ import annotations

import json
import urllib.error

import pytest
from typer.testing import CliRunner

import codebase_index.retrieval.external_rerank as ext
from codebase_index.cli import app
from codebase_index.config import Config
from codebase_index.doctor import run_doctor
from codebase_index.indexer.pipeline import build_index
from codebase_index.service import search_payload
from codebase_index.storage.db import Database

QUERY = "stop users from spamming the chat endpoint"
ANSWER = "app/api/chat/route.ts"
FAKE_AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"

_FILES = {
    ANSWER: f"""\
const AUDIT_KEY = "{FAKE_AWS_KEY}";
export async function POST(req: Request) {{
  const limited = await checkRateLimit(req.headers.get('x-ip'), {{ max: 10 }});
  if (!limited.ok) return new Response('slow down', {{ status: 429 }});
  return new Response('ok');
}}
""",
    "lib/chat/types.ts": """\
// The chat endpoint payload sent by users.
export interface ChatEndpointPayload { users: string[]; endpoint: string }
""",
    "lib/chat/users.ts": """\
// Users of the chat endpoint, shown in the sidebar.
export function listChatUsers(endpoint: string) { return [endpoint]; }
""",
    "components/chat/panel.tsx": """\
export function ChatPanel() { return <div className="chat">users and the chat endpoint</div>; }
""",
}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    for rel, text in _FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    monkeypatch.delenv(ext.API_KEY_ENV, raising=False)
    cfg = Config()
    cfg.root = str(root)
    db_path = tmp_path / "index.sqlite"
    with Database(db_path) as db:
        build_index(cfg, db, root=root)
    return root, cfg, db_path


def _enable(cfg, monkeypatch):
    cfg.rerank.backend = "typesafe"
    cfg.rerank.allow_external = True
    monkeypatch.setenv(ext.API_KEY_ENV, "test-key")


def _fake_post(sent: list, *, fail: bool = False):
    def post(url, headers, body, timeout):
        sent.append({"url": url, "headers": headers, "body": json.dumps(body)})
        if fail:
            raise urllib.error.URLError("offline")
        noul = 0.95 if body["state"]["path"] == ANSWER else 0.05
        return {"model": "jev-1.13.0", "answers": {"answers_question": {"type": "noul", "noul": noul}},
                "usage": {"input_tokens": 100, "output_tokens": 20}}
    return post


def _paths(payload):
    return [r["path"] for r in payload["results"]]


def test_rerank_is_refused_without_opt_in(repo):
    _root, cfg, db_path = repo
    cfg.rerank.backend = "typesafe"
    local = search_payload(db_path, cfg, QUERY)
    asked = search_payload(db_path, cfg, QUERY, rerank=True)
    assert asked["rerank"]["applied"] is False
    assert "allow_external" in asked["rerank"]["reason"]
    assert _paths(asked) == _paths(local)


def test_rerank_is_refused_without_a_key(repo):
    _root, cfg, db_path = repo
    cfg.rerank.backend = "typesafe"
    cfg.rerank.allow_external = True
    asked = search_payload(db_path, cfg, QUERY, rerank=True)
    assert asked["rerank"] == {"applied": False, "reason": asked["rerank"]["reason"]}
    assert "API key" in asked["rerank"]["reason"]


def test_key_file_is_read_when_the_env_var_is_unset(repo, tmp_path):
    _root, cfg, _db = repo
    key_file = tmp_path / "key"
    key_file.write_text("file-key\n", encoding="utf-8")
    cfg.rerank.backend = "typesafe"
    cfg.rerank.allow_external = True
    cfg.rerank.api_key_file = str(key_file)
    assert ext.api_key(cfg) == "file-key" and ext.refusal(cfg) is None


def test_rerank_reorders_by_probability_and_redacts_what_it_sends(repo, monkeypatch):
    _root, cfg, db_path = repo
    _enable(cfg, monkeypatch)
    sent: list = []
    monkeypatch.setattr(ext, "_http_post", _fake_post(sent))
    payload = search_payload(db_path, cfg, QUERY, rerank=True)
    assert _paths(payload)[0] == ANSWER
    assert payload["rerank"]["applied"] is True
    assert payload["rerank"]["candidates"] == len(sent) > 1
    assert payload["rerank"]["input_tokens"] == 100 * len(sent)
    assert payload["confidence"] == "high"
    bodies = "".join(s["body"] for s in sent)
    assert FAKE_AWS_KEY not in bodies and "<<redacted:aws_access_key>>" in bodies
    assert all(s["headers"]["Authorization"] == "Bearer test-key" for s in sent)


def test_every_call_failing_keeps_the_local_order(repo, monkeypatch):
    _root, cfg, db_path = repo
    local = search_payload(db_path, cfg, QUERY)
    _enable(cfg, monkeypatch)
    cfg.rerank.retries = 0
    sent: list = []
    monkeypatch.setattr(ext, "_http_post", _fake_post(sent, fail=True))
    payload = search_payload(db_path, cfg, QUERY, rerank=True)
    assert payload["rerank"]["applied"] is False
    assert payload["rerank"]["reason"] == "every rerank call failed"
    assert _paths(payload) == _paths(local)


def test_rerank_settings_do_not_invalidate_the_index():
    a, b = Config(), Config()
    b.rerank.backend = "typesafe"
    b.rerank.allow_external = True
    b.rerank.top_n = 5
    assert a.config_hash() == b.config_hash()


def test_doctor_names_the_endpoint_snippets_go_to(repo, monkeypatch):
    root, cfg, _db = repo
    _enable(cfg, monkeypatch)
    (finding,) = [f for f in run_doctor(root, cfg) if f.id == "rerank_external"]
    assert cfg.rerank.endpoint in finding.detail and "EXTERNAL RERANK" in finding.detail


def test_cli_rerank_flag(repo, monkeypatch):
    root, _cfg, _db = repo
    cache = root / ".claude" / "cache" / "codebase-index"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "config.json").write_text(json.dumps(
        {"rerank": {"backend": "typesafe", "allow_external": True}}), encoding="utf-8")
    monkeypatch.setenv(ext.API_KEY_ENV, "test-key")
    sent: list = []
    monkeypatch.setattr(ext, "_http_post", _fake_post(sent))
    result = CliRunner().invoke(
        app, ["--root", str(root), "search", QUERY, "--rerank", "--compact"]
    )
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    head = next(i for i, line in enumerate(lines) if line.startswith("# "))
    assert "reranked by typesafe" in lines[head]
    assert lines[head + 1].startswith(ANSWER)
