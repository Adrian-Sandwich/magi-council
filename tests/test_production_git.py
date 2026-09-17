"""Real Git repositories; no LLMs, production data, or user branches."""

from pathlib import Path
from contextlib import nullcontext
import os
import sys

import pytest

import production
import board
import heads
import relay


@pytest.fixture
def repo(tmp_path, allow_real_processes, monkeypatch):
    # Ignore user signing, hooks and global worktree preferences in these repos.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "empty-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    path = tmp_path / "repo with spaces"
    path.mkdir()
    p = str(path)
    production.git(p, "init", "-b", "main")
    production.git(p, "config", "user.name", "Test")
    production.git(p, "config", "user.email", "test@example.invalid")
    (path / "app.txt").write_text("base\n")
    production.git(p, "add", "app.txt")
    production.git(p, "commit", "-m", "initial")
    return p


def implementation(repo):
    run = production.plan(repo, 42)
    worktree = production.prepare(run)
    (Path(worktree) / "app.txt").write_text("implemented\n")
    production.git(worktree, "add", "app.txt")
    production.git(worktree, "commit", "-m", "implementation")
    sha, diff = production.review_target(run)
    run.update(reviewed_sha=sha, review_id=43)
    assert "implemented" in diff
    return run


def test_worktree_leaves_user_branch_and_dirty_files_untouched(repo):
    (Path(repo) / "app.txt").write_text("user edits\n")
    (Path(repo) / "notes.txt").write_text("untracked\n")
    original = production.git(repo, "rev-parse", "HEAD")
    run = implementation(repo)
    assert production.git(repo, "symbolic-ref", "--short", "HEAD") == "main"
    assert production.git(repo, "rev-parse", "HEAD") == original
    assert (Path(repo) / "app.txt").read_text() == "user edits\n"
    assert (Path(repo) / "notes.txt").read_text() == "untracked\n"
    assert production.common_dir(run["worktree"]) == production.common_dir(repo)


def test_retry_preserves_base_and_uncommitted_execution(repo):
    run = production.plan(repo, 42)
    worktree = production.prepare(run)
    (Path(worktree) / "unfinished.txt").write_text("keep this")
    production.git(repo, "checkout", "-b", "other")
    resumed = production.plan(repo, 42, run)
    assert resumed == run
    assert production.prepare(resumed) == worktree
    assert (Path(worktree) / "unfinished.txt").read_text() == "keep this"


def test_relay_commits_executor_workspace_without_model_git_access(repo):
    run = production.plan(repo, 42)
    worktree = production.prepare(run)
    (Path(worktree) / "app.txt").write_text("implemented by model\n")
    (Path(worktree) / "new.txt").write_text("new file\n")
    sha = production.commit_execution(run, "MAGI execution")
    assert sha == production.git(worktree, "rev-parse", "HEAD")
    assert production.git(worktree, "status", "--porcelain") == ""
    reviewed_sha, diff = production.review_target(run)
    assert reviewed_sha == sha and "implemented by model" in diff and "new file" in diff


def test_existing_unowned_branch_is_not_adopted(repo):
    production.git(repo, "branch", "magi/d42")
    run = production.plan(repo, 42)
    with pytest.raises(RuntimeError):
        production.prepare(run)
    assert production.git(repo, "symbolic-ref", "--short", "HEAD") == "main"


def test_two_executions_have_distinct_worktrees(repo):
    first = production.prepare(production.plan(repo, 1))
    second = production.prepare(production.plan(repo, 2))
    assert first != second
    (Path(first) / "only-first.txt").write_text("one")
    assert not (Path(second) / "only-first.txt").exists()


@pytest.mark.parametrize("change", ["dirty", "branch"])
def test_review_refuses_uncommitted_or_wrong_branch(repo, change):
    run = implementation(repo)
    if change == "dirty":
        (Path(run["worktree"]) / "untracked.txt").write_text("missing commit")
    else:
        production.git(run["worktree"], "checkout", "-b", "wrong")
    with pytest.raises(RuntimeError):
        production.review_target(run)


def test_merge_has_exact_reviewed_parents_and_recovers_idempotently(repo):
    run = implementation(repo)
    merged = production.merge_reviewed(run, "MAGI reviewed")
    assert production.git(repo, "show", "-s", "--format=%P", merged).split() == [run["base_sha"], run["reviewed_sha"]]
    assert (Path(repo) / "app.txt").read_text() == "implemented\n"
    assert production.git(repo, "status", "--porcelain") == ""
    assert production.git(repo, "symbolic-ref", "--short", "HEAD") == "main"
    assert production.merge_reviewed(run, "retry") == merged


@pytest.mark.parametrize("change", ["base", "branch", "dirty_base", "reviewed", "dirty_reviewed"])
def test_changes_after_review_block_merge_without_touching_user_files(repo, change):
    run = implementation(repo)
    if change == "base":
        (Path(repo) / "app.txt").write_text("conflicting user commit\n")
        production.git(repo, "add", "app.txt")
        production.git(repo, "commit", "-m", "base advanced")
    elif change == "branch":
        production.git(repo, "checkout", "-b", "other")
    elif change == "dirty_base":
        (Path(repo) / "app.txt").write_text("uncommitted user work\n")
    else:
        worktree = run["worktree"]
        (Path(worktree) / "app.txt").write_text("unreviewed change\n")
        if change == "reviewed":
            production.git(worktree, "add", "app.txt")
            production.git(worktree, "commit", "-m", "not reviewed")
    before = production.git(repo, "rev-parse", "HEAD")
    content = (Path(repo) / "app.txt").read_text()
    with pytest.raises(RuntimeError):
        production.merge_reviewed(run, "must not merge")
    assert production.git(repo, "rev-parse", "HEAD") == before
    assert (Path(repo) / "app.txt").read_text() == content
    assert not (Path(repo) / ".git" / "MERGE_HEAD").exists()


def test_empty_implementation_is_a_noop(repo):
    run = production.plan(repo, 42)
    production.prepare(run)
    sha, diff = production.review_target(run)
    assert diff == ""
    run.update(reviewed_sha=sha, review_id=43)
    assert production.merge_reviewed(run, "no changes") == run["base_sha"]


def test_merge_refuses_same_parents_with_unreviewed_tree(repo):
    run = implementation(repo)
    forged = production.git(repo, "commit-tree", f"{run['base_sha']}^{{tree}}",
                            "-p", run["base_sha"], "-p", run["reviewed_sha"], "-m", "wrong tree")
    integration = str(Path(production.common_dir(repo)) / "magi-worktrees" / "merge-43")
    production.git(repo, "worktree", "add", "--detach", integration, forged)
    with pytest.raises(RuntimeError, match="contenido de integración"):
        production.merge_reviewed(run, "must not merge")
    assert production.git(repo, "rev-parse", "HEAD") == run["base_sha"]


def test_detached_user_checkout_is_not_used_as_base(repo):
    production.git(repo, "checkout", "--detach")
    with pytest.raises(RuntimeError):
        production.plan(repo, 42)


def test_merge_preserves_ignored_user_file(repo):
    (Path(repo) / ".gitignore").write_text("local.txt\n")
    production.git(repo, "add", ".gitignore")
    production.git(repo, "commit", "-m", "ignore local data")
    (Path(repo) / "local.txt").write_text("private user data")
    run = production.plan(repo, 42)
    worktree = production.prepare(run)
    (Path(worktree) / "local.txt").write_text("tracked by executor")
    production.git(worktree, "add", "-f", "local.txt")
    production.git(worktree, "commit", "-m", "add ignored path")
    sha, _ = production.review_target(run)
    run.update(reviewed_sha=sha, review_id=43)
    with pytest.raises(RuntimeError):
        production.merge_reviewed(run, "must preserve local data")
    assert (Path(repo) / "local.txt").read_text() == "private user data"
    assert production.git(repo, "rev-parse", "HEAD") == run["base_sha"]


@pytest.fixture
def pg(monkeypatch, tmp_path):
    """Opt-in PostgreSQL coverage. All writes target session-local temp tables."""
    dsn = os.environ.get("CLAMI_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("set CLAMI_TEST_POSTGRES_DSN for isolated PostgreSQL tests")
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row, connect_timeout=5) as conn:
        conn.execute("""
            CREATE TEMP TABLE decisions (
                id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                title text, artifact text, protocol text, thread text UNIQUE,
                heads jsonb, created_by text, production boolean DEFAULT false,
                status text DEFAULT 'open', round int DEFAULT 1, anchor_id bigint,
                ruling text, confidence real, minority_report jsonb,
                created_at timestamptz DEFAULT now(), closed_at timestamptz
            );
            CREATE TEMP TABLE messages (
                id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                thread text, author text, kind text, body text, artifact text,
                created_at timestamptz DEFAULT now()
            );
            CREATE TEMP TABLE positions (
                decision_id bigint, head text, round int, position text,
                conditions jsonb, message_id bigint,
                PRIMARY KEY (decision_id, head, round)
            );
        """)
        # An accidental unqualified name cannot fall back to public tables.
        conn.execute("SET search_path TO pg_temp")
        monkeypatch.setattr(relay, "connect", lambda: nullcontext(conn))
        monkeypatch.setattr(relay, "LOG_DIR", tmp_path)
        monkeypatch.setattr(relay, "event", lambda *a, **kw: None)
        registry = [{"seat": name, "type": "cli", "bin": sys.executable}
                    for name in ["melchior", "balthasar", "casper"]]
        monkeypatch.setattr(heads, "load", lambda: registry)
        script = (
            "from pathlib import Path; import subprocess; "
            "Path('app.txt').write_text('implemented\\n'); "
            "subprocess.run(['git','add','app.txt'],check=True); "
            "subprocess.run(['git','commit','-m','stub implementation'],check=True)"
        )
        monkeypatch.setattr(relay, "executor_seat", lambda: {
            "seat": "melchior", "bin": sys.executable, "args": ["-c", script],
        })
        yield conn


def approve(pg, did, votes=("yes", "yes", "yes")):
    for seat, vote in zip(["melchior", "balthasar", "casper"], votes):
        with pg.transaction():
            board.record_position(pg, did, seat, vote, "test review")


@pytest.mark.parametrize("outcome", ["approved", "majority", "aborted", "changed_base"])
def test_production_pipeline_with_postgres_and_real_git(repo, pg, outcome):
    with pg.transaction():
        opened = board.start_decision(pg, "test plan", artifact=repo, production=True)
    did = opened["decision_id"]
    approve(pg, did)
    d = pg.execute("SELECT * FROM decisions WHERE id = %s", (did,)).fetchone()
    relay._run_executor_turn(d, repo)
    original = pg.execute("SELECT * FROM decisions WHERE id = %s", (did,)).fetchone()
    assert original["minority_report"]["execution_state"] == "reviewing"
    run = original["minority_report"]["execution"]
    rev = pg.execute("SELECT * FROM decisions WHERE id = %s", (run["review_id"],)).fetchone()
    assert rev["artifact"] == run["worktree"]
    assert (Path(repo) / "app.txt").read_text() == "base\n"
    assert relay._ejecucion_gestionada(pg, original["thread"])
    # Review identity is structural; editing its title cannot change the target.
    pg.execute("UPDATE decisions SET title = 'renamed review' WHERE id = %s", (rev["id"],))
    approve(pg, rev["id"], ("yes", "yes", "no") if outcome == "majority" else ("yes",) * 3)
    if outcome == "aborted":
        with pg.transaction():
            board.abort_decision(pg, did)
    if outcome == "changed_base":
        production.git(repo, "commit", "--allow-empty", "-m", "base advanced")
    relay._maybe_merge_reviews(pg)
    count = pg.execute("SELECT count(*) AS n FROM messages").fetchone()["n"]
    relay._maybe_merge_reviews(pg)
    assert pg.execute("SELECT count(*) AS n FROM messages").fetchone()["n"] == count
    updated = pg.execute("SELECT * FROM decisions WHERE id = %s", (did,)).fetchone()
    if outcome == "approved":
        assert updated["status"] == "closed"
        assert updated["minority_report"]["execution_state"] == "merged"
        assert (Path(repo) / "app.txt").read_text() == "implemented\n"
    else:
        assert (Path(repo) / "app.txt").read_text() == "base\n"
        if outcome != "aborted":
            assert updated["minority_report"]["execution_state"] == "merge_blocked"


def test_failed_attempt_requires_explicit_retry_in_postgres(repo, pg):
    with pg.transaction():
        opened = board.start_decision(pg, "test plan", artifact=repo, production=True)
    approve(pg, opened["decision_id"])
    d = pg.execute("SELECT * FROM decisions WHERE id = %s", (opened["decision_id"],)).fetchone()
    relay._execution_failed(d, "simulated failure")
    assert relay._ejecucion_gestionada(pg, d["thread"])
    with pg.transaction():
        board.human_message(pg, d["thread"], "context only")
    assert relay._ejecucion_gestionada(pg, d["thread"])
    with pg.transaction():
        board.human_message(pg, d["thread"], "seguí")
    assert not relay._ejecucion_gestionada(pg, d["thread"])
    relay._run_executor_turn(d, repo)
    assert relay._ejecucion_gestionada(pg, d["thread"])
    failures = pg.execute("SELECT body FROM messages WHERE body LIKE 'EJECUCIÓN FALLIDA%'").fetchall()
    assert len(failures) == 1


def test_repo_lease_blocks_another_executor(repo, pg):
    import psycopg
    opened = board.start_decision(pg, "test lock", artifact=repo, production=True)
    approve(pg, opened["decision_id"])
    d = pg.execute("SELECT * FROM decisions WHERE id = %s", (opened["decision_id"],)).fetchone()
    key = os.path.normcase(production.common_dir(repo))
    with psycopg.connect(os.environ["CLAMI_TEST_POSTGRES_DSN"], autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (key,))
        relay._run_executor_turn(d, repo)
        assert not (Path(production.common_dir(repo)) / "magi-worktrees").exists()
    relay._run_executor_turn(d, repo)
    updated = pg.execute("SELECT * FROM decisions WHERE id = %s", (d["id"],)).fetchone()
    assert updated["minority_report"]["execution_state"] == "reviewing"
