"""The git adapter and the subtasks module against a small synthetic repo (built with real git), ingested into
the SwarmScope store next to the synthetic village."""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import A_GPT, A_OPUS, _msg, build_store, call, call_error, config_for, make_village

from swarm_mcp.scope.ingest import ingest
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
    """Synthetic village (with chat naming the PRs) + the synthetic repo, both ingested into one store."""
    data = tmp_path / "data"
    vdir = make_village(data)
    make_repo(vdir / "repos")
    with gzip.open(vdir / "chat_messages.jsonl.gz", "rt") as f:
        rows = [json.loads(x) for x in f]
    rows += [
        _msg(900, "2026-01-06 10:30:00.000000", "PR #1 is up: talent tree core, please review", A_OPUS),
        _msg(901, "2026-01-06 12:30:00.000000", "Wired talents in PR #2, builds on PR #1", A_GPT),
    ]
    with gzip.open(vdir / "chat_messages.jsonl.gz", "wt") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)
    db = build_store(data)
    ingest("git", vdir / "repos" / "rpg.git", db, progress=lambda _m: None)
    return data


@pytest.fixture
def gapp(git_data: Path):
    return build_server(config_for(git_data))


def test_subtasks_needs_a_store(raw_data_dir: Path):
    rec = build_server(config_for(raw_data_dir)).swarm_registry.records["subtasks"]
    assert rec.status == "skipped" and "store not found" in rec.reasons[0]


def test_git_records_use_generic_kinds(gapp):
    src = {x["source"]: x for x in call(gapp, "scope_list_sources")["sources"]}["rpg"]
    assert src["row_counts"]["periods"] == 5 and src["row_counts"]["artifacts"] == 5
    assert any("inferred from main" in n for n in src["ingest_meta"]["notes"])
    pr = call(gapp, "scope_get_record", evidence_id="rpg:period:pr-1")
    assert pr["kind"] == "pull_request" and pr["meta"]["state"] == "merged" and pr["meta"]["short"] == "PR #1"
    assert pr["meta"]["merged_by"] == "rpg:agent:gpt-5.2" and pr["label"]["content"] == "feat: Talent tree core"
    states = {n: call(gapp, "scope_get_record", evidence_id=f"rpg:period:pr-{n}")["meta"]["state"] for n in range(1, 6)}
    assert states == {1: "merged", 2: "merged", 3: "unmerged", 4: "unmerged", 5: "merged"}
    commit = call(gapp, "scope_get_record", evidence_id=pr["records"][0])
    assert commit["kind"] == "commit" and commit["agent"] == "Claude Opus 4.5"
    assert commit["artifacts"] == [{"artifact_id": "rpg:artifact:src/talents.js", "op": "create"}]
    art = call(gapp, "scope_get_record", evidence_id="rpg:artifact:src/talents.js")
    assert art["touches_by_op"] == {"create": 1, "modify": 1} and art["kind"] == "file"
    assert call(gapp, "scope_get_record", evidence_id="rpg:artifact:tests/talents-test.mjs")["meta"] == {"role": "test"}
    assert "does not resolve" in call_error(gapp, "scope_get_record", evidence_id="rpg:period:pr-99")


def test_handoffs_are_typed_and_attributed(gapp):
    out = call(gapp, "subtasks_trace_pair", corpus="rpg", actor_a="Opus 4.5", actor_b="GPT-5.2")
    kinds = {(h["from_actor"], h["to_actor"], h["type"]) for h in out["handoffs"]}
    assert ("Claude Opus 4.5", "GPT-5.2", "integrates") in kinds  # GPT (git name gpt-5-2) imported Opus's talents.js
    h = out["handoffs"][0]
    assert h["from_unit"] == "rpg:period:pr-1" and h["to_unit"] == "rpg:period:pr-2"
    assert h["artifacts"] == ["rpg:artifact:src/talents.js"]
    assert all(e.startswith("rpg:event:") for e in h["evidence"])
    for e in h["evidence"]:
        call(gapp, "scope_get_record", evidence_id=e)  # every cited id resolves
    gem = call(gapp, "subtasks_trace_pair", corpus="rpg", actor_a="Opus 4.5", actor_b="Gemini 2.5")
    assert {h["type"] for h in gem["handoffs"]} == {"tests", "fixes"}
    assert gem["summary"]["finalised_the_others_unit"] == 1  # Opus merged Gemini's fix
    assert "did no work" in call_error(gapp, "subtasks_trace_pair", corpus="rpg", actor_a="Opus 4.5", actor_b="o3")


def test_list_get_and_locate(gapp):
    out = call(gapp, "subtasks_list", corpus="rpg", granularity="coarse", min_size=1)
    assert out["total_units"] == 5 and out["unit"] == "pull request"
    sub = call(gapp, "subtasks_locate", event_id="rpg:period:pr-1", granularity="coarse")["matches"][0]["subtask"]
    got = call(gapp, "subtasks_get", subtask_id=sub["subtask_id"])
    members = {m["event_id"] for m in got["members"]}
    assert {"rpg:period:pr-1", "rpg:period:pr-2"} <= members and "rpg:period:pr-4" not in members
    assert got["label"].startswith("talent") and got["handoffs"]
    assert {"GPT-5.2", "Claude Opus 4.5"} <= {p["actor"] for p in got["participants"]}
    assert got["chat"]["messages_mentioning_members"] == 2
    chat_id = got["chat"]["cited"][1]["event_id"]
    assert chat_id.startswith("village:msg:")
    located = call(gapp, "subtasks_locate", event_id=chat_id)["matches"]
    assert [m["unit"]["event_id"] for m in located] == ["rpg:period:pr-1", "rpg:period:pr-2"]
    fish = call(gapp, "subtasks_locate", event_id="rpg:period:pr-4")["matches"][0]
    assert fish["unit"]["actor"] == "Minuteandone"  # no village agent matches this git identity
    assert "Malformed subtask_id" in call_error(gapp, "subtasks_get", subtask_id="talents")
    commit = got["handoffs"][0]["evidence"][-1]
    located = call(gapp, "subtasks_locate", event_id=commit, granularity="coarse")["matches"][0]["subtask"]
    assert located["subtask_id"] == sub["subtask_id"]
    row = {c["corpus"]: c for c in call(gapp, "subtasks_corpora")["corpora"]}["rpg"]
    assert {"unit": "pull request", "units": 5, "artifact": "file"}.items() <= row.items()
