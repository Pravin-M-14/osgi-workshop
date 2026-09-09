"""Render the dependency graph and build plan: DOT/SVG, Mermaid, HTML, ASCII, JSON."""

from __future__ import annotations

import html
import json
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path

from .graph import DependencyGraph
from .model import ChangeReason, EdgeKind, Kind
from .plan import BuildPlan

# Node palette, keyed by the role a project plays in the scenario.
COLOURS = {
    "changed": ("#b3261e", "#fde7e5"),  # directly changed
    "dependent": ("#b26a00", "#fff3e0"),  # rebuilt because something upstream changed
    "global": ("#6a1b9a", "#f3e5f5"),  # rebuilt because a shared file changed
    "skipped": ("#9aa0a6", "#f5f5f5"),  # not rebuilt
    "normal": ("#1a56b0", "#e8f0fe"),  # full-build view
}

#: Maps a rebuild reason onto the CSS class used for its badge.
_REASON_TAG = {
    ChangeReason.DIRECTLY_CHANGED: "changed",
    ChangeReason.DEPENDENT: "dependent",
    ChangeReason.GLOBAL: "global",
    ChangeReason.FULL_BUILD: "bundle",
}

EDGE_STYLE = {
    EdgeKind.IMPORT_PACKAGE: ("solid", "#3c4043"),
    EdgeKind.REQUIRE_BUNDLE: ("bold", "#0b6b3a"),
    EdgeKind.DYNAMIC_IMPORT: ("dotted", "#5f6368"),
    EdgeKind.FEATURE_REQUIRES: ("dashed", "#7b1fa2"),
    EdgeKind.FEATURE_INCLUDES: ("dashed", "#00695c"),
    EdgeKind.FEATURE_PLUGIN: ("dotted", "#8a6d00"),
    EdgeKind.PARENT_POM: ("dotted", "#9aa0a6"),
}


def has_graphviz() -> bool:
    return shutil.which("dot") is not None


# ---------------------------------------------------------------------------
# roles
# ---------------------------------------------------------------------------


def node_roles(plan: BuildPlan) -> dict[str, str]:
    """Classify every node for colouring purposes."""
    roles: dict[str, str] = {}
    for node_id in plan.graph.nodes:
        if plan.scenario == "full":
            roles[node_id] = "normal"
            continue
        if node_id in plan.changed_ids:
            roles[node_id] = "changed"
        elif node_id in plan.selected_ids:
            entry = plan.entry(node_id)
            roles[node_id] = (
                "global" if entry and entry.reason is ChangeReason.GLOBAL else "dependent"
            )
        else:
            roles[node_id] = "skipped"
    return roles


# ---------------------------------------------------------------------------
# Graphviz
# ---------------------------------------------------------------------------


def to_dot(plan: BuildPlan, title: str, cluster_by_component: bool = True) -> str:
    graph = plan.graph
    roles = node_roles(plan)
    waves = {entry.project_id: entry.wave for entry in plan.entries}

    lines: list[str] = [
        "digraph NorthwindOMS {",
        "  rankdir=BT;",  # dependencies at the bottom, consumers above
        "  splines=spline;",
        "  bgcolor=\"white\";",
        f'  label=<<b>{html.escape(title)}</b><br/><font point-size="10">'
        "arrow points from a module to the module it depends on; "
        "lower rows are built first</font>>;",
        "  labelloc=t;",
        "  fontname=\"Helvetica\";",
        "  fontsize=16;",
        '  node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=10, penwidth=1.4];',
        '  edge [fontname="Helvetica", fontsize=8, arrowsize=0.7];',
    ]

    grouped: dict[str, list[str]] = {}
    for node_id, project in graph.nodes.items():
        grouped.setdefault(project.component, []).append(node_id)

    def node_line(node_id: str) -> str:
        project = graph.nodes[node_id]
        role = roles[node_id]
        outline, fill = COLOURS[role]
        wave = waves.get(node_id)
        badge = f"wave {wave}" if wave else "not rebuilt"
        shape = "box" if project.kind is Kind.BUNDLE else "box3d"
        style = "rounded,filled" if project.kind is Kind.BUNDLE else "filled"
        if role == "skipped":
            style += ",dashed"
        label = (
            f'<<b>{html.escape(project.short_id)}</b><br/>'
            f'<font point-size="8">{html.escape(str(project.kind))} &middot; {badge}</font>>'
        )
        return (
            f'    "{node_id}" [label={label}, shape={shape}, style="{style}", '
            f'color="{outline}", fillcolor="{fill}", fontcolor="#202124"];'
        )

    if cluster_by_component:
        for index, (component, members) in enumerate(sorted(grouped.items())):
            lines.append(f"  subgraph cluster_{index} {{")
            lines.append(f'    label="{html.escape(component)}";')
            lines.append('    style="rounded,dashed";')
            lines.append('    color="#c6cbd1";')
            lines.append('    fontname="Helvetica";')
            lines.append("    fontsize=11;")
            lines.append('    fontcolor="#5f6368";')
            for node_id in sorted(members):
                lines.append(node_line(node_id))
            lines.append("  }")
    else:
        for node_id in sorted(graph.nodes):
            lines.append(node_line(node_id))

    seen: set[tuple[str, str, str]] = set()
    for edge in graph.edges:
        key = (edge.source, edge.target, str(edge.kind))
        if key in seen:
            continue
        seen.add(key)
        style, colour = EDGE_STYLE.get(edge.kind, ("solid", "#3c4043"))
        on_impact_path = (
            plan.scenario == "changed"
            and edge.source in plan.selected_ids
            and edge.target in plan.selected_ids
        )
        penwidth = 2.0 if on_impact_path else 1.0
        if roles[edge.source] == "skipped" or roles[edge.target] == "skipped":
            colour = "#c6cbd1"
        lines.append(
            f'  "{edge.source}" -> "{edge.target}" '
            f'[style={style}, color="{colour}", penwidth={penwidth}, '
            f'tooltip="{html.escape(edge.describe())}"];'
        )

    lines.append("}")
    return "\n".join(lines) + "\n"


def render_dot(dot_source: str, out_path: Path, output_format: str = "svg") -> Path | None:
    if not has_graphviz():
        return None
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["dot", f"-T{output_format}", "-o", str(out_path)],
        input=dot_source,
        capture_output=True,
        text=True,
        check=False,
    )
    return out_path if result.returncode == 0 and out_path.is_file() else None


# ---------------------------------------------------------------------------
# Mermaid
# ---------------------------------------------------------------------------


def to_mermaid(plan: BuildPlan, title: str) -> str:
    graph = plan.graph
    roles = node_roles(plan)
    safe = {node_id: node_id.replace(".", "_").replace("-", "_") for node_id in graph.nodes}

    lines = [f"%% {title}", "graph BT"]
    grouped: dict[str, list[str]] = {}
    for node_id, project in graph.nodes.items():
        grouped.setdefault(project.component, []).append(node_id)

    for component, members in sorted(grouped.items()):
        lines.append(f'  subgraph {component.replace("<", "").replace(">", "")}["{component}"]')
        for node_id in sorted(members):
            project = graph.nodes[node_id]
            marker = "([" if project.kind is Kind.FEATURE else "["
            closer = "])" if project.kind is Kind.FEATURE else "]"
            entry = plan.entry(node_id)
            suffix = f"<br/>wave {entry.wave}" if entry else "<br/>not rebuilt"
            lines.append(f'    {safe[node_id]}{marker}"{project.short_id}{suffix}"{closer}')
        lines.append("  end")

    seen: set[tuple[str, str]] = set()
    for edge in graph.edges:
        key = (edge.source, edge.target)
        if key in seen:
            continue
        seen.add(key)
        arrow = "-.->" if edge.kind is EdgeKind.FEATURE_PLUGIN else "-->"
        lines.append(f"  {safe[edge.source]} {arrow} {safe[edge.target]}")

    for role, (outline, fill) in COLOURS.items():
        lines.append(f"  classDef {role} fill:{fill},stroke:{outline},stroke-width:2px;")
    for role in COLOURS:
        members = sorted(node for node_id, node in roles.items() if node == role for node in [node_id])
        if members:
            lines.append(f'  class {",".join(safe[m] for m in members)} {role};')

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------------


def ascii_tree(graph: DependencyGraph, roots: list[str], max_depth: int = 12) -> list[str]:
    """Render forward-dependency trees, marking already-expanded subtrees."""
    lines: list[str] = []

    def walk(node_id: str, prefix: str, is_last: bool, depth: int, seen: tuple[str, ...]) -> None:
        connector = "" if depth == 0 else ("`-- " if is_last else "|-- ")
        project = graph.nodes.get(node_id)
        label = project.short_id if project else node_id
        suffix = ""
        if node_id in seen:
            suffix = "  (already shown)"
        elif depth >= max_depth:
            suffix = "  (depth limit)"
        lines.append(f"{prefix}{connector}{label}{suffix}")
        if node_id in seen or depth >= max_depth:
            return
        children = graph.direct_dependencies(node_id)
        child_prefix = prefix if depth == 0 else prefix + ("    " if is_last else "|   ")
        for index, child in enumerate(children):
            walk(child, child_prefix, index == len(children) - 1, depth + 1, seen + (node_id,))

    for root in roots:
        walk(root, "", True, 0, ())
        lines.append("")
    return lines


def impact_tree(plan: BuildPlan, max_depth: int = 12) -> list[str]:
    """Render reverse-dependency (impact) trees rooted at each changed module."""
    lines: list[str] = []
    graph = plan.graph

    def walk(node_id: str, prefix: str, is_last: bool, depth: int, seen: tuple[str, ...]) -> None:
        connector = "" if depth == 0 else ("`-- " if is_last else "|-- ")
        project = graph.nodes.get(node_id)
        label = project.short_id if project else node_id
        suffix = "  <- CHANGED" if depth == 0 else ""
        if node_id in seen:
            suffix = "  (already shown)"
        lines.append(f"{prefix}{connector}{label}{suffix}")
        if node_id in seen or depth >= max_depth:
            return
        children = [
            child
            for child in graph.direct_dependents(node_id)
            if child in plan.selected_ids
        ]
        child_prefix = prefix if depth == 0 else prefix + ("    " if is_last else "|   ")
        for index, child in enumerate(children):
            walk(child, child_prefix, index == len(children) - 1, depth + 1, seen + (node_id,))

    for changed in sorted(plan.changed_ids):
        walk(changed, "", True, 0, ())
        lines.append("")
    return lines


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def to_json(plan: BuildPlan) -> str:
    graph = plan.graph
    payload = {
        "scenario": plan.scenario,
        "reactorRoot": str(plan.reactor.root),
        "fullRebuildReason": plan.full_rebuild_reason,
        "changeDetection": (
            {
                "strategy": plan.change_set.strategy,
                "baseline": plan.change_set.baseline,
                "globalChange": plan.change_set.global_change,
                "files": [asdict(change) for change in plan.change_set.changes],
            }
            if plan.change_set
            else None
        ),
        "modules": [
            {
                "id": node_id,
                "kind": str(project.kind),
                "component": project.component,
                "path": project.rel_path,
                "version": project.version,
                "dependsOn": graph.direct_dependencies(node_id),
                "dependedOnBy": graph.direct_dependents(node_id),
            }
            for node_id, project in sorted(graph.nodes.items())
        ],
        "edges": [
            {
                "from": edge.source,
                "to": edge.target,
                "kind": str(edge.kind),
                "detail": edge.detail,
                "optional": edge.optional,
            }
            for edge in graph.edges
        ],
        "componentGraph": {
            component: sorted(dependencies)
            for component, dependencies in sorted(plan.component_graph.deps.items())
        },
        "changedModules": sorted(plan.changed_ids),
        "selectedModules": [entry.project_id for entry in plan.entries],
        "skippedModules": sorted(plan.skipped_ids),
        "buildWaves": plan.waves,
        "componentWaves": plan.component_waves,
        "impactPaths": plan.impact_paths,
        "cycles": plan.cycles,
        "validations": [
            {"severity": v.severity, "project": v.project_id, "message": v.message}
            for v in plan.reactor.validations
        ],
        "mavenModuleList": plan.module_paths,
    }
    return json.dumps(payload, indent=2) + "\n"


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

_CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin:0; font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
       color:#202124; background:#f6f7f9; }
header { background:#0f2540; color:#fff; padding:28px 32px; }
header h1 { margin:0 0 6px; font-size:22px; font-weight:650; letter-spacing:-.01em; }
header p { margin:0; opacity:.75; font-size:13px; }
main { max-width:1180px; margin:0 auto; padding:24px 32px 64px; }
section { background:#fff; border:1px solid #e3e6ea; border-radius:10px; padding:22px 24px; margin:18px 0; }
h2 { font-size:16px; margin:0 0 14px; font-weight:650; }
h3 { font-size:13px; margin:22px 0 8px; font-weight:650; color:#5f6368;
     text-transform:uppercase; letter-spacing:.05em; }
.stats { display:flex; flex-wrap:wrap; gap:12px; margin:0; padding:0; list-style:none; }
.stats li { flex:1 1 140px; background:#fff; border:1px solid #e3e6ea; border-radius:10px; padding:14px 16px; }
.stats .n { display:block; font-size:26px; font-weight:680; letter-spacing:-.02em; }
.stats .k { font-size:11px; color:#5f6368; text-transform:uppercase; letter-spacing:.06em; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th,td { text-align:left; padding:8px 10px; border-bottom:1px solid #eceff2; vertical-align:top; }
th { font-size:11px; text-transform:uppercase; letter-spacing:.05em; color:#5f6368; font-weight:650; }
tbody tr:hover { background:#fafbfc; }
code,.mono { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:12px; }
.tag { display:inline-block; padding:1px 8px; border-radius:999px; font-size:11px; font-weight:600;
       border:1px solid; white-space:nowrap; }
.tag.changed { color:#b3261e; background:#fde7e5; border-color:#f3bdb8; }
.tag.dependent { color:#8a5200; background:#fff3e0; border-color:#f2d5a8; }
.tag.global { color:#6a1b9a; background:#f3e5f5; border-color:#dfc0e4; }
.tag.skipped { color:#5f6368; background:#f1f3f4; border-color:#dadce0; }
.tag.bundle { color:#1a56b0; background:#e8f0fe; border-color:#c3d6f7; }
.tag.feature { color:#0b6b3a; background:#e6f4ea; border-color:#b7dfc6; }
.tag.warning { color:#8a5200; background:#fff8e1; border-color:#f2dfa8; }
.tag.error { color:#b3261e; background:#fde7e5; border-color:#f3bdb8; }
.graph { overflow:auto; border:1px solid #e3e6ea; border-radius:8px; background:#fff; padding:8px; }
.graph svg { max-width:100%; height:auto; display:block; margin:0 auto; }
pre { background:#0f2540; color:#e8eaed; padding:14px 16px; border-radius:8px; overflow:auto;
      font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:12px; line-height:1.5; }
.path { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:12px; }
.path b { color:#b3261e; }
.legend { display:flex; flex-wrap:wrap; gap:14px; font-size:12px; color:#5f6368; margin-top:12px; }
.legend span { display:flex; align-items:center; gap:6px; }
.swatch { width:12px; height:12px; border-radius:3px; border:1.5px solid; display:inline-block; }
.note { background:#f8f9fa; border-left:3px solid #1a56b0; padding:10px 14px; border-radius:0 6px 6px 0;
        font-size:13px; margin:12px 0; }
.note.warn { border-left-color:#f9ab00; background:#fffbf0; }
.wave { font-weight:650; color:#1a56b0; }
details summary { cursor:pointer; font-size:13px; color:#1a56b0; font-weight:600; }
details[open] summary { margin-bottom:10px; }
"""


def _tag(text: str, kind: str) -> str:
    return f'<span class="tag {kind}">{html.escape(text)}</span>'


def _stat(number: object, label: str) -> str:
    return f'<li><span class="n">{html.escape(str(number))}</span><span class="k">{html.escape(label)}</span></li>'


def _svg_or_mermaid(plan: BuildPlan, title: str, out_dir: Path, stem: str) -> str:
    dot_source = to_dot(plan, title)
    (out_dir / f"{stem}.dot").write_text(dot_source, encoding="utf-8")
    (out_dir / f"{stem}.mmd").write_text(to_mermaid(plan, title), encoding="utf-8")

    svg_path = out_dir / f"{stem}.svg"
    if render_dot(dot_source, svg_path) is not None:
        render_dot(dot_source, out_dir / f"{stem}.png", "png")
        svg_markup = svg_path.read_text(encoding="utf-8")
        svg_markup = svg_markup[svg_markup.find("<svg") :]
        return f'<div class="graph">{svg_markup}</div>'

    mermaid = to_mermaid(plan, title)
    return (
        '<div class="note warn">Graphviz (<code>dot</code>) is not installed, so the diagram '
        "below is rendered from Mermaid in your browser. Install Graphviz to get embedded SVG/PNG.</div>"
        f'<div class="mermaid">{html.escape(mermaid)}</div>'
    )


def _legend() -> str:
    items = [
        ("changed", "directly changed"),
        ("dependent", "rebuilt (depends on a change)"),
        ("global", "rebuilt (shared file changed)"),
        ("skipped", "not rebuilt"),
        ("normal", "module (full build)"),
    ]
    parts = []
    for role, label in items:
        outline, fill = COLOURS[role]
        parts.append(
            f'<span><i class="swatch" style="background:{fill};border-color:{outline}"></i>{html.escape(label)}</span>'
        )
    parts.append("<span>rectangle = bundle &middot; 3-D box = feature</span>")
    parts.append("<span>arrow points at the dependency; lower rows build first</span>")
    return f'<div class="legend">{"".join(parts)}</div>'


def _order_table(plan: BuildPlan) -> str:
    rows = []
    for index, entry in enumerate(plan.entries, start=1):
        triggers = (
            ", ".join(plan.graph.nodes[t].short_id for t in entry.triggers if t in plan.graph.nodes)
            or "&mdash;"
        )
        files = (
            "<br>".join(f"<code>{html.escape(f)}</code>" for f in entry.changed_files) or "&mdash;"
        )
        rows.append(
            "<tr>"
            f'<td class="mono">{index}</td>'
            f'<td class="wave">{entry.wave}</td>'
            f'<td><code>{html.escape(entry.project_id)}</code></td>'
            f"<td>{_tag(str(entry.kind), str(entry.kind))}</td>"
            f"<td>{html.escape(entry.component)}</td>"
            f"<td>{_tag(str(entry.reason).replace('-', ' '), _REASON_TAG[entry.reason])}</td>"
            f"<td>{triggers}</td>"
            f"<td>{files}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>#</th><th>Wave</th><th>Module</th><th>Kind</th>"
        "<th>Component</th><th>Why</th><th>Triggered by</th><th>Changed files</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _module_table(plan: BuildPlan) -> str:
    rows = []
    for node_id, project in sorted(plan.graph.nodes.items()):
        dependencies = plan.graph.direct_dependencies(node_id)
        dependents = plan.graph.direct_dependents(node_id)
        rows.append(
            "<tr>"
            f'<td><code>{html.escape(node_id)}</code></td>'
            f"<td>{_tag(str(project.kind), str(project.kind))}</td>"
            f"<td>{html.escape(project.component)}</td>"
            f'<td class="mono">{html.escape(project.rel_path)}</td>'
            f'<td class="mono">{html.escape(", ".join(plan.graph.nodes[d].short_id for d in dependencies)) or "&mdash;"}</td>'
            f'<td class="mono">{html.escape(", ".join(plan.graph.nodes[d].short_id for d in dependents)) or "&mdash;"}</td>'
            "</tr>"
        )
    return (
        "<table><thead><tr><th>Module</th><th>Kind</th><th>Component</th><th>Path</th>"
        f"<th>Depends on</th><th>Depended on by</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _paths_section(plan: BuildPlan) -> str:
    if not plan.impact_paths:
        return ""
    blocks = []
    for seed, paths in sorted(plan.impact_paths.items()):
        seed_short = plan.graph.nodes[seed].short_id
        rows = []
        for target, chain in sorted(paths.items()):
            pretty = " &rarr; ".join(
                f"<b>{html.escape(plan.graph.nodes[step].short_id)}</b>"
                if step == seed
                else html.escape(plan.graph.nodes[step].short_id)
                for step in chain
            )
            edge_kinds = []
            for left, right in zip(chain, chain[1:]):
                found = plan.graph.edges_between(right, left)
                edge_kinds.append(str(found[0].kind) if found else "?")
            rows.append(
                "<tr>"
                f'<td><code>{html.escape(target)}</code></td>'
                f'<td class="path">{pretty}</td>'
                f'<td class="mono">{html.escape(" / ".join(edge_kinds))}</td>'
                f'<td class="mono">{len(chain) - 1}</td>'
                "</tr>"
            )
        if not rows:
            rows.append('<tr><td colspan="4">nothing else depends on this module</td></tr>')
        blocks.append(
            f"<h3>changed: {html.escape(seed_short)} <code>({html.escape(seed)})</code></h3>"
            "<table><thead><tr><th>Impacted module</th><th>Dependency path</th>"
            f"<th>Edge kinds</th><th>Hops</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"
        )
    return "<section><h2>Dependency path for every changed module</h2>" + "".join(blocks) + "</section>"


def _changes_section(plan: BuildPlan) -> str:
    change_set = plan.change_set
    if change_set is None:
        return ""
    rows = []
    for change in change_set.changes:
        owner = plan.graph.nodes.get(change.owner_id or "")
        rows.append(
            "<tr>"
            f'<td class="mono">{html.escape(change.status)}</td>'
            f'<td class="mono">{html.escape(change.rel_path)}</td>'
            f"<td>{html.escape(change.category)}</td>"
            f'<td><code>{html.escape(owner.id if owner else (change.owner_id or "unowned"))}</code></td>'
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="4">no changes detected</td></tr>')
    banner = ""
    if change_set.global_change:
        banner = (
            f'<div class="note warn"><b>Reactor-wide change.</b> '
            f"{html.escape(change_set.global_change)}</div>"
        )
    return (
        "<section><h2>Detected changes</h2>"
        f'<p style="margin:0 0 12px;font-size:13px;color:#5f6368">strategy: '
        f"<code>{html.escape(change_set.strategy)}</code> &middot; baseline: "
        f"<code>{html.escape(change_set.baseline)}</code></p>"
        f"{banner}"
        "<table><thead><tr><th>Status</th><th>File</th><th>Category</th><th>Owning module</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></section>"
    )


def _component_section(plan: BuildPlan) -> str:
    rows = []
    for component in sorted(plan.component_graph.members):
        dependencies = sorted(plan.component_graph.deps.get(component, set()))
        reasons = []
        for dependency in dependencies:
            why = plan.component_graph.reasons.get((component, dependency), [])
            reasons.append(f"{dependency} ({len(why)} edge{'s' if len(why) != 1 else ''})")
        rows.append(
            "<tr>"
            f"<td><b>{html.escape(component)}</b></td>"
            f'<td class="mono">{html.escape(", ".join(plan.component_graph.members[component]))}</td>'
            f'<td class="mono">{html.escape(", ".join(reasons)) or "&mdash;"}</td>'
            "</tr>"
        )
    waves = " &rarr; ".join(
        "[" + ", ".join(html.escape(component) for component in wave) + "]"
        for wave in plan.component_waves
    )
    return (
        "<section><h2>Component-level view</h2>"
        f'<p style="font-size:13px;color:#5f6368;margin:0 0 12px">Component build order: '
        f'<span class="path">{waves or "&mdash;"}</span></p>'
        "<table><thead><tr><th>Component</th><th>Members</th><th>Depends on components</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></section>"
    )


def _validation_section(plan: BuildPlan) -> str:
    validations = plan.reactor.validations
    if not validations:
        return (
            '<section><h2>Product consistency checks</h2>'
            '<div class="note">No metadata inconsistencies found.</div></section>'
        )
    rows = []
    for validation in validations:
        rows.append(
            "<tr>"
            f"<td>{_tag(validation.severity, validation.severity)}</td>"
            f'<td><code>{html.escape(validation.project_id)}</code></td>'
            f"<td>{html.escape(validation.message)}</td>"
            "</tr>"
        )
    return (
        "<section><h2>Product consistency checks</h2>"
        '<div class="note warn">These are real metadata inconsistencies found while parsing the '
        "OSGi manifests and feature definitions. They do not stop the build, but they mean a "
        "feature can be installed without a bundle it actually needs.</div>"
        "<table><thead><tr><th>Severity</th><th>Where</th><th>Finding</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></section>"
    )


def write_report(
    out_dir: Path,
    plans: list[tuple[str, BuildPlan]],
    maven_commands: dict[str, list[str]] | None = None,
    generated_at: str = "",
) -> Path:
    """Write a single self-contained HTML report covering the supplied plans."""
    out_dir.mkdir(parents=True, exist_ok=True)
    maven_commands = maven_commands or {}
    body: list[str] = []
    needs_mermaid = False

    for stem, plan in plans:
        title = (
            "Full product build - dependency graph"
            if plan.scenario == "full"
            else "Changed-modules build - dependency graph and impact"
        )
        graph_markup = _svg_or_mermaid(plan, title, out_dir, stem)
        needs_mermaid = needs_mermaid or 'class="mermaid"' in graph_markup

        total = len(plan.graph.nodes)
        selected = len(plan.entries)
        stats = [
            _stat(total, "modules in product"),
            _stat(selected, "modules built"),
            _stat(len(plan.waves), "build waves"),
        ]
        if plan.scenario == "changed":
            saved = total - selected
            stats.insert(1, _stat(len(plan.changed_ids), "directly changed"))
            stats.append(_stat(saved, "modules skipped"))
            stats.append(
                _stat(f"{(saved / total * 100 if total else 0):.0f}%", "of reactor avoided")
            )

        heading = "Scenario 1 &mdash; build all products" if plan.scenario == "full" else (
            "Scenario 2 &mdash; build only changed products"
        )

        command = maven_commands.get(stem)
        command_block = (
            f"<h3>Maven invocation</h3><pre>{html.escape(' '.join(command))}</pre>"
            if command
            else ""
        )

        cycles_block = ""
        if plan.cycles:
            cycles_block = (
                '<div class="note warn"><b>Dependency cycles detected</b> &mdash; Tycho cannot '
                "order these: "
                + "; ".join(" &harr; ".join(html.escape(m) for m in cycle) for cycle in plan.cycles)
                + "</div>"
            )

        full_reason = ""
        if plan.full_rebuild_reason:
            full_reason = (
                f'<div class="note warn"><b>Escalated to a full rebuild.</b> '
                f"{html.escape(plan.full_rebuild_reason)}</div>"
            )

        body.append(
            f"<section><h2>{heading}</h2>"
            f'<ul class="stats">{"".join(stats)}</ul>'
            f"{cycles_block}{full_reason}"
            f"<h3>Dependency graph</h3>{graph_markup}{_legend()}"
            f"<h3>Build order</h3>{_order_table(plan)}"
            f"{command_block}"
            "</section>"
        )

        if plan.scenario == "changed":
            body.append(_changes_section(plan))
            body.append(_paths_section(plan))
            if plan.skipped_ids:
                skipped = ", ".join(
                    f"<code>{html.escape(plan.graph.nodes[node].short_id)}</code>"
                    for node in sorted(plan.skipped_ids)
                )
                body.append(
                    "<section><h2>Modules deliberately not rebuilt</h2>"
                    f'<p style="font-size:13px">{skipped}</p>'
                    '<p style="font-size:13px;color:#5f6368;margin:8px 0 0">Nothing in the '
                    "change set reaches these modules along a dependency edge, so their existing "
                    "artifacts remain valid.</p></section>"
                )

    reference_plan = plans[-1][1]
    body.append(_component_section(reference_plan))
    body.append(
        "<section><h2>All modules and their dependencies</h2>"
        f"{_module_table(reference_plan)}</section>"
    )
    body.append(_validation_section(reference_plan))

    mermaid_script = (
        '<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>'
        "<script>mermaid.initialize({startOnLoad:true});</script>"
        if needs_mermaid
        else ""
    )

    document = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Northwind OMS build pipeline report</title>
<style>{_CSS}</style></head>
<body>
<header>
  <h1>Northwind OMS &mdash; smart build pipeline report</h1>
  <p>Dependency graph derived from OSGi metadata (MANIFEST.MF and feature.xml), not from POM
     declarations{f" &middot; generated {html.escape(generated_at)}" if generated_at else ""}</p>
</header>
<main>{''.join(body)}</main>
{mermaid_script}
</body></html>
"""
    report_path = out_dir / "index.html"
    report_path.write_text(document, encoding="utf-8")
    return report_path


def write_markdown(out_dir: Path, plans: list[tuple[str, BuildPlan]]) -> Path:
    """A Mermaid-based markdown report for GitHub/GitLab rendering."""
    out_dir.mkdir(parents=True, exist_ok=True)
    lines: list[str] = ["# Northwind OMS build pipeline report", ""]

    for stem, plan in plans:
        heading = (
            "Scenario 1 - build all products"
            if plan.scenario == "full"
            else "Scenario 2 - build only changed products"
        )
        lines += [f"## {heading}", ""]
        lines += [
            f"- Modules in product: **{len(plan.graph.nodes)}**",
            f"- Modules built: **{len(plan.entries)}**",
            f"- Build waves: **{len(plan.waves)}**",
        ]
        if plan.scenario == "changed":
            lines.append(f"- Directly changed: **{len(plan.changed_ids)}**")
            lines.append(f"- Skipped: **{len(plan.skipped_ids)}**")
        lines += ["", "### Dependency graph", "", "```mermaid"]
        lines.append(to_mermaid(plan, heading).rstrip())
        lines += ["```", "", "### Build order", "", "| # | Wave | Module | Kind | Why |", "|---|---|---|---|---|"]
        for index, entry in enumerate(plan.entries, start=1):
            lines.append(
                f"| {index} | {entry.wave} | `{entry.project_id}` | {entry.kind} | {entry.reason} |"
            )
        lines.append("")

        if plan.impact_paths:
            lines += ["### Dependency paths from changed modules", ""]
            for seed, paths in sorted(plan.impact_paths.items()):
                lines.append(f"**{seed}**")
                lines.append("")
                for target, chain in sorted(paths.items()):
                    lines.append(f"- `{' -> '.join(chain)}`")
                lines.append("")

    markdown_path = out_dir / "report.md"
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return markdown_path
