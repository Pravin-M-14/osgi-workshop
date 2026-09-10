"""GitHub Actions integration: job matrices, step outputs, and job summaries.

The workflow itself stays declarative -- it never parses OSGi metadata.  This
module turns a :class:`~omsbuild.plan.BuildPlan` into the three things Actions
needs:

* **step outputs** consumed by ``needs.plan.outputs.*`` -- notably one job
  matrix per dependency wave, because Actions cannot create a dynamic *chain*
  of jobs (only a dynamic fan-out).  The workflow therefore declares a fixed
  ladder of wave jobs and each one skips itself when its matrix is empty.
* **a job summary** in Markdown, including a Mermaid graph.  GitHub renders
  Mermaid natively in job summaries, so the dependency graph shows up as a
  picture on the run page with no artifact download.
* **a PR comment body**, a condensed version of the same thing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .model import ChangeReason, Kind
from .plan import BuildPlan
from .render import to_mermaid

#: Length of the fixed wave-job ladder declared in the workflow.  The current
#: product resolves to five waves; six gives headroom.  A plan deeper than this
#: is still built correctly -- the overflow waves are handed to a final job
#: that builds them in order on a single runner.
MAX_WAVES = 6


#: Exactly the keys the wave jobs read out of ``matrix``. Every key placed in a
#: matrix entry becomes part of that job's identity and its auto-generated
#: display name in the Actions UI, so carrying fields nobody consumes is not
#: free -- it is dead surface in the contract between the resolver and the
#: workflow. ``MatrixContractTests`` checks this set against the ``matrix.*``
#: references in the YAML, so adding a key here without using it, or using one
#: in the workflow without emitting it, both fail loudly.
MATRIX_KEYS = ("module", "short", "path")


def _matrix_for(plan: BuildPlan, wave_index: int) -> dict:
    """Matrix entries for one wave: one entry per module in that wave."""
    if wave_index > len(plan.waves):
        return {"include": []}
    include = []
    for project_id in plan.waves[wave_index - 1]:
        project = plan.graph.nodes[project_id]
        include.append(
            {
                "module": project_id,
                "short": project.short_id,
                "path": project.rel_path,
            }
        )
    return {"include": include}


def _overflow_paths(plan: BuildPlan) -> list[str]:
    """Module paths for every wave beyond the fixed ladder, in order."""
    paths: list[str] = []
    for wave in plan.waves[MAX_WAVES:]:
        for project_id in wave:
            paths.append(plan.graph.nodes[project_id].rel_path)
    return paths


def outputs(plan: BuildPlan) -> dict[str, str]:
    """Every step output the workflow consumes."""
    total = len(plan.graph.nodes)
    selected = len(plan.entries)

    result: dict[str, str] = {
        "scenario": plan.scenario,
        "has_work": "true" if plan.entries else "false",
        "module_total": str(total),
        "module_count": str(selected),
        "changed_count": str(len(plan.changed_ids)),
        "skipped_count": str(len(plan.skipped_ids)),
        "wave_count": str(len(plan.waves)),
        "ladder_waves": str(min(len(plan.waves), MAX_WAVES)),
        "cycles": "true" if plan.cycles else "false",
        "full_rebuild_reason": plan.full_rebuild_reason or "",
        "all_module_paths": ",".join(plan.module_paths),
        "changed_modules": " ".join(sorted(plan.changed_ids)),
        "components": ",".join(plan.selected_components),
    }

    for wave_index in range(1, MAX_WAVES + 1):
        matrix = _matrix_for(plan, wave_index)
        result[f"wave{wave_index}_matrix"] = json.dumps(matrix, separators=(",", ":"))
        result[f"wave{wave_index}_has_work"] = "true" if matrix["include"] else "false"
        result[f"wave{wave_index}_paths"] = ",".join(
            entry["path"] for entry in matrix["include"]
        )

    overflow = _overflow_paths(plan)
    result["overflow_paths"] = ",".join(overflow)
    result["overflow_has_work"] = "true" if overflow else "false"

    return result


def write_outputs(plan: BuildPlan, output_file: Path | None = None) -> Path | None:
    """Append step outputs to ``$GITHUB_OUTPUT`` (multiline-safe heredocs)."""
    target = output_file or (
        Path(os.environ["GITHUB_OUTPUT"]) if os.environ.get("GITHUB_OUTPUT") else None
    )
    if target is None:
        return None
    with target.open("a", encoding="utf-8") as handle:
        for key, value in outputs(plan).items():
            if "\n" in value:
                handle.write(f"{key}<<__OMS_EOF__\n{value}\n__OMS_EOF__\n")
            else:
                handle.write(f"{key}={value}\n")
    return target


# ---------------------------------------------------------------------------
# job summary
# ---------------------------------------------------------------------------


def summary_markdown(plan: BuildPlan, run_url: str = "") -> str:
    total = len(plan.graph.nodes)
    selected = len(plan.entries)
    skipped = len(plan.skipped_ids)
    heading = (
        "Scenario 1 — build all products"
        if plan.scenario == "full"
        else "Scenario 2 — build only changed products"
    )

    lines: list[str] = [f"## {heading}", ""]

    if plan.scenario == "changed" and total:
        avoided = f"{skipped / total * 100:.0f}%"
        lines += [
            "| | |",
            "|---|---|",
            f"| Modules in product | **{total}** |",
            f"| Directly changed | **{len(plan.changed_ids)}** |",
            f"| Selected to rebuild | **{selected}** |",
            f"| Skipped | **{skipped}** ({avoided} of the reactor avoided) |",
            f"| Build waves | **{len(plan.waves)}** |",
            "",
        ]
    else:
        lines += [
            "| | |",
            "|---|---|",
            f"| Modules in product | **{total}** |",
            f"| Build waves | **{len(plan.waves)}** |",
            "",
        ]

    if plan.full_rebuild_reason:
        lines += [
            f"> [!WARNING]",
            f"> Escalated to a full rebuild: {plan.full_rebuild_reason}",
            "",
        ]

    if plan.cycles:
        lines += ["> [!CAUTION]", "> Dependency cycles detected:"]
        for cycle in plan.cycles:
            lines.append(f"> - `{' -> '.join(cycle)}`")
        lines.append("")

    # Detected changes
    if plan.change_set is not None:
        lines += ["### Detected changes", ""]
        if not plan.change_set.changes:
            lines += ["_No changes detected._", ""]
        else:
            lines += [
                f"Strategy `{plan.change_set.strategy}`, baseline "
                f"`{plan.change_set.baseline}`.",
                "",
                "| Status | File | Owning module |",
                "|---|---|---|",
            ]
            for change in plan.change_set.changes[:60]:
                owner = change.owner_id or "_unowned_"
                lines.append(f"| `{change.status}` | `{change.rel_path}` | `{owner}` |")
            if len(plan.change_set.changes) > 60:
                lines.append(f"| … | _{len(plan.change_set.changes) - 60} more_ | |")
            lines.append("")

    # Impact paths
    if plan.impact_paths:
        lines += ["### Dependency chain resolution", ""]
        for seed, paths in sorted(plan.impact_paths.items()):
            short = plan.graph.nodes[seed].short_id
            lines += [f"<details><summary><code>{short}</code> changed &rarr; "
                      f"{len(paths)} module(s) impacted</summary>", ""]
            lines += ["| Impacted module | Dependency path | Hops |", "|---|---|---|"]
            for target, chain in sorted(paths.items()):
                pretty = " → ".join(plan.graph.nodes[step].short_id for step in chain)
                lines.append(f"| `{target}` | {pretty} | {len(chain) - 1} |")
            lines += ["", "</details>", ""]

    # Build order
    lines += ["### Build order", ""]
    if not plan.entries:
        lines += ["_Nothing to build._", ""]
    for wave_index, wave in enumerate(plan.waves, start=1):
        names = []
        for project_id in wave:
            entry = plan.entry(project_id)
            marker = (
                " 🔴"
                if entry and entry.reason is ChangeReason.DIRECTLY_CHANGED
                else ""
            )
            kind = "📦" if plan.graph.nodes[project_id].kind is Kind.BUNDLE else "🧩"
            names.append(f"{kind} `{plan.graph.nodes[project_id].short_id}`{marker}")
        lines.append(f"**Wave {wave_index}** &nbsp; {' &nbsp;·&nbsp; '.join(names)}")
    lines += ["", "🔴 directly changed &nbsp; 📦 bundle &nbsp; 🧩 feature", ""]

    if plan.scenario == "changed" and plan.skipped_ids:
        skipped_list = ", ".join(
            f"`{plan.graph.nodes[node].short_id}`" for node in sorted(plan.skipped_ids)
        )
        lines += [
            "<details><summary>Modules deliberately not rebuilt</summary>",
            "",
            skipped_list,
            "",
            "No dependency edge reaches these modules from the change set, so their "
            "existing artifacts remain valid.",
            "",
            "</details>",
            "",
        ]

    # Graph -- GitHub renders Mermaid natively in job summaries.
    lines += ["### Dependency graph", "", "```mermaid", to_mermaid(plan, heading).rstrip(), "```", ""]

    # Component view
    component_order = " → ".join(
        "[" + ", ".join(wave) + "]" for wave in plan.component_waves
    )
    if component_order:
        lines += [f"**Component build order:** {component_order}", ""]

    if plan.reactor.validations:
        lines += [
            "<details><summary>"
            f"Product consistency findings ({len(plan.reactor.validations)})</summary>",
            "",
            "| Severity | Where | Finding |",
            "|---|---|---|",
        ]
        for validation in plan.reactor.validations:
            lines.append(
                f"| {validation.severity} | `{validation.project_id}` | {validation.message} |"
            )
        lines += ["", "</details>", ""]

    if run_url:
        lines.append(f"[Full HTML report is attached to this run as an artifact.]({run_url})")

    return "\n".join(lines) + "\n"


def write_summary(plan: BuildPlan, summary_file: Path | None = None, run_url: str = "") -> Path | None:
    target = summary_file or (
        Path(os.environ["GITHUB_STEP_SUMMARY"])
        if os.environ.get("GITHUB_STEP_SUMMARY")
        else None
    )
    if target is None:
        return None
    with target.open("a", encoding="utf-8") as handle:
        handle.write(summary_markdown(plan, run_url))
    return target


# ---------------------------------------------------------------------------
# PR comment
# ---------------------------------------------------------------------------

COMMENT_MARKER = "<!-- oms-build-pipeline -->"


def comment_markdown(plan: BuildPlan, run_url: str = "") -> str:
    total = len(plan.graph.nodes)
    selected = len(plan.entries)
    skipped = len(plan.skipped_ids)
    avoided = f"{skipped / total * 100:.0f}%" if total else "0%"

    lines = [
        COMMENT_MARKER,
        "### 🧩 OMS smart build pipeline",
        "",
        f"Change detection resolved **{len(plan.changed_ids)}** directly changed module(s) "
        f"into **{selected}** of **{total}** modules to rebuild "
        f"across **{len(plan.waves)}** wave(s) — skipping **{skipped}** ({avoided} of the reactor).",
        "",
    ]

    if plan.full_rebuild_reason:
        lines += [f"> ⚠️ Escalated to a full rebuild: {plan.full_rebuild_reason}", ""]

    if plan.changed_ids:
        lines += ["**Changed modules**", ""]
        for project_id in sorted(plan.changed_ids):
            impacted = len(plan.impact_paths.get(project_id, {}))
            lines.append(
                f"- `{plan.graph.nodes[project_id].short_id}` → {impacted} downstream module(s)"
            )
        lines.append("")

    lines += ["**Build order**", ""]
    for wave_index, wave in enumerate(plan.waves, start=1):
        names = ", ".join(f"`{plan.graph.nodes[node].short_id}`" for node in wave)
        lines.append(f"{wave_index}. {names}")
    lines.append("")

    lines += ["```mermaid", to_mermaid(plan, "changed-modules impact").rstrip(), "```", ""]

    if run_url:
        lines.append(f"[Dependency graph and full logs →]({run_url})")

    return "\n".join(lines) + "\n"
