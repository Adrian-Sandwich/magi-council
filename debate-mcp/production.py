"""Git operations for isolated executions and immutable review targets.

Worktrees are retained after success or failure for inspection. No cleanup
operation discards user files, and merging never checks out another branch.
"""

from pathlib import Path
import subprocess


def git(repo: str, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )
    if result.returncode:
        raise RuntimeError(f"git {args[0]}: {(result.stderr or result.stdout).strip()}")
    return result.stdout.strip()


def repository(repo: str) -> str:
    return str(Path(git(repo, "rev-parse", "--show-toplevel")).resolve())


def common_dir(repo: str) -> str:
    return str(Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve())


def plan(repo: str, decision_id: int, previous: dict | None = None) -> dict:
    """Capture the base once, before creating or resuming an execution."""
    repo = repository(repo)
    if previous:
        if previous["repo"] != repo:
            raise RuntimeError("el repositorio cambió desde el primer intento")
        return dict(previous)
    base = git(repo, "symbolic-ref", "--short", "HEAD")
    sha = git(repo, "rev-parse", "HEAD")
    worktree = str(Path(common_dir(repo)) / "magi-worktrees" / f"d{decision_id}")
    return dict(repo=repo, base_branch=base, base_sha=sha,
                branch=f"magi/d{decision_id}", worktree=worktree)


def prepare(run: dict) -> str:
    """Create only our own branch, or resume the recorded worktree as-is."""
    repo, worktree, branch = run["repo"], run["worktree"], run["branch"]
    if Path(worktree).exists():
        if repository(worktree) != str(Path(worktree).resolve()):
            raise RuntimeError("el directorio de ejecución no es un worktree")
        if common_dir(worktree) != common_dir(repo):
            raise RuntimeError("el worktree pertenece a otro repositorio")
        if git(worktree, "symbolic-ref", "--short", "HEAD") != branch:
            raise RuntimeError("la rama del worktree cambió; requiere inspección")
    else:
        # -b refuses an existing branch instead of adopting unrelated work.
        git(repo, "worktree", "add", "-b", branch, worktree, run["base_sha"])
    git(worktree, "merge-base", "--is-ancestor", run["base_sha"], "HEAD")
    return worktree


def review_target(run: dict) -> tuple[str, str]:
    worktree = run["worktree"]
    if git(worktree, "symbolic-ref", "--short", "HEAD") != run["branch"]:
        raise RuntimeError("el ejecutor cambió de rama")
    if git(worktree, "status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError("el ejecutor dejó cambios sin commit; no se abre revisión")
    sha = git(worktree, "rev-parse", "HEAD")
    git(worktree, "merge-base", "--is-ancestor", run["base_sha"], sha)
    diff = git(worktree, "diff", "--no-ext-diff", "--no-textconv", run["base_sha"], sha, "--")
    return sha, diff


def commit_execution(run: dict, message: str) -> str:
    """Record an executor's workspace edits without giving the model Git access."""
    worktree = run["worktree"]
    if git(worktree, "symbolic-ref", "--short", "HEAD") != run["branch"]:
        raise RuntimeError("el ejecutor cambió de rama")
    git(worktree, "merge-base", "--is-ancestor", run["base_sha"], "HEAD")
    if not git(worktree, "status", "--porcelain", "--untracked-files=all"):
        sha = git(worktree, "rev-parse", "HEAD")
        if sha == run["base_sha"]:
            raise RuntimeError("el ejecutor terminó sin producir cambios")
        return sha  # compatible with executors that can and do commit themselves
    git(worktree, "add", "-A")
    if not git(worktree, "diff", "--cached", "--name-only"):
        raise RuntimeError("los cambios del ejecutor están ignorados y no pueden revisarse")
    git(worktree, "-c", "core.hooksPath=", "-c", "user.name=MAGI Executor", "-c",
        "user.email=magi@localhost", "commit", "--no-gpg-sign", "-m", message)
    return git(worktree, "rev-parse", "HEAD")


def merge_reviewed(run: dict, message: str) -> str:
    """Merge exactly the approved SHA, only into the unchanged clean base.

    A detached integration worktree absorbs conflicts. The original checkout
    is touched only for the final fast-forward; Git refuses dirty collisions.
    """
    repo, base, source = run["repo"], run["base_sha"], run["reviewed_sha"]
    if git(repo, "symbolic-ref", "--short", "HEAD") != run["base_branch"]:
        raise RuntimeError("la rama activa cambió; merge automático detenido")
    if git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError("el repositorio tiene cambios locales; merge automático detenido")
    current = git(repo, "rev-parse", "HEAD")
    # Recovery after Git succeeded but recording the outcome failed.
    if current != base:
        parents = git(repo, "show", "-s", "--format=%P", current).split()
        if parents == [base, source] and git(repo, "rev-parse", f"{current}^{{tree}}") == git(repo, "rev-parse", f"{source}^{{tree}}"):
            return current
        raise RuntimeError("la base avanzó después del plan; requiere nueva revisión")
    if git(run["worktree"], "rev-parse", "HEAD") != source:
        raise RuntimeError("el worktree cambió después de la revisión")
    if git(run["worktree"], "status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError("hay cambios sin revisar en el worktree")
    git(repo, "merge-base", "--is-ancestor", base, source)
    if source == base:
        return base
    integration = str(Path(common_dir(repo)) / "magi-worktrees" / f"merge-{run['review_id']}")
    if not Path(integration).exists():
        git(repo, "worktree", "add", "--detach", integration, base)
    if common_dir(integration) != common_dir(repo):
        raise RuntimeError("el worktree de integración pertenece a otro repo")
    if repository(integration) != str(Path(integration).resolve()):
        raise RuntimeError("el directorio de integración no es un worktree")
    if git(integration, "rev-parse", "--abbrev-ref", "HEAD") != "HEAD":
        raise RuntimeError("el worktree de integración debe estar en detached HEAD")
    if git(integration, "status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError("el worktree de integración tiene cambios; requiere inspección")
    head = git(integration, "rev-parse", "HEAD")
    if head == base:
        git(integration, "-c", "core.hooksPath=", "-c",
            "user.name=MAGI Integrator", "-c", "user.email=magi@localhost",
            "merge", "--no-ff", "--no-edit", "--no-gpg-sign", source, "-m", message)
        head = git(integration, "rev-parse", "HEAD")
    if git(integration, "show", "-s", "--format=%P", head).split() != [base, source]:
        raise RuntimeError("el commit de integración no corresponde a la revisión")
    if git(integration, "rev-parse", f"{head}^{{tree}}") != git(integration, "rev-parse", f"{source}^{{tree}}"):
        raise RuntimeError("el contenido de integración difiere del commit revisado")
    # Recheck after potentially slow merge work. Never switch user branches.
    if git(repo, "symbolic-ref", "--short", "HEAD") != run["base_branch"] or git(repo, "rev-parse", "HEAD") != base:
        raise RuntimeError("la rama base cambió durante la integración")
    if git(repo, "status", "--porcelain", "--untracked-files=all"):
        raise RuntimeError("aparecieron cambios locales durante la integración")
    git(repo, "-c", "core.hooksPath=", "merge", "--ff-only", "--no-edit", "--no-overwrite-ignore", head)
    return head


def cleanup(run: dict) -> list[str]:
    """Después de integrar: borra el worktree del plan, el de integración y la
    rama del plan. La corrida de aceptación dejó 22 worktrees en el repo de
    prueba porque nadie los podaba. El merge ya está en la rama base; si algo
    de esto falla, el llamador lo anota y el directorio queda para inspección."""
    repo = run["repo"]
    removed = []
    integration = (str(Path(common_dir(repo)) / "magi-worktrees" / f"merge-{run['review_id']}")
                   if run.get("review_id") else None)
    for path in (run.get("worktree"), integration):
        if path and Path(path).exists():
            git(repo, "worktree", "remove", "--force", path)
            removed.append(path)
    git(repo, "worktree", "prune")
    branch = run.get("branch")
    if branch and git(repo, "branch", "--list", branch):
        git(repo, "branch", "-d", branch)
    return removed
