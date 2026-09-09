"""Change detection.

Two interchangeable strategies, chosen automatically:

**git**   -- when the reactor sits inside a git work tree.  Diffs against a
            configurable ref and folds in staged, unstaged and untracked files
            so a developer's working copy is handled the same way CI is.

**hash**  -- when there is no repository.  A SHA-256 fingerprint of every
            tracked-looking file is stored in a state file; a later run
            compares against it.  Requires no VCS at all.

Both strategies emit the same :class:`~omsbuild.model.FileChange` records, and
both classify each file so the planner can distinguish "a Java file changed"
(rebuild this bundle and its consumers) from "the parent POM changed" (every
project is suspect).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .model import FileChange
from .scan import Reactor

DEFAULT_STATE_FILE = ".oms-build-state.json"

#: Generated output and IDE noise: never a reason to rebuild.
IGNORED_PATTERNS = (
    "*/target/*",
    "target/*",
    "*/bin/*",
    "bin/*",
    ".git/*",
    "*/.git/*",
    "*.class",
    "*/.settings/*",
    "*.log",
    "build-pipeline/*",
    "build-reports/*",
    DEFAULT_STATE_FILE,
    "*/" + DEFAULT_STATE_FILE,
)

#: Files that invalidate the whole reactor when touched.
GLOBAL_PATHS = ("pom.xml",)


def is_ignored(rel_path: str) -> bool:
    return any(fnmatch.fnmatch(rel_path, pattern) for pattern in IGNORED_PATTERNS)


def classify(rel_path: str) -> str:
    name = rel_path.rsplit("/", 1)[-1]
    if name == "MANIFEST.MF":
        return "manifest"
    if name == "feature.xml":
        return "feature"
    if name == "pom.xml":
        return "pom"
    if name == "build.properties":
        return "build-properties"
    if rel_path.endswith((".java", ".properties", ".xml", ".json", ".txt")):
        return "source"
    return "other"


@dataclass
class ChangeSet:
    """The result of change detection."""

    strategy: str
    baseline: str
    changes: list[FileChange] = field(default_factory=list)
    global_change: str | None = None
    unowned: list[str] = field(default_factory=list)

    @property
    def changed_project_ids(self) -> set[str]:
        return {change.owner_id for change in self.changes if change.owner_id}

    def files_for(self, project_id: str) -> list[FileChange]:
        return [change for change in self.changes if change.owner_id == project_id]


# ---------------------------------------------------------------------------
# git strategy
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> tuple[int, str]:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stdout.strip()


def is_git_repo(root: Path) -> bool:
    code, output = _git(root, "rev-parse", "--is-inside-work-tree")
    return code == 0 and output == "true"


def _git_prefix(root: Path) -> str:
    """Path of ``root`` relative to the repo top level, if nested."""
    code, output = _git(root, "rev-parse", "--show-prefix")
    return output if code == 0 else ""


def resolve_base(root: Path, ref: str) -> str:
    """Prefer the merge base of ``ref`` and HEAD over ``ref`` itself.

    On a pull request, diffing straight against ``origin/main`` also reports
    everything that landed on main since the branch was cut, which inflates the
    rebuild set. The merge base gives "what this branch actually changed".
    ``HEAD`` is left alone so that a plain working-copy diff still works.
    """
    if ref == "HEAD":
        return ref
    code, output = _git(root, "merge-base", ref, "HEAD")
    return output if code == 0 and output else ref


def detect_git_changes(reactor: Reactor, ref: str) -> ChangeSet:
    root = reactor.root
    prefix = _git_prefix(root)
    seen: dict[str, str] = {}

    code, _ = _git(root, "rev-parse", "--verify", ref)
    if code != 0:
        raise ValueError(
            f"git ref {ref!r} does not exist; pass --ref or use --strategy hash"
        )

    base = resolve_base(root, ref)
    label = ref if base == ref else f"{ref} (merge-base {base[:12]})"
    change_set = ChangeSet(strategy="git", baseline=label)

    # Committed differences between the base and HEAD, plus the working tree.
    code, output = _git(root, "diff", "--name-status", base, "--", ".")
    if code == 0:
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status, path = parts[0].strip(), parts[-1].strip()
            seen.setdefault(path, status)

    # Staged, unstaged and untracked files in the working copy.
    code, output = _git(root, "status", "--porcelain", "--", ".")
    if code == 0:
        for line in output.splitlines():
            if len(line) < 4:
                continue
            status = line[:2].strip() or "M"
            path = line[3:].strip()
            if " -> " in path:  # rename
                path = path.split(" -> ", 1)[1]
            path = path.strip('"')
            seen[path] = "?" if status == "??" else status

    for repo_path, status in sorted(seen.items()):
        rel_path = repo_path[len(prefix) :] if prefix and repo_path.startswith(prefix) else repo_path
        if is_ignored(rel_path):
            continue
        _record(reactor, change_set, rel_path, status)

    return change_set


# ---------------------------------------------------------------------------
# hash strategy
# ---------------------------------------------------------------------------


def _fingerprint(reactor: Reactor) -> dict[str, str]:
    digests: dict[str, str] = {}
    for path in sorted(reactor.root.rglob("*")):
        if not path.is_file():
            continue
        rel_path = path.relative_to(reactor.root).as_posix()
        if is_ignored(rel_path):
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        digests[rel_path] = digest
    return digests


def write_baseline(reactor: Reactor, state_path: Path) -> int:
    digests = _fingerprint(reactor)
    state_path.write_text(
        json.dumps({"version": 1, "files": digests}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return len(digests)


def detect_hash_changes(reactor: Reactor, state_path: Path) -> ChangeSet:
    change_set = ChangeSet(strategy="hash", baseline=state_path.name)
    current = _fingerprint(reactor)

    if not state_path.is_file():
        change_set.global_change = (
            f"no baseline at {state_path.name}; treating every module as changed"
        )
        for rel_path in sorted(current):
            _record(reactor, change_set, rel_path, "A")
        return change_set

    previous = json.loads(state_path.read_text(encoding="utf-8")).get("files", {})
    for rel_path, digest in sorted(current.items()):
        if rel_path not in previous:
            _record(reactor, change_set, rel_path, "A")
        elif previous[rel_path] != digest:
            _record(reactor, change_set, rel_path, "M")
    for rel_path in sorted(set(previous) - set(current)):
        _record(reactor, change_set, rel_path, "D")

    return change_set


# ---------------------------------------------------------------------------
# shared
# ---------------------------------------------------------------------------


def _record(reactor: Reactor, change_set: ChangeSet, rel_path: str, status: str) -> None:
    owner = reactor.owner_of_path(rel_path)
    owner_id = owner.id if owner is not None else None
    category = classify(rel_path)

    if rel_path in GLOBAL_PATHS or (owner is not None and not owner.is_buildable):
        change_set.global_change = (
            f"{rel_path} is shared by the whole reactor ({category}); "
            "a full rebuild is required"
        )

    if owner_id is None:
        change_set.unowned.append(rel_path)

    change_set.changes.append(
        FileChange(rel_path=rel_path, status=status, owner_id=owner_id, category=category)
    )


def detect(
    reactor: Reactor,
    strategy: str = "auto",
    ref: str = "HEAD",
    state_path: Path | None = None,
) -> ChangeSet:
    """Detect changes using the requested (or best available) strategy."""
    resolved_state = state_path or (reactor.root / DEFAULT_STATE_FILE)
    if strategy == "auto":
        strategy = "git" if is_git_repo(reactor.root) else "hash"
    if strategy == "git":
        if not is_git_repo(reactor.root):
            raise ValueError(f"{reactor.root} is not inside a git work tree")
        return detect_git_changes(reactor, ref)
    if strategy == "hash":
        return detect_hash_changes(reactor, resolved_state)
    raise ValueError(f"unknown change-detection strategy: {strategy}")
