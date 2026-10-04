"""The swarm-mcp command line: info, add (AI Village, git, wiki and mapped datasets), export. All data is
synthetic."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from conftest import make_village
from setup_datasets import make_nested_jsonl, make_sqlite_board
from test_git_subtasks import make_repo
from test_mapped_ingest import BOARD_SPEC
from test_wiki import make_wiki

from swarm_mcp.cli import default_name, main
from swarm_mcp.scope import db

EMAIL = "bob.smith@gmail.com"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project root with a swarm.toml; the cwd, so Config.load() finds it."""
    root = tmp_path / "proj"
    (root / "data").mkdir(parents=True)
    (root / "swarm.toml").write_text('[data]\ndir = "data"\nfindings = "findings"\nsweeps = "sweeps"\n')
    monkeypatch.chdir(root)
    for var in ("SWARM_DATA_DIR", "ANTHROPIC_API_KEY", "SWARM_LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    return root


def cli(*args: str) -> int:
    with pytest.raises(SystemExit) as e:
        main(list(args))
    return e.value.code


def counts(store: Path, source: str) -> dict[str, int]:
    with db.connect(store) as s:
        return {t: s.scalar(f"SELECT count(*) FROM {t} WHERE source = ?", [source]) for t in ("messages", "agents")}


def test_add_detects_ai_village_and_is_idempotent(project: Path, capsys):
    make_village(project / "data")
    store = project / "data" / "swarmscope.duckdb"
    assert cli("add", "data/ai-village", "--dry-run") == 0
    assert "nothing ingested" in capsys.readouterr().out and not store.exists()

    assert cli("add", "data/ai-village") == 0
    out = capsys.readouterr().out
    assert "detected the AI Village layout" in out and "ingested source 'village'" in out
    assert "260 messages" in out and "swarm-mcp info" in out
    assert counts(store, "village") == {"messages": 260, "agents": 4}
    assert cli("add", "data") == 0  # the folder that holds ai-village/ works too, and re-adding replaces
    assert counts(store, "village") == {"messages": 260, "agents": 4}
    assert cli("add", "data/ai-village", "--name", "other") == 2
    assert "always uses the source name 'village'" in capsys.readouterr().err
    assert cli("add", "data/nope") == 2


def test_info_reports_and_fails_on_bad_findings(project: Path, capsys):
    make_village(project / "data")
    assert cli("add", "data/ai-village") == 0
    capsys.readouterr()
    assert cli("info") == 0
    out = capsys.readouterr().out
    assert "Sources" in out and "village" in out and "260 messages" in out and "Findings  [ok]" in out
    assert "core_get, core_info" in out and "scope_search" in out
    assert cli("info", "--json") == 0
    info = json.loads(capsys.readouterr().out)
    assert info["config"]["config_file"] == str(project / "swarm.toml") and info["findings"]["ok"] is True

    fdir = project / "findings"
    fdir.mkdir()
    bad = {"finding_id": "f-1", "created_at": "2026-10-03T12:00:00Z", "claim": "c",
           "evidence_ids": ["village:msg:does-not-exist"], "status": "open"}  # fmt: skip
    (fdir / "findings.jsonl").write_text(json.dumps(bad) + "\n")
    assert cli("info") == 1
    out = capsys.readouterr().out
    assert "BAD EVIDENCE" in out and "village:msg:does-not-exist" in out


def test_add_with_a_mapping(project: Path, capsys):
    make_sqlite_board(project / "data" / "board")
    mapping = project / "board.json"
    mapping.write_text(json.dumps(BOARD_SPEC))
    store = project / "data" / "swarmscope.duckdb"

    assert cli("add", "data/board", "--mapping", str(mapping), "--dry-run") == 0
    out = capsys.readouterr().out
    assert "PASS" in out and "dry run" in out and not store.exists()

    assert cli("add", "data/board", "--mapping", str(mapping)) == 0
    out = capsys.readouterr().out
    assert "ingested source 'board'" in out and "240 messages, 12 actions" in out
    assert counts(store, "board")["messages"] == 240
    assert cli("add", "data/board", "--mapping", str(mapping), "--name", "forum") == 2

    broken = dict(BOARD_SPEC, records=[dict(BOARD_SPEC["records"][0], text="no_such_field")])
    mapping.write_text(json.dumps(broken))
    assert cli("add", "data/board", "--mapping", str(mapping)) == 1
    assert "does not pass the check, so nothing was ingested" in capsys.readouterr().out


def test_add_drafts_a_mapping(project: Path, capsys):
    make_nested_jsonl(project / "data" / "crew")
    assert cli("add", "data/crew") == 0  # heuristic draft (--agent none) passes on this layout
    out = capsys.readouterr().out
    assert "drafting a mapping (--agent none)" in out and "ingested source 'crew'" in out
    assert (project / "mappings" / "crew.json").exists()
    assert counts(project / "data" / "swarmscope.duckdb", "crew")["messages"] == 300

    assert cli("add", "data/crew", "--name", "crew2", "--agent", "claude-code") == 0
    out = capsys.readouterr().out
    assert "/swarm-setup crew2" in out and (project / "mappings" / "crew2.task.md").exists()
    assert "swarm-mcp add" in (project / "mappings" / "crew2.task.md").read_text()
    assert counts(project / "data" / "swarmscope.duckdb", "crew2")["messages"] == 0  # nothing ingested

    assert cli("add", "data/crew", "--name", "crew3", "--agent", "api") == 2  # no mapping yet and no key
    assert "--agent none" in capsys.readouterr().err
    assert cli("add", "data/crew", "--agent", "api") == 0  # mappings/crew.json exists: no draft, no key needed
    assert "using existing mapping mappings/crew.json" in capsys.readouterr().out
    assert cli("add", "data/crew", "--name", "Bad Name") == 2


def table_count(store: Path, table: str, source: str) -> int:
    with db.connect(store) as s:
        return s.scalar(f"SELECT count(*) FROM {table} WHERE source = ?", [source])


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_add_a_git_repo(project: Path, capsys):
    bare = make_repo(project / "data" / "repos")  # data/repos/rpg.git with 5 pull request heads
    store = project / "data" / "swarmscope.duckdb"
    assert cli("add", "data/repos/rpg.git", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "detected a bare git repository" in out and "built-in git adapter" in out
    assert "dry run: source 'rpg'" in out and "5 pull request heads" in out and not store.exists()

    assert cli("add", "data/repos/rpg.git") == 0  # source defaults to the repo name
    out = capsys.readouterr().out
    assert "ingested source 'rpg' (git adapter)" in out and "5 periods, 5 artifacts" in out
    assert table_count(store, "periods", "rpg") == 5 and table_count(store, "agents", "rpg") == 4

    assert cli("add", "data/repos/rpg.git", "--adapter", "git", "--name", "game") == 0
    assert "ingested source 'game'" in capsys.readouterr().out
    assert table_count(store, "periods", "game") == 5 and table_count(store, "periods", "rpg") == 5
    with db.connect(store) as s:
        assert s.scalar("SELECT evidence_id FROM periods WHERE source = 'game' ORDER BY 1 LIMIT 1").startswith(
            "game:period:pr-"
        )

    shutil.copytree(bare, project / "data" / "repos" / "plain")  # HEAD + objects/ + refs/, no .git suffix
    assert cli("add", "data/repos/plain") == 0
    assert "detected a bare git repository" in capsys.readouterr().out
    assert table_count(store, "periods", "plain") == 5

    (project / "data" / "notarepo").mkdir()
    assert cli("add", "data/notarepo", "--adapter", "git") == 2
    assert "not a git repository root" in capsys.readouterr().err
    assert cli("add", "data/repos/rpg.git", "--name", "Bad Name") == 2
    assert cli("add", "data/repos/rpg.git", "--adapter", "git", "--mapping", "x.json") == 2
    assert cli("add", "data/repos/rpg.git", "--adapter", "village") == 2
    assert "no AI Village file set" in capsys.readouterr().err


def test_add_a_wiki_only_when_asked(project: Path, capsys):
    make_wiki(project / "data")  # data/test-wiki/test-wiki.db: 3 pages, 5 revisions
    store = project / "data" / "swarmscope.duckdb"
    assert cli("add", "data/test-wiki", "--adapter", "wiki", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "a wiki database in" in out and "detected" not in out
    assert "dry run: source 'test-wiki'" in out and "3 pages, 5 revisions" in out and not store.exists()

    assert cli("add", "data/test-wiki", "--adapter", "wiki") == 0
    out = capsys.readouterr().out
    assert "ingested source 'test-wiki' (wiki adapter)" in out and "5 messages" in out and "3 artifacts" in out
    assert counts(store, "test-wiki")["messages"] == 5

    assert cli("add", "data/test-wiki/test-wiki.db", "--adapter", "wiki", "--name", "wiki2") == 0
    assert "ingested source 'wiki2'" in capsys.readouterr().out
    assert counts(store, "wiki2")["messages"] == 5 and counts(store, "test-wiki")["messages"] == 5


def test_export_from_the_store(project: Path, capsys):
    make_village(project / "data")
    assert cli("add", "data/ai-village") == 0
    capsys.readouterr()
    args = ("export", "--source", "village", "--channel", "general", "--since", "2026-01-05", "--until", "2026-01-06")
    assert cli(*args, "--out", "data/export") == 0
    out = capsys.readouterr().out
    assert "exported 4 records" in out and "village: 4 msg" in out and "check passed" in out
    text = (project / "data" / "export" / "events.jsonl").read_text()
    assert EMAIL not in text and "[email]" in text and "help@agentvillage.org" in text

    assert cli(*args, "--out", "data/export", "--json", "--with-agents") == 0
    res = json.loads(capsys.readouterr().out)
    assert res["ok"] is True and res["records"]["events"] == 4 and res["records"]["agents"] == 4
    assert res["redaction"]["counts"]["email"] >= 1 and res["check"]["findings"] == []
    assert cli(*args, "--out", "data/export2", "--no-check") == 0
    assert "check skipped" in capsys.readouterr().out
    assert cli("export", "--out", "data/x", "--source", "nope") == 2


def test_removed_commands_are_gone(project: Path, capsys):
    for old in (["ingest", "ai_village", "data"], ["check-findings"], ["--list-modules"]):
        assert cli(*old) == 2  # falls through to the server's parser, which rejects it


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_add_refuses_a_name_collision_without_replace(project: Path, capsys):
    """Regression: `add <a repo named village.git>` silently replaced the AI Village source (0 messages left),
    and two different repos or a mapped folder with the same slug did the same."""
    make_village(project / "data")
    store = project / "data" / "swarmscope.duckdb"
    assert cli("add", "data/ai-village") == 0
    assert "a new source" in capsys.readouterr().out
    assert cli("add", "data/ai-village") == 0
    assert "replaced the previous copy of the same dataset" in capsys.readouterr().out

    bare = make_repo(project / "data" / "repos")
    clash = project / "data" / "repos" / "village.git"
    shutil.copytree(bare, clash)
    assert cli("add", str(clash)) == 2  # the git adapter would name it 'village'
    err = capsys.readouterr().err
    assert "source 'village' already holds ai_village data from" in err and "--name" in err and "--replace" in err
    assert counts(store, "village")["messages"] == 260  # untouched

    assert cli("add", "data/repos/rpg.git") == 0
    other = project / "data" / "elsewhere" / "rpg.git"
    shutil.copytree(bare, other)
    assert cli("add", str(other)) == 2  # same adapter, a different repo with the same name
    assert "source 'rpg' already holds git data from" in capsys.readouterr().err

    make_sqlite_board(project / "data" / "village")
    mapping = project / "board.json"
    mapping.write_text(json.dumps({**BOARD_SPEC, "source": "village"}))
    assert cli("add", "data/village", "--mapping", str(mapping)) == 2  # a mapped dataset with the same slug
    assert counts(store, "village")["messages"] == 260

    assert cli("add", str(clash), "--replace") == 0  # deliberate
    out = capsys.readouterr().out
    assert "replaced source 'village', which held ai_village data from" in out
    assert counts(store, "village")["messages"] == 0


def test_add_replaces_a_mapped_source_when_only_the_mapping_changed(project: Path, capsys):
    make_sqlite_board(project / "data" / "board")
    first, second = project / "board.json", project / "board-v2.json"
    first.write_text(json.dumps(BOARD_SPEC))
    second.write_text(json.dumps(BOARD_SPEC))
    assert cli("add", "data/board", "--mapping", str(first)) == 0
    capsys.readouterr()
    assert cli("add", "data/board", "--mapping", str(second)) == 0
    out = capsys.readouterr().out
    assert "which used another mapping" in out and str(first.resolve()) in out and "ingested source 'board'" in out


def test_add_keeps_a_hand_edited_mapping(project: Path, capsys):
    """Regression: re-running `add` without --mapping (even with --dry-run) redrafted mappings/<source>.json
    over the user's edits."""
    make_nested_jsonl(project / "data" / "crew")
    mapping = project / "mappings" / "crew.json"
    store = project / "data" / "swarmscope.duckdb"
    assert cli("add", "data/crew", "--dry-run") == 0  # no mapping yet: a dry run writes the draft for review
    out = capsys.readouterr().out
    assert "drafting a mapping" in out and "nothing ingested" in out and mapping.exists() and not store.exists()

    edited = dict(json.loads(mapping.read_text()), description="hand edited")
    mapping.write_text(json.dumps(edited))
    assert cli("add", "data/crew", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "using existing mapping mappings/crew.json (delete it to redraft)" in out and "drafting" not in out
    assert json.loads(mapping.read_text()) == edited and not store.exists()

    assert cli("add", "data/crew") == 0
    out = capsys.readouterr().out
    assert "using existing mapping" in out and "ingested source 'crew'" in out
    assert json.loads(mapping.read_text()) == edited
    assert counts(store, "crew")["messages"] == 300

    broken = dict(edited, records=[dict(edited["records"][0], text="no_such_field")])
    mapping.write_text(json.dumps(broken))  # the edited mapping is the one checked, not a fresh draft
    assert cli("add", "data/crew") == 1
    assert "does not pass the check" in capsys.readouterr().out
    assert json.loads(mapping.read_text()) == broken

    mapping.unlink()  # deleting it redrafts
    assert cli("add", "data/crew", "--dry-run") == 0
    assert "drafting a mapping" in capsys.readouterr().out and mapping.exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_add_git_needs_a_repository_root(project: Path, capsys):
    """Regression: `add <dir> --adapter git` on a folder inside a working tree (and auto-detect on any folder
    named *.git) ran `git -C <dir>`, which walks up and ingested the enclosing repository."""
    from swarm_mcp.cli import git_repo_dir, git_root

    bare = make_repo(project / "data" / "repos")  # also leaves the working tree data/work-repo (with commits)
    tree = project / "data" / "work-repo"
    store = project / "data" / "swarmscope.duckdb"
    fake = tree / "fake.git"  # a folder named *.git inside a working tree
    fake.mkdir()
    (fake / "notes.txt").write_text("not a repository\n")
    lookalike = project / "data" / "lookalike.git"  # HEAD, objects/ and refs/, but not a repository
    for d in ("objects", "refs"):
        (lookalike / d).mkdir(parents=True)
    (lookalike / "HEAD").write_text("not a ref\n")

    for sub in (tree / "src", fake, lookalike):
        assert git_repo_dir(sub) is None and git_root(sub) is None
        assert cli("add", str(sub), "--adapter", "git") == 2
        assert "not a git repository root" in capsys.readouterr().err
        assert cli("add", str(sub), "--adapter", "git", "--dry-run") == 2
        capsys.readouterr()
    cli("add", str(fake), "--dry-run")  # auto: not taken for a git repo (it goes on to draft a mapping)
    assert "git adapter" not in capsys.readouterr().out
    assert not store.exists()

    assert git_repo_dir(bare) == bare and git_root(bare) == bare  # a bare repo
    assert git_repo_dir(tree) == tree / ".git" and git_root(tree) == tree / ".git"  # a working tree's top folder
    assert cli("add", "data/work-repo", "--adapter", "git", "--dry-run") == 0
    out = capsys.readouterr().out
    assert f"a git working tree in {tree}:" in out and "dry run: source 'work-repo'" in out
    assert cli("add", "data/work-repo", "--adapter", "git") == 0
    assert "ingested source 'work-repo' (git adapter)" in capsys.readouterr().out
    assert table_count(store, "periods", "work-repo") == 0 and table_count(store, "agents", "work-repo") > 0
    assert cli("add", "data/repos/rpg.git", "--adapter", "git", "--dry-run") == 0
    assert "dry run: source 'rpg'" in capsys.readouterr().out


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_add_git_slugifies_the_default_source(project: Path, capsys):
    """Regression: `add "data/repos/My Repo.git"` created source 'My Repo', whose ids evidence.parse rejects."""
    from swarm_mcp.scope.evidence import parse

    bare = make_repo(project / "data" / "repos")
    spaced = project / "data" / "repos" / "My Repo.git"
    shutil.copytree(bare, spaced)
    store = project / "data" / "swarmscope.duckdb"
    assert cli("add", str(spaced), "--dry-run") == 0
    assert "dry run: source 'my_repo'" in capsys.readouterr().out
    assert cli("add", str(spaced)) == 0
    assert "ingested source 'my_repo' (git adapter)" in capsys.readouterr().out
    with db.connect(store) as s:
        ids = [r["evidence_id"] for r in s.all("SELECT evidence_id FROM periods WHERE source = 'my_repo'")]
    assert len(ids) == 5 and all(parse(i) for i in ids)


def test_add_wiki_needs_a_db_in_the_given_folder(project: Path, capsys, monkeypatch: pytest.MonkeyPatch):
    """Regression: with no *.db in the given folder, the wiki adapter searched the SIBLING folders and ingested
    another dataset under that sibling's name. The adapter and ingest are fakes here (empty files, nothing is
    opened); a refused path must never reach them."""
    import swarm_mcp.scope.adapters as adapters
    import swarm_mcp.scope.ingest as ingest_mod

    calls: list[tuple[str, Path]] = []

    class FakeWiki:
        def __init__(self, source: str | None):
            self.source = source

        def inspect(self, path: Path) -> dict:
            calls.append(("inspect", Path(path)))
            return {"source": self.source or (path.parent if path.is_file() else path).name, "pages": 0}

    def fake_get_adapter(name: str, source: str | None = None) -> FakeWiki:
        assert name == "wiki"
        return FakeWiki(source)

    def fake_ingest(name: str, path: Path, db_path: Path, source: str | None = None, **_) -> dict:
        calls.append(("ingest", Path(path)))
        return {"source": source or Path(path).name, "adapter": name, "db": str(db_path), "counts": {}, "seconds": 0}

    monkeypatch.setattr(adapters, "get_adapter", fake_get_adapter)
    monkeypatch.setattr(ingest_mod, "ingest", fake_ingest)
    data = project / "data"
    (data / "mydata" / "nested").mkdir(parents=True)
    (data / "mydata" / "notes.txt").write_text("no database here\n")
    (data / "mydata" / "nested" / "inner.db").touch()  # not directly in mydata/
    (data / "mydata" / "folder.db").mkdir()  # a folder, not a database file
    (data / "sibling").mkdir()
    (data / "sibling" / "sibling.db").touch()  # empty; the sibling the adapter used to fall back to

    for bad in ("data/mydata", "data/mydata/notes.txt", "data/mydata/folder.db"):
        for extra in ((), ("--dry-run",)):
            assert cli("add", bad, "--adapter", "wiki", *extra) == 2
            assert "no wiki database in" in capsys.readouterr().err
    assert calls == []

    assert cli("add", "data/sibling", "--adapter", "wiki", "--dry-run") == 0  # a folder with a *.db
    assert "dry run: source 'sibling'" in capsys.readouterr().out
    assert calls == [("inspect", data / "sibling")]
    calls.clear()
    assert cli("add", "data/sibling/sibling.db", "--adapter", "wiki", "--name", "wiki2") == 0  # a .db file
    assert "ingested source 'wiki2'" in capsys.readouterr().out
    assert calls == [("inspect", data / "sibling" / "sibling.db"), ("ingest", data / "sibling" / "sibling.db")]

    calls.clear()
    (data / "My Wiki").mkdir()
    (data / "My Wiki" / "w.db").touch()
    assert cli("add", "data/My Wiki", "--adapter", "wiki") == 2  # default source 'My Wiki': not an id part
    assert "pass --name" in capsys.readouterr().err
    assert [c[0] for c in calls] == ["inspect"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_add_auto_detects_a_working_tree_and_never_keeps_an_empty_draft(project: Path, capsys):
    """Regression: auto-detect only knew bare repositories, so a working tree, a folder inside one or a fake
    *.git fell through to the mapping drafter, which wrote mappings/<name>.json with no records (spec_invalid);
    every re-run then reused that junk mapping. A working tree was also labelled 'a bare git repository'."""
    make_repo(project / "data" / "repos")  # also leaves the working tree data/work-repo (with commits)
    tree = project / "data" / "work-repo"
    mappings = project / "mappings"
    assert cli("add", "data/work-repo", "--dry-run") == 0
    out = capsys.readouterr().out
    assert f"detected a git working tree in {tree}: using the built-in git adapter" in out
    assert "bare" not in out and "dry run: source 'work-repo'" in out

    lookalike = project / "data" / "lookalike.git"  # HEAD, objects/ and refs/, but not a repository
    for d in ("objects", "refs"):
        (lookalike / d).mkdir(parents=True)
    (lookalike / "HEAD").write_text("")
    for sub, hint in ((tree / "src", f"inside the git repository {tree}"), (lookalike, "git does not read it")):
        for _ in range(2):  # a re-run drafts again: nothing junk was kept to be reused
            assert cli("add", str(sub), "--dry-run") == 2
            out, err = capsys.readouterr()
            assert "spec_invalid" in out and "using existing mapping" not in out
            assert "found no table of timestamped records" in err and "no mapping was written" in err and hint in err
            assert not (mappings / f"{default_name(sub)}.json").exists()
    assert not list(mappings.glob("*.json")) or all(f.name.endswith(".setup.json") for f in mappings.glob("*"))


def test_add_reuses_a_drafted_mapping_only_for_its_own_dataset(project: Path, capsys):
    """Regression: `add <another dataset> --name crew` reused mappings/crew.json (drafted for data/crew), then
    advised editing it, which would break the dataset it belongs to."""
    make_nested_jsonl(project / "data" / "crew")
    make_sqlite_board(project / "data" / "board")
    make_nested_jsonl(project / "data" / "elsewhere" / "crew")  # same layout, same default name, another folder
    mapping = project / "mappings" / "crew.json"
    assert cli("add", "data/crew", "--dry-run") == 0 and mapping.exists()
    drafted = mapping.read_text()
    capsys.readouterr()
    for other in (("data/board", "--name", "crew"), ("data/elsewhere/crew",)):
        assert cli("add", *other, "--dry-run") == 2
        err = capsys.readouterr().err
        assert f"mappings/crew.json belongs to {(project / 'data' / 'crew').resolve()}" in err
        assert "pick another --name or pass --mapping" in err
    assert mapping.read_text() == drafted
    assert cli("add", "data/crew", "--dry-run") == 0  # its own dataset still reuses it
    assert "using existing mapping mappings/crew.json" in capsys.readouterr().out
    (project / "mappings" / "crew.setup.json").unlink()  # no record of the dataset (e.g. a committed mapping)
    assert cli("add", "data/elsewhere/crew", "--dry-run") == 0
    assert "using existing mapping mappings/crew.json" in capsys.readouterr().out
