"""Domain model for the Northwind OMS build pipeline.

The Tycho reactor in this product does *not* express its real dependencies in
the POMs -- every plugin POM is a three-line ``eclipse-plugin`` stanza.  The
truth lives in OSGi metadata:

  * ``META-INF/MANIFEST.MF`` -- ``Import-Package`` / ``Require-Bundle`` /
    ``Export-Package``
  * ``feature.xml``          -- ``<requires><import feature|plugin .../>``,
                                ``<plugin id=.../>``, ``<includes id=.../>``

Everything in this module is a plain data holder; the parsing lives in
``scan.py`` and the graph algorithms in ``graph.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Kind(str, Enum):
    """What sort of reactor project we are looking at."""

    BUNDLE = "bundle"  # packaging: eclipse-plugin
    FEATURE = "feature"  # packaging: eclipse-feature
    AGGREGATOR = "aggregator"  # packaging: pom

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


class EdgeKind(str, Enum):
    """Why one project depends on another.

    The distinction matters for reporting: an ``IMPORT_PACKAGE`` edge is a
    package-level wiring resolved through the target platform, whereas a
    ``FEATURE_PLUGIN`` edge simply means "this feature packages that bundle,
    so the bundle has to exist first".
    """

    IMPORT_PACKAGE = "import-package"
    REQUIRE_BUNDLE = "require-bundle"
    DYNAMIC_IMPORT = "dynamic-import-package"
    FEATURE_REQUIRES = "feature-requires"
    FEATURE_INCLUDES = "feature-includes"
    FEATURE_PLUGIN = "feature-plugin"
    PARENT_POM = "parent-pom"

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


@dataclass(frozen=True)
class Requirement:
    """A single parsed OSGi requirement clause."""

    name: str
    version_range: str = ""
    optional: bool = False
    directives: dict[str, str] = field(default_factory=dict, compare=False)

    def describe(self) -> str:
        text = self.name
        if self.version_range:
            text += f" {self.version_range}"
        if self.optional:
            text += " (optional)"
        return text


@dataclass
class Project:
    """One node of the reactor: a bundle, a feature, or an aggregator POM."""

    id: str
    kind: Kind
    path: Path
    rel_path: str
    component: str
    artifact_id: str = ""
    version: str = ""
    name: str = ""

    # --- bundle metadata -------------------------------------------------
    exports: dict[str, str] = field(default_factory=dict)
    imports: list[Requirement] = field(default_factory=list)
    require_bundles: list[Requirement] = field(default_factory=list)
    dynamic_imports: list[Requirement] = field(default_factory=list)
    source_dirs: list[str] = field(default_factory=list)

    # --- feature metadata ------------------------------------------------
    feature_requires: list[Requirement] = field(default_factory=list)
    feature_plugins: list[str] = field(default_factory=list)
    feature_includes: list[str] = field(default_factory=list)

    # --- reactor structure -----------------------------------------------
    parent_rel_path: str | None = None
    module_rel_paths: list[str] = field(default_factory=list)

    @property
    def is_buildable(self) -> bool:
        """Aggregators are built implicitly by Maven; they are not units of work."""
        return self.kind in (Kind.BUNDLE, Kind.FEATURE)

    @property
    def short_id(self) -> str:
        """``com.northwind.oms.core`` -> ``core`` for compact diagrams."""
        for prefix in ("com.northwind.oms.tpcl.", "com.northwind.oms."):
            if self.id.startswith(prefix):
                return self.id[len(prefix) :]
        return self.id


@dataclass(frozen=True)
class Edge:
    """A directed dependency: ``source`` must be built *after* ``target``."""

    source: str
    target: str
    kind: EdgeKind
    detail: str = ""
    optional: bool = False

    def describe(self) -> str:
        text = f"{self.source} -> {self.target} [{self.kind}"
        if self.detail:
            text += f": {self.detail}"
        text += "]"
        if self.optional:
            text += " (optional)"
        return text


class ChangeReason(str, Enum):
    """Why a project is in the rebuild set.

    ``DIRECTLY_CHANGED`` means a file inside it was touched.  ``DEPENDENT``
    means it consumes something that changed.  ``GLOBAL`` means a shared file
    (the parent POM, the target-platform definition) changed and therefore
    every project is suspect.
    """

    DIRECTLY_CHANGED = "directly-changed"
    DEPENDENT = "dependent"
    GLOBAL = "global"
    FULL_BUILD = "full-build"

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


@dataclass
class FileChange:
    """A changed file, resolved to the project that owns it."""

    rel_path: str
    status: str
    owner_id: str | None
    category: str  # source | manifest | feature | pom | build-properties | other


@dataclass
class Validation:
    """A consistency problem found while scanning the product."""

    severity: str  # error | warning
    project_id: str
    message: str
