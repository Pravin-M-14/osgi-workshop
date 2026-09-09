"""Dependency graph, topological ordering, and impact analysis.

Edge direction convention throughout: an edge ``A -> B`` means "A depends on
B", therefore **B must be built before A**.  The reverse edges (``rdeps``) are
what drive impact analysis: if B changed, everything reachable from B by
reverse edges must be rebuilt.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .model import Edge, EdgeKind, Kind, Project
from .scan import Reactor


@dataclass
class DependencyGraph:
    """Directed graph over reactor project ids."""

    nodes: dict[str, Project] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    deps: dict[str, set[str]] = field(default_factory=dict)
    rdeps: dict[str, set[str]] = field(default_factory=dict)

    # -- construction -----------------------------------------------------

    def add_node(self, project: Project) -> None:
        self.nodes.setdefault(project.id, project)
        self.deps.setdefault(project.id, set())
        self.rdeps.setdefault(project.id, set())

    def add_edge(self, edge: Edge) -> None:
        if edge.source == edge.target:
            return
        if edge.source not in self.nodes or edge.target not in self.nodes:
            return
        self.edges.append(edge)
        self.deps[edge.source].add(edge.target)
        self.rdeps[edge.target].add(edge.source)

    # -- queries ----------------------------------------------------------

    def edges_between(self, source: str, target: str) -> list[Edge]:
        return [
            edge for edge in self.edges if edge.source == source and edge.target == target
        ]

    def direct_dependencies(self, node_id: str) -> list[str]:
        return sorted(self.deps.get(node_id, set()))

    def direct_dependents(self, node_id: str) -> list[str]:
        return sorted(self.rdeps.get(node_id, set()))

    def transitive_dependents(self, seeds: set[str]) -> set[str]:
        """Every node reachable from ``seeds`` along reverse edges (inclusive)."""
        seen: set[str] = set()
        queue: deque[str] = deque(seed for seed in seeds if seed in self.nodes)
        seen.update(queue)
        while queue:
            current = queue.popleft()
            for dependent in self.rdeps.get(current, set()):
                if dependent not in seen:
                    seen.add(dependent)
                    queue.append(dependent)
        return seen

    def transitive_dependencies(self, seeds: set[str]) -> set[str]:
        """Every node reachable from ``seeds`` along forward edges (inclusive)."""
        seen: set[str] = set()
        queue: deque[str] = deque(seed for seed in seeds if seed in self.nodes)
        seen.update(queue)
        while queue:
            current = queue.popleft()
            for dependency in self.deps.get(current, set()):
                if dependency not in seen:
                    seen.add(dependency)
                    queue.append(dependency)
        return seen

    def shortest_paths_from(self, seed: str, targets: set[str]) -> dict[str, list[str]]:
        """Shortest reverse-edge path from ``seed`` to each of ``targets``.

        This answers the README's "dependency path for every changed module":
        for a changed bundle, show *how* the impact propagates to each module
        that has to be rebuilt.
        """
        if seed not in self.nodes:
            return {}
        previous: dict[str, str | None] = {seed: None}
        queue: deque[str] = deque([seed])
        while queue:
            current = queue.popleft()
            for dependent in sorted(self.rdeps.get(current, set())):
                if dependent not in previous:
                    previous[dependent] = current
                    queue.append(dependent)

        paths: dict[str, list[str]] = {}
        for target in sorted(targets):
            if target not in previous:
                continue
            chain: list[str] = []
            cursor: str | None = target
            while cursor is not None:
                chain.append(cursor)
                cursor = previous[cursor]
            paths[target] = list(reversed(chain))
        return paths

    # -- ordering ---------------------------------------------------------

    def find_cycles(self, subset: set[str] | None = None) -> list[list[str]]:
        """Return simple cycles within the (optionally restricted) graph.

        Iterative Tarjan strongly-connected-components; any component of size
        greater than one is a dependency cycle, which Tycho cannot build.
        """
        scope = set(self.nodes) if subset is None else {n for n in subset if n in self.nodes}
        index_counter = 0
        indices: dict[str, int] = {}
        low: dict[str, int] = {}
        on_stack: dict[str, bool] = {}
        stack: list[str] = []
        components: list[list[str]] = []

        for root in sorted(scope):
            if root in indices:
                continue
            work: list[tuple[str, list[str]]] = [
                (root, sorted(child for child in self.deps[root] if child in scope))
            ]
            indices[root] = low[root] = index_counter
            index_counter += 1
            stack.append(root)
            on_stack[root] = True

            while work:
                node, children = work[-1]
                if children:
                    child = children.pop(0)
                    if child not in indices:
                        indices[child] = low[child] = index_counter
                        index_counter += 1
                        stack.append(child)
                        on_stack[child] = True
                        work.append(
                            (
                                child,
                                sorted(
                                    grandchild
                                    for grandchild in self.deps[child]
                                    if grandchild in scope
                                ),
                            )
                        )
                    elif on_stack.get(child):
                        low[node] = min(low[node], indices[child])
                else:
                    work.pop()
                    if work:
                        parent = work[-1][0]
                        low[parent] = min(low[parent], low[node])
                    if low[node] == indices[node]:
                        component: list[str] = []
                        while True:
                            member = stack.pop()
                            on_stack[member] = False
                            component.append(member)
                            if member == node:
                                break
                        if len(component) > 1:
                            components.append(sorted(component))

        return sorted(components, key=lambda component: component[0])

    def topological_waves(self, subset: set[str] | None = None) -> list[list[str]]:
        """Kahn's algorithm, grouped into waves of mutually independent nodes.

        Each wave can be built in parallel (``mvn -T``); wave *n* only depends
        on waves ``< n``.  Ties are broken alphabetically so the build order is
        reproducible run to run -- important when a human is diffing build logs.
        """
        scope = set(self.nodes) if subset is None else {n for n in subset if n in self.nodes}
        remaining_deps = {
            node: {dep for dep in self.deps[node] if dep in scope} for node in scope
        }
        waves: list[list[str]] = []
        settled: set[str] = set()

        while len(settled) < len(scope):
            wave = sorted(
                node
                for node in scope
                if node not in settled and not (remaining_deps[node] - settled)
            )
            if not wave:
                break  # a cycle blocks progress; reported separately
            waves.append(wave)
            settled.update(wave)

        return waves

    def topological_order(self, subset: set[str] | None = None) -> list[str]:
        return [node for wave in self.topological_waves(subset) for node in wave]


# ---------------------------------------------------------------------------
# Graph construction from a scanned reactor
# ---------------------------------------------------------------------------


def build_graph(reactor: Reactor, include_aggregators: bool = False) -> DependencyGraph:
    """Derive the product graph from OSGi metadata.

    Edges created:

    ``Import-Package``   bundle -> exporting workspace bundle (packages supplied
                         by the external target platform are skipped)
    ``Require-Bundle``   bundle -> required workspace bundle
    ``feature-plugin``   feature -> each bundle it packages
    ``feature-requires`` feature -> imported feature (or bundle)
    ``feature-includes`` feature -> nested feature
    """
    graph = DependencyGraph()
    for project in reactor.projects.values():
        if project.is_buildable or include_aggregators:
            graph.add_node(project)

    for project in reactor.projects.values():
        if project.kind is Kind.BUNDLE:
            _add_bundle_edges(graph, reactor, project)
        elif project.kind is Kind.FEATURE:
            _add_feature_edges(graph, reactor, project)

    if include_aggregators:
        for project in reactor.projects.values():
            if project.parent_rel_path is None:
                continue
            parent = reactor.by_rel_path.get(project.parent_rel_path)
            if parent is not None and parent.kind is Kind.AGGREGATOR:
                graph.add_edge(
                    Edge(project.id, parent.id, EdgeKind.PARENT_POM, "parent pom")
                )

    return graph


def _add_bundle_edges(graph: DependencyGraph, reactor: Reactor, project: Project) -> None:
    for requirement in project.imports:
        owner = reactor.package_owner.get(requirement.name)
        if owner is None or owner == project.id:
            continue  # external / target-platform package, or self-import
        graph.add_edge(
            Edge(
                project.id,
                owner,
                EdgeKind.IMPORT_PACKAGE,
                requirement.describe(),
                requirement.optional,
            )
        )

    for requirement in project.dynamic_imports:
        owner = reactor.package_owner.get(requirement.name)
        if owner is None or owner == project.id:
            continue
        graph.add_edge(
            Edge(
                project.id,
                owner,
                EdgeKind.DYNAMIC_IMPORT,
                requirement.describe(),
                optional=True,
            )
        )

    for requirement in project.require_bundles:
        if requirement.name not in graph.nodes:
            continue
        graph.add_edge(
            Edge(
                project.id,
                requirement.name,
                EdgeKind.REQUIRE_BUNDLE,
                requirement.describe(),
                requirement.optional,
            )
        )


def _add_feature_edges(graph: DependencyGraph, reactor: Reactor, project: Project) -> None:
    for plugin_id in project.feature_plugins:
        if plugin_id in graph.nodes:
            graph.add_edge(
                Edge(project.id, plugin_id, EdgeKind.FEATURE_PLUGIN, "packages plugin")
            )

    for requirement in project.feature_requires:
        if requirement.name in graph.nodes:
            graph.add_edge(
                Edge(
                    project.id,
                    requirement.name,
                    EdgeKind.FEATURE_REQUIRES,
                    requirement.describe(),
                )
            )

    for include_id in project.feature_includes:
        if include_id in graph.nodes:
            graph.add_edge(
                Edge(project.id, include_id, EdgeKind.FEATURE_INCLUDES, "includes feature")
            )


# ---------------------------------------------------------------------------
# Component-level projection
# ---------------------------------------------------------------------------


@dataclass
class ComponentGraph:
    """The bundle/feature graph lifted to top-level component directories."""

    members: dict[str, list[str]] = field(default_factory=dict)
    deps: dict[str, set[str]] = field(default_factory=dict)
    rdeps: dict[str, set[str]] = field(default_factory=dict)
    reasons: dict[tuple[str, str], list[str]] = field(default_factory=dict)

    def topological_waves(self, subset: set[str] | None = None) -> list[list[str]]:
        scope = set(self.members) if subset is None else set(subset)
        remaining = {
            component: {dep for dep in self.deps.get(component, set()) if dep in scope}
            for component in scope
        }
        waves: list[list[str]] = []
        settled: set[str] = set()
        while len(settled) < len(scope):
            wave = sorted(
                component
                for component in scope
                if component not in settled and not (remaining[component] - settled)
            )
            if not wave:
                break
            waves.append(wave)
            settled.update(wave)
        return waves


def project_to_components(graph: DependencyGraph) -> ComponentGraph:
    component_graph = ComponentGraph()
    for node_id, project in graph.nodes.items():
        component_graph.members.setdefault(project.component, []).append(node_id)
        component_graph.deps.setdefault(project.component, set())
        component_graph.rdeps.setdefault(project.component, set())
    for members in component_graph.members.values():
        members.sort()

    for edge in graph.edges:
        source = graph.nodes[edge.source].component
        target = graph.nodes[edge.target].component
        if source == target:
            continue
        component_graph.deps[source].add(target)
        component_graph.rdeps[target].add(source)
        component_graph.reasons.setdefault((source, target), []).append(
            f"{edge.source} -> {edge.target} ({edge.kind})"
        )

    return component_graph
