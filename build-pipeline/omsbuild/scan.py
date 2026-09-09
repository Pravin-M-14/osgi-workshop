"""Scan the reactor and parse OSGi metadata into :mod:`omsbuild.model` objects.

Discovery is driven by the POM ``<modules>`` tree rather than by globbing, so
the pipeline sees exactly the same set of projects Maven would.  Once a project
is located, its real dependencies are read from its manifest or feature file.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from .model import Kind, Project, Requirement, Validation

POM_NS = "{http://maven.apache.org/POM/4.0.0}"


# ---------------------------------------------------------------------------
# OSGi manifest parsing
# ---------------------------------------------------------------------------


def unfold_manifest(text: str) -> list[str]:
    """Undo the 72-byte line wrapping mandated by the JAR manifest spec.

    A continuation line starts with exactly one space, which is *not* part of
    the value.  Getting this wrong silently truncates ``Import-Package`` lists,
    which is precisely the data the whole pipeline depends on.
    """
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for raw in normalised.split("\n"):
        if raw.startswith(" ") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return [line for line in lines if line.strip()]


def split_top_level(value: str, separator: str) -> list[str]:
    """Split on ``separator`` while respecting double-quoted sections.

    Necessary because version ranges contain commas: the clause
    ``com.northwind.oms.core.model;version="[1.0.0,2.0.0)"`` is *one* clause,
    not two.
    """
    parts: list[str] = []
    buffer: list[str] = []
    in_quotes = False
    for char in value:
        if char == '"':
            in_quotes = not in_quotes
            buffer.append(char)
        elif char == separator and not in_quotes:
            parts.append("".join(buffer).strip())
            buffer = []
        else:
            buffer.append(char)
    tail = "".join(buffer).strip()
    if tail:
        parts.append(tail)
    return [part for part in parts if part]


def parse_headers(manifest_text: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in unfold_manifest(manifest_text):
        if ":" not in line:
            continue
        key, _, raw_value = line.partition(":")
        headers[key.strip()] = raw_value.strip()
    return headers


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def parse_requirement_header(value: str) -> list[Requirement]:
    """Parse an ``Import-Package`` / ``Require-Bundle`` style header."""
    requirements: list[Requirement] = []
    for clause in split_top_level(value, ","):
        pieces = split_top_level(clause, ";")
        if not pieces:
            continue
        # A clause may name several packages sharing one attribute set:
        #   a.b.c;x.y.z;version="[1,2)"
        names: list[str] = []
        attributes: dict[str, str] = {}
        for piece in pieces:
            if ":=" in piece:
                key, _, raw = piece.partition(":=")
                attributes[key.strip() + ":"] = _unquote(raw)
            elif "=" in piece:
                key, _, raw = piece.partition("=")
                attributes[key.strip()] = _unquote(raw)
            else:
                names.append(piece.strip())
        version_range = attributes.get("version") or attributes.get("bundle-version") or ""
        optional = attributes.get("resolution:", "").lower() == "optional"
        for name in names:
            requirements.append(
                Requirement(
                    name=name,
                    version_range=version_range,
                    optional=optional,
                    directives=attributes,
                )
            )
    return requirements


def parse_export_header(value: str) -> dict[str, str]:
    exports: dict[str, str] = {}
    for requirement in parse_requirement_header(value):
        exports[requirement.name] = requirement.version_range
    return exports


def parse_symbolic_name(value: str) -> str:
    """Strip directives such as ``;singleton:=true`` from a bundle name."""
    return split_top_level(value, ";")[0].strip() if value else ""


def parse_build_properties(path: Path) -> list[str]:
    """Extract ``source..`` entries so we know which folders hold Java code."""
    if not path.is_file():
        return ["src"]
    joined: list[str] = []
    for raw in unfold_manifest(path.read_text(encoding="utf-8", errors="replace")):
        joined.append(raw)
    text = "\n".join(joined)
    # Handle backslash continuations used by build.properties.
    text = text.replace("\\\n", "")
    sources: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("source."):
            continue
        _, _, value = line.partition("=")
        for entry in value.split(","):
            entry = entry.strip().rstrip("/")
            if entry:
                sources.append(entry)
    return sources or ["src"]


# ---------------------------------------------------------------------------
# POM + feature parsing
# ---------------------------------------------------------------------------


def _pom_text(element: ET.Element | None, tag: str) -> str:
    if element is None:
        return ""
    child = element.find(POM_NS + tag)
    return (child.text or "").strip() if child is not None and child.text else ""


def parse_pom(path: Path) -> dict[str, object]:
    root = ET.parse(path).getroot()
    modules_element = root.find(POM_NS + "modules")
    modules = (
        [(module.text or "").strip() for module in modules_element.findall(POM_NS + "module")]
        if modules_element is not None
        else []
    )
    return {
        "artifact_id": _pom_text(root, "artifactId"),
        "packaging": _pom_text(root, "packaging") or "jar",
        "version": _pom_text(root, "version"),
        "name": _pom_text(root, "name"),
        "modules": [module for module in modules if module],
    }


def parse_feature(path: Path) -> dict[str, object]:
    root = ET.parse(path).getroot()
    requires: list[Requirement] = []
    requires_element = root.find("requires")
    if requires_element is not None:
        for imported in requires_element.findall("import"):
            target = imported.get("feature") or imported.get("plugin")
            if not target:
                continue
            requires.append(
                Requirement(
                    name=target,
                    version_range=imported.get("version", ""),
                    optional=False,
                    directives={"match": imported.get("match", "")},
                )
            )
    plugins = [
        plugin.get("id", "") for plugin in root.findall("plugin") if plugin.get("id")
    ]
    includes = [
        include.get("id", "") for include in root.findall("includes") if include.get("id")
    ]
    return {
        "id": root.get("id", ""),
        "version": root.get("version", ""),
        "label": root.get("label", ""),
        "requires": requires,
        "plugins": plugins,
        "includes": includes,
    }


# ---------------------------------------------------------------------------
# Reactor walk
# ---------------------------------------------------------------------------


class Reactor:
    """The scanned product: every project, indexed several useful ways."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.projects: dict[str, Project] = {}
        self.by_rel_path: dict[str, Project] = {}
        self.package_owner: dict[str, str] = {}
        self.validations: list[Validation] = []

    # -- lookup helpers ---------------------------------------------------

    @property
    def buildable(self) -> list[Project]:
        return [project for project in self.projects.values() if project.is_buildable]

    @property
    def components(self) -> list[str]:
        seen: list[str] = []
        for project in self.buildable:
            if project.component not in seen:
                seen.append(project.component)
        return sorted(seen)

    def get(self, project_id: str) -> Project | None:
        return self.projects.get(project_id)

    def owner_of_path(self, rel_path: str) -> Project | None:
        """Resolve a repo-relative file path to the project that contains it.

        Longest-prefix wins so that ``catalog/plugins/x/src/A.java`` maps to the
        plugin and not to the ``catalog`` aggregator.
        """
        best: Project | None = None
        for candidate in self.projects.values():
            prefix = candidate.rel_path
            if prefix in ("", "."):
                continue
            if rel_path == prefix or rel_path.startswith(prefix + "/"):
                if best is None or len(prefix) > len(best.rel_path):
                    best = candidate
        return best


def scan(root: Path) -> Reactor:
    """Walk the aggregator tree from the root POM and parse every project."""
    reactor = Reactor(root)
    root_pom = reactor.root / "pom.xml"
    if not root_pom.is_file():
        raise FileNotFoundError(f"no pom.xml at reactor root: {reactor.root}")
    _visit(reactor, reactor.root, parent_rel=None)
    _index_packages(reactor)
    _validate(reactor)
    return reactor


def _rel(reactor: Reactor, path: Path) -> str:
    relative = path.resolve().relative_to(reactor.root).as_posix()
    return "" if relative == "." else relative


def _visit(reactor: Reactor, directory: Path, parent_rel: str | None) -> None:
    pom_path = directory / "pom.xml"
    if not pom_path.is_file():
        reactor.validations.append(
            Validation("error", _rel(reactor, directory), "declared module has no pom.xml")
        )
        return

    pom = parse_pom(pom_path)
    rel_path = _rel(reactor, directory)
    packaging = str(pom["packaging"])
    manifest_path = directory / "META-INF" / "MANIFEST.MF"
    feature_path = directory / "feature.xml"

    if packaging == "eclipse-plugin" or manifest_path.is_file():
        project = _build_bundle(reactor, directory, pom, rel_path, manifest_path)
    elif packaging == "eclipse-feature" or feature_path.is_file():
        project = _build_feature(reactor, directory, pom, rel_path, feature_path)
    else:
        project = Project(
            id=str(pom["artifact_id"]) or rel_path or "<root>",
            kind=Kind.AGGREGATOR,
            path=directory,
            rel_path=rel_path,
            component=_component_of(rel_path),
            artifact_id=str(pom["artifact_id"]),
            version=str(pom["version"]),
            name=str(pom["name"]),
        )

    project.parent_rel_path = parent_rel
    project.module_rel_paths = [
        _rel(reactor, directory / str(module)) for module in pom["modules"]  # type: ignore[arg-type]
    ]

    if project.id in reactor.projects:
        reactor.validations.append(
            Validation("error", project.id, f"duplicate project id (also at {rel_path})")
        )
    reactor.projects[project.id] = project
    reactor.by_rel_path[rel_path] = project

    for module in pom["modules"]:  # type: ignore[union-attr]
        _visit(reactor, directory / str(module), parent_rel=rel_path)


def _component_of(rel_path: str) -> str:
    if not rel_path:
        return "<product>"
    return rel_path.split("/", 1)[0]


def _build_bundle(
    reactor: Reactor, directory: Path, pom: dict, rel_path: str, manifest_path: Path
) -> Project:
    if not manifest_path.is_file():
        reactor.validations.append(
            Validation("error", str(pom["artifact_id"]), "eclipse-plugin without META-INF/MANIFEST.MF")
        )
        headers: dict[str, str] = {}
    else:
        headers = parse_headers(manifest_path.read_text(encoding="utf-8", errors="replace"))

    symbolic_name = parse_symbolic_name(headers.get("Bundle-SymbolicName", "")) or str(
        pom["artifact_id"]
    )
    return Project(
        id=symbolic_name,
        kind=Kind.BUNDLE,
        path=directory,
        rel_path=rel_path,
        component=_component_of(rel_path),
        artifact_id=str(pom["artifact_id"]),
        version=headers.get("Bundle-Version", str(pom["version"])),
        name=headers.get("Bundle-Name", str(pom["name"])),
        exports=parse_export_header(headers.get("Export-Package", "")),
        imports=parse_requirement_header(headers.get("Import-Package", "")),
        require_bundles=parse_requirement_header(headers.get("Require-Bundle", "")),
        dynamic_imports=parse_requirement_header(headers.get("DynamicImport-Package", "")),
        source_dirs=parse_build_properties(directory / "build.properties"),
    )


def _build_feature(
    reactor: Reactor, directory: Path, pom: dict, rel_path: str, feature_path: Path
) -> Project:
    if not feature_path.is_file():
        reactor.validations.append(
            Validation("error", str(pom["artifact_id"]), "eclipse-feature without feature.xml")
        )
        parsed: dict[str, object] = {
            "id": str(pom["artifact_id"]),
            "version": "",
            "label": "",
            "requires": [],
            "plugins": [],
            "includes": [],
        }
    else:
        parsed = parse_feature(feature_path)

    return Project(
        id=str(parsed["id"]) or str(pom["artifact_id"]),
        kind=Kind.FEATURE,
        path=directory,
        rel_path=rel_path,
        component=_component_of(rel_path),
        artifact_id=str(pom["artifact_id"]),
        version=str(parsed["version"]),
        name=str(pom["name"]),
        feature_requires=list(parsed["requires"]),  # type: ignore[arg-type]
        feature_plugins=list(parsed["plugins"]),  # type: ignore[arg-type]
        feature_includes=list(parsed["includes"]),  # type: ignore[arg-type]
    )


def _index_packages(reactor: Reactor) -> None:
    """Map every exported package to its exporting bundle."""
    for project in reactor.projects.values():
        for package in project.exports:
            existing = reactor.package_owner.get(package)
            if existing and existing != project.id:
                reactor.validations.append(
                    Validation(
                        "warning",
                        project.id,
                        f"package {package} is exported by both {existing} and {project.id}; "
                        "package-level wiring is ambiguous",
                    )
                )
            reactor.package_owner[package] = project.id


def _validate(reactor: Reactor) -> None:
    """Cross-check bundle requirements against the feature declarations.

    A bundle may legitimately import a package supplied by the external target
    platform (``org.osgi.framework``), but a ``Require-Bundle`` on a *workspace*
    bundle whose feature is never imported by the requiring bundle's feature is
    a real product-assembly bug: the feature can install without its
    dependency present.
    """
    features_by_plugin: dict[str, list[Project]] = {}
    for project in reactor.projects.values():
        if project.kind is not Kind.FEATURE:
            continue
        for plugin_id in project.feature_plugins:
            features_by_plugin.setdefault(plugin_id, []).append(project)

    feature_of_bundle: dict[str, str] = {
        plugin_id: features[0].id for plugin_id, features in features_by_plugin.items()
    }

    for project in reactor.projects.values():
        if project.kind is not Kind.BUNDLE:
            continue
        for feature in features_by_plugin.get(project.id, []):
            declared = {requirement.name for requirement in feature.feature_requires}
            declared |= set(feature.feature_includes)
            own_plugins = set(feature.feature_plugins)

            needed_bundles: set[tuple[str, str]] = set()
            for requirement in project.require_bundles:
                if requirement.name in reactor.projects:
                    needed_bundles.add((requirement.name, "Require-Bundle"))
            for requirement in project.imports:
                owner = reactor.package_owner.get(requirement.name)
                if owner and owner != project.id:
                    needed_bundles.add((owner, f"Import-Package {requirement.name}"))

            for bundle_id, why in sorted(needed_bundles):
                if bundle_id in own_plugins:
                    continue
                providing_feature = feature_of_bundle.get(bundle_id)
                if providing_feature is None:
                    reactor.validations.append(
                        Validation(
                            "warning",
                            project.id,
                            f"{why} resolves to {bundle_id}, which no feature packages",
                        )
                    )
                elif providing_feature not in declared:
                    reactor.validations.append(
                        Validation(
                            "warning",
                            feature.id,
                            f"{project.id} needs {bundle_id} ({why}) but "
                            f"{feature.id} does not import {providing_feature}",
                        )
                    )

    for project in reactor.projects.values():
        if project.kind is not Kind.BUNDLE:
            continue
        for requirement in project.imports:
            owner = reactor.package_owner.get(requirement.name)
            if owner is None and requirement.name.startswith("com.northwind"):
                reactor.validations.append(
                    Validation(
                        "error",
                        project.id,
                        f"imports product package {requirement.name} that nothing exports",
                    )
                )
