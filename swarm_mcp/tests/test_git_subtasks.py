"""git and subtasks modules against a small synthetic repo (built with real git) plus the synthetic village."""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import A_GPT, A_OPUS, _msg, call, call_error, config_for, make_village

from swarm_mcp.server import build_server

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

OPUS = ("Claude Opus 4.5", "claude-opus-4.5@agentvillage.org")
GPT = ("gpt-5-2", "gpt-5.2@agentvillage.org")  # git name differs from the village name: matched via email
GEM = ("Gemini 2.5 Pro", "gemini-2.5-pro@agentvillage.org")
OUTSIDER = ("Minuteandone", "someone@example.com")


def _git(cwd: Path, *args: str, who=OPUS, when="2026-01-06T10:00:00Z") -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": who[0],
        "GIT_AUTHOR_EMAIL": who[1],
        "GIT_COMMITTER_NAME": who[0],
        "GIT_COMMITTER_EMAIL": who[1],
        "GIT_AUTHOR_DATE": when,
        "GIT_COMMITTER_DATE": when,
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def _commit(w: Path, files: dict[str, str], msg: str, who, when) -> str:
    for p, text in files.items():
        (w / p).parent.mkdir(parents=True, exist_ok=True)
        (w / p).write_text(text)
    _git(w, "add", "-A", who=who, when=when)
    _git(w, "commit", "-q", "-m", msg, who=who, when=when)
    return _git(w, "rev-parse", "HEAD")


TALENTS = "export function allocateTalent(talentTree, talentId) {\n  return talentTree.ranks[talentId] + 1;\n}\n"
FISHING = "export function castFishingLine(fishingSpot, baitType) {\n  return fishingSpot.catch(baitType);\n}\n"


def make_repo(root: Path) -> Path:
    """main + 5 PRs: talents core (merge), talents UI (squash), talents tests (unmerged), fishing (unmerged),
    a talents fix (merge). Returns the bare clone with refs/pull/N/head."""
    w = root.parent / "work-repo"
    w.mkdir(parents=True)
    root.mkdir(parents=True, exist_ok=True)
    _git(w, "init", "-q", "-b", "main")
    _commit(
        w,
        {"package.json": "{}\n", "src/main.js": "import { render } from './render.js';\n"},
        "init",
        OPUS,
        "2026-01-05T10:00:00Z",
    )
    heads = {}
    # PR 1: Opus creates src/talents.js; GPT merges it
    _git(w, "checkout", "-q", "-b", "talents")
    heads[1] = _commit(w, {"src/talents.js": TALENTS}, "feat: add talent tree core", OPUS, "2026-01-06T10:00:00Z")
    _git(w, "checkout", "-q", "main")
    _git(
        w,
        "merge",
        "-q",
        "--no-ff",
        "talents",
        "-m",
        "Merge pull request #1 from village/talents\n\nfeat: Talent tree core",
        who=GPT,
        when="2026-01-06T11:00:00Z",
    )
    # PR 2: GPT wires talents into main.js and adds a UI file; squash-merged
    _git(w, "checkout", "-q", "-b", "talents-ui")
    heads[2] = _commit(
        w,
        {
            "src/main.js": "import { render } from './render.js';\nimport { allocateTalent } from './talents.js';\n",
            "src/talents-ui.js": "export function renderTalentTree(talentTree) { return talentTree.ranks; }\n",
        },
        "feat: wire talent tree into main",
        GPT,
        "2026-01-06T12:00:00Z",
    )
    _git(w, "checkout", "-q", "main")
    _git(w, "merge", "-q", "--squash", "talents-ui", who=GPT, when="2026-01-06T13:00:00Z")
    _git(w, "commit", "-q", "-m", "feat: wire talent tree into main (#2)", who=GPT, when="2026-01-06T13:00:00Z")
    # PR 3: Gemini adds tests importing talents.js (never merged)
    _git(w, "checkout", "-q", "-b", "talent-tests")
    heads[3] = _commit(
        w,
        {
            "tests/talents-test.mjs": "import { allocateTalent } from '../src/talents.js';\nallocateTalent({ranks: {}}, 'a');\n"
        },
        "test: talent tree allocation tests",
        GEM,
        "2026-01-06T14:00:00Z",
    )
    # PR 4: an outsider adds an unrelated fishing module (never merged)
    _git(w, "checkout", "-q", "main")
    _git(w, "checkout", "-q", "-b", "fishing")
    heads[4] = _commit(w, {"src/fishing.js": FISHING}, "Add fishing minigame", OUTSIDER, "2026-01-06T15:00:00Z")
    # PR 5: Gemini fixes talents.js; merged by Opus
    _git(w, "checkout", "-q", "main")
    _git(w, "checkout", "-q", "-b", "talent-fix")
    heads[5] = _commit(
        w, {"src/talents.js": TALENTS.replace("+ 1", "+ 2")}, "fix: talent rank off by one", GEM, "2026-01-07T09:00:00Z"
    )
    _git(w, "checkout", "-q", "main")
    _git(
        w,
        "merge",
        "-q",
        "--no-ff",
        "talent-fix",
        "-m",
        "Merge pull request #5 from village/talent-fix\n\nfix: talent rank off by one",
        who=OPUS,
        when="2026-01-07T10:00:00Z",
    )
    bare = root / "rpg.git"
    _git(root, "clone", "-q", "--bare", str(w), str(bare))
    for n, sha in heads.items():
        _git(bare, "update-ref", f"refs/pull/{n}/head", sha)
    return bare


@pytest.fixture
def git_data(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    vdir = make_village(data)
    make_repo(vdir / "repos")
    # chat that mentions the PRs
    with gzip.open(vdir / "chat_messages.jsonl.gz", "rt") as f:
        rows = [json.loads(x) for x in f]
    rows += [
        _msg(900, "2026-01-06 10:30:00.000000", "PR #1 is up: talent tree core, please review", A_OPUS),
        _msg(901, "2026-01-06 12:30:00.000000", "Wired talents in PR #2, builds on PR #1", A_GPT),
    ]
    with gzip.open(vdir / "chat_messages.jsonl.gz", "wt") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)
    return data


@pytest.fixture
def gapp(git_data: Path):
    return build_server(config_for(git_data))


def test_modules_skip_without_repos(data_dir: Path):
    reg = build_server(config_for(data_dir)).swarm_registry.records
    assert reg["git"].status == "skipped" and "no bare git repos" in reg["git"].reasons[0]
    assert reg["subtasks"].status == "skipped"


def test_git_prs_and_states(gapp):
    out = call(gapp, "git_prs")
    by = {p["number"]: p for p in out["prs"]}
    assert out["total_matches"] == 5
    assert by[1]["state"] == "merged" and by[2]["state"] == "merged" and by[5]["state"] == "merged"
    assert by[3]["state"] == "unmerged" and by[4]["state"] == "unmerged"
    assert by[2]["title"] == "feat: wire talent tree into main"  # squash title without (#2)
    assert call(gapp, "git_prs", query="fishing")["prs"][0]["event_id"] == "git:pr:rpg#4"
    assert call(gapp, "git_repos")["repos"][0]["pr_states"] == {"merged": 3, "unmerged": 2}


def test_git_event_ids_resolve(gapp):
    pr = call(gapp, "core_get_event", event_id="git:pr:rpg#1", after=5)
    assert pr["event"]["title"] == "feat: Talent tree core" and pr["event"]["merged_by"] == "gpt-5-2"
    assert pr["event"]["merge_kind"] == "merge" and len(pr["after"]) == 1
    sha = pr["after"][0]["sha"]
    c = call(gapp, "core_get_event", event_id=f"git:commit:rpg@{sha[:9]}")
    assert c["event"]["actor"] == "Claude Opus 4.5" and "src/talents.js (+3 -0)" in c["event"]["text"]
    assert c["event"]["prs"] == ["git:pr:rpg#1"] and c["context"] == "commits of PR #1"
    assert "No 'pr' record" in call_error(gapp, "core_get_event", event_id="git:pr:rpg#99")
    assert "No 'commit' record" in call_error(gapp, "core_get_event", event_id="git:commit:rpg@deadbeef")


def test_handoffs_are_typed_and_attributed(gapp):
    out = call(gapp, "subtasks_trace_pair", agent_a="Opus 4.5", agent_b="GPT-5.2")
    kinds = {(h["from_agent"], h["to_agent"], h["type"]) for h in out["handoffs"]}
    assert ("Claude Opus 4.5", "GPT-5.2", "integrates") in kinds  # GPT imported Opus's talents.js
    h = out["handoffs"][0]
    assert h["from_pr"] == "git:pr:rpg#1" and h["to_pr"] == "git:pr:rpg#2" and h["files"] == ["src/talents.js"]
    assert all(e.startswith("git:commit:rpg@") for e in h["evidence"])
    gem = call(gapp, "subtasks_trace_pair", agent_a="Opus 4.5", agent_b="Gemini 2.5")
    assert {h["type"] for h in gem["handoffs"]} == {"tests", "fixes"}
    assert gem["summary"]["merged_the_others_pr"] == 1  # Opus merged Gemini's fix
    assert "made no commits" in call_error(gapp, "subtasks_trace_pair", agent_a="Opus 4.5", agent_b="o3")


def test_list_get_and_locate(gapp):
    out = call(gapp, "subtasks_list", granularity="coarse", min_size=1)
    assert out["total_prs"] == 5 and out["method"] == "combined"
    sub = call(gapp, "subtasks_locate", event_id="git:pr:rpg#1", granularity="coarse")["matches"][0]["subtask"]
    got = call(gapp, "subtasks_get", subtask_id=sub["subtask_id"])
    members = {m["event_id"] for m in got["members"]}
    assert {"git:pr:rpg#1", "git:pr:rpg#2"} <= members and "git:pr:rpg#4" not in members
    assert got["label"].startswith("talent") and got["handoffs"]
    agents = {p["agent"] for p in got["participants"]}
    assert "GPT-5.2" in agents and "Claude Opus 4.5" in agents
    assert got["chat"]["messages_mentioning_members"] == 2
    chat_id = got["chat"]["cited"][1]["event_id"]
    assert [m["pr"]["event_id"] for m in call(gapp, "subtasks_locate", event_id=chat_id)["matches"]] == [
        "git:pr:rpg#1",
        "git:pr:rpg#2",
    ]
    fish = call(gapp, "subtasks_locate", event_id="git:pr:rpg#4")["matches"][0]
    assert fish["pr"]["author"] == "git:Minuteandone"  # outsider kept, labelled as a git identity
    assert "Malformed subtask_id" in call_error(gapp, "subtasks_get", subtask_id="talents")
