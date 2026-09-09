"""Invoke Maven/Tycho for a build plan, with a dry-run mode.

Two Tycho-specific details drive the design here:

1. **Partial reactors need previously built artifacts.**  When only a subset of
   projects is built, the ones that are skipped must still be resolvable.  The
   default goal is therefore ``install`` (not ``verify``) so each build leaves
   its bundles in the local Maven repository, where Tycho's
   ``pomDependencies=consider`` / local-artifact resolution can find them on
   the next partial run.

2. **``-pl`` order is advisory.**  Maven and Tycho re-sort the reactor
   themselves.  Passing the list in our computed order keeps the log readable
   and makes tie-breaks deterministic, but correctness comes from passing the
   *complete* impact closure -- which is what the planner guarantees.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .plan import BuildPlan

DEFAULT_GOALS = ("clean", "install")


@dataclass
class BuildResult:
    command: list[str]
    exit_code: int
    duration_seconds: float
    dry_run: bool
    log_path: Path | None = None
    skipped_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.skipped_reason is not None or self.exit_code == 0


@dataclass
class MavenOptions:
    goals: tuple[str, ...] = DEFAULT_GOALS
    threads: str | None = None
    offline: bool = False
    batch_mode: bool = True
    extra_args: list[str] = field(default_factory=list)
    maven_executable: str | None = None


def find_maven(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit if shutil.which(explicit) or Path(explicit).is_file() else None
    for candidate in ("mvn", "mvnw", "./mvnw"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def build_command(plan: BuildPlan, options: MavenOptions) -> list[str]:
    executable = find_maven(options.maven_executable) or "mvn"
    command = [executable, *options.goals]
    if options.batch_mode:
        command.append("--batch-mode")
    if options.offline:
        command.append("--offline")
    if options.threads:
        command.extend(["-T", options.threads])
    if plan.scenario == "changed":
        # Explicit closure; -am/-amd are deliberately NOT used because Maven
        # derives them from POM dependencies, which this product does not have.
        command.extend(["-pl", ",".join(plan.module_paths)])
    command.extend(options.extra_args)
    return command


def run(
    plan: BuildPlan,
    options: MavenOptions,
    dry_run: bool,
    log_path: Path | None = None,
    echo=print,
) -> BuildResult:
    command = build_command(plan, options)

    if not plan.entries:
        echo("  nothing to build -- no module was affected")
        return BuildResult(command, 0, 0.0, dry_run, skipped_reason="empty plan")

    if dry_run:
        echo("  DRY RUN -- the command below was not executed:")
        echo("    " + " ".join(command))
        return BuildResult(command, 0, 0.0, True, skipped_reason="dry run")

    executable = find_maven(options.maven_executable)
    if executable is None:
        echo("  ERROR: no Maven executable on PATH; re-run with --dry-run or set --maven")
        return BuildResult(command, 127, 0.0, False)

    echo("  executing: " + " ".join(command))
    started = time.monotonic()
    handle = log_path.open("w", encoding="utf-8") if log_path else None
    try:
        process = subprocess.Popen(
            command,
            cwd=plan.reactor.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, "MAVEN_OPTS": os.environ.get("MAVEN_OPTS", "")},
        )
        assert process.stdout is not None
        for line in process.stdout:
            stripped = line.rstrip("\n")
            echo("  | " + stripped)
            if handle:
                handle.write(line)
        exit_code = process.wait()
    finally:
        if handle:
            handle.close()

    duration = time.monotonic() - started
    return BuildResult(command, exit_code, duration, False, log_path)
