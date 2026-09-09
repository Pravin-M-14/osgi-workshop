"""Turn a graph plus a change set into an ordered, explainable build plan."""

from __future__ import annotations

from dataclasses import dataclass, field

from .changes import ChangeSet
from .graph import ComponentGraph, DependencyGraph, project_to_components
from .model import ChangeReason, Kind
from .scan import Reactor


@dataclass
class PlanEntry:
    """One project in the build plan, with the reason it is there."""

    project_id: str
    rel_path: str
    kind: Kind
    component: str
    wave: int
    reason: ChangeReason
    triggers: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)


@dataclass
class BuildPlan:
    scenario: str  # full | changed
    reactor: Reactor
    graph: DependencyGraph
    component_graph: ComponentGraph
    entries: list[PlanEntry] = field(default_factory=list)
    waves: list[list[str]] = field(default_factory=list)
    component_waves: list[list[str]] = field(default_factory=list)
    changed_ids: set[str] = field(default_factory=set)
    selected_ids: set[str] = field(default_factory=set)
    skipped_ids: set[str] = field(default_factory=set)
    impact_paths: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    cycles: list[list[str]] = field(default_factory=list)
    change_set: ChangeSet | None = None
    full_rebuild_reason: str | None = None

    @property
    def ordered_ids(self) -> list[str]:
        return [entry.project_id for entry in self.entries]

    @property
    def module_paths(self) -> list[str]:
        """Reactor-relative paths, in build order, for ``mvn -pl``."""
        return [entry.rel_path for entry in self.entries]

    @property
    def selected_components(self) -> list[str]:
        seen: list[str] = []
        for entry in self.entries:
            if entry.component not in seen:
                seen.append(entry.component)
        return seen

    def entry(self, project_id: str) -> PlanEntry | None:
        for candidate in self.entries:
            if candidate.project_id == project_id:
                return candidate
        return None


def plan_full(reactor: Reactor, graph: DependencyGraph) -> BuildPlan:
    """Scenario 1: build every module, in dependency order."""
    component_graph = project_to_components(graph)
    plan = BuildPlan(
        scenario="full",
        reactor=reactor,
        graph=graph,
        component_graph=component_graph,
        selected_ids=set(graph.nodes),
    )
    plan.cycles = graph.find_cycles()
    plan.waves = graph.topological_waves()
    plan.component_waves = component_graph.topological_waves()
    plan.entries = _entries_from_waves(graph, plan.waves, {}, {})
    for entry in plan.entries:
        entry.reason = ChangeReason.FULL_BUILD
    return plan


def plan_changed(
    reactor: Reactor,
    graph: DependencyGraph,
    change_set: ChangeSet,
    force_full_on_global: bool = True,
) -> BuildPlan:
    """Scenario 2: build only what changed, plus everything that depends on it."""
    component_graph = project_to_components(graph)
    plan = BuildPlan(
        scenario="changed",
        reactor=reactor,
        graph=graph,
        component_graph=component_graph,
        change_set=change_set,
    )
    plan.cycles = graph.find_cycles()

    changed_ids = {
        project_id
        for project_id in change_set.changed_project_ids
        if project_id in graph.nodes
    }
    plan.changed_ids = changed_ids

    if change_set.global_change and force_full_on_global:
        plan.full_rebuild_reason = change_set.global_change
        plan.selected_ids = set(graph.nodes)
    else:
        plan.selected_ids = graph.transitive_dependents(changed_ids)

    plan.skipped_ids = set(graph.nodes) - plan.selected_ids
    plan.waves = graph.topological_waves(plan.selected_ids)
    plan.component_waves = component_graph.topological_waves(
        {graph.nodes[node].component for node in plan.selected_ids}
    )

    # Why is each selected project in the plan, and by which path?
    triggers: dict[str, list[str]] = {}
    for seed in sorted(changed_ids):
        paths = graph.shortest_paths_from(seed, plan.selected_ids - {seed})
        plan.impact_paths[seed] = paths
        for target in paths:
            triggers.setdefault(target, []).append(seed)

    changed_files = {
        project_id: [change.rel_path for change in change_set.files_for(project_id)]
        for project_id in changed_ids
    }

    plan.entries = _entries_from_waves(graph, plan.waves, triggers, changed_files)
    for entry in plan.entries:
        if entry.project_id in changed_ids:
            entry.reason = ChangeReason.DIRECTLY_CHANGED
        elif plan.full_rebuild_reason and not entry.triggers:
            entry.reason = ChangeReason.GLOBAL
        else:
            entry.reason = ChangeReason.DEPENDENT

    return plan


def _entries_from_waves(
    graph: DependencyGraph,
    waves: list[list[str]],
    triggers: dict[str, list[str]],
    changed_files: dict[str, list[str]],
) -> list[PlanEntry]:
    entries: list[PlanEntry] = []
    for wave_index, wave in enumerate(waves, start=1):
        for project_id in wave:
            project = graph.nodes[project_id]
            entries.append(
                PlanEntry(
                    project_id=project_id,
                    rel_path=project.rel_path,
                    kind=project.kind,
                    component=project.component,
                    wave=wave_index,
                    reason=ChangeReason.DIRECTLY_CHANGED
                    if project_id in changed_files
                    else ChangeReason.DEPENDENT,
                    triggers=sorted(triggers.get(project_id, [])),
                    changed_files=changed_files.get(project_id, []),
                )
            )
    return entries
