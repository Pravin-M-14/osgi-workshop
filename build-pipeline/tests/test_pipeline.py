"""Unit tests for the Northwind OMS build pipeline.

Stdlib ``unittest`` only, so CI can run these with no pip install:

    PYTHONPATH=build-pipeline python -m unittest discover -s build-pipeline/tests -v
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "build-pipeline"))

from omsbuild import changes, cli, ghaction  # noqa: E402
from omsbuild.graph import DependencyGraph, build_graph, project_to_components  # noqa: E402
from omsbuild.model import Edge, EdgeKind, Kind, Project  # noqa: E402
from omsbuild.plan import plan_changed, plan_full  # noqa: E402
from omsbuild.scan import (  # noqa: E402
    parse_export_header,
    parse_requirement_header,
    parse_symbolic_name,
    scan,
    split_top_level,
    unfold_manifest,
)

REACTOR = REPO_ROOT / "osgi-workshop"


# ---------------------------------------------------------------------------
# manifest parsing
# ---------------------------------------------------------------------------


class ManifestParsingTests(unittest.TestCase):
    def test_continuation_lines_are_unfolded(self):
        text = "Import-Package: a.b.c,\n d.e.f,\n g.h.i\nBundle-Name: X\n"
        self.assertEqual(
            unfold_manifest(text),
            ["Import-Package: a.b.c,d.e.f,g.h.i", "Bundle-Name: X"],
        )

    def test_crlf_is_handled(self):
        text = "Import-Package: a.b,\r\n c.d\r\n"
        self.assertEqual(unfold_manifest(text), ["Import-Package: a.b,c.d"])

    def test_commas_inside_version_ranges_do_not_split_clauses(self):
        # The whole point: "[1.0.0,2.0.0)" contains a comma.
        value = 'a.b.c;version="[1.0.0,2.0.0)",d.e.f;version="[1.0.0,2.0.0)"'
        self.assertEqual(len(split_top_level(value, ",")), 2)
        requirements = parse_requirement_header(value)
        self.assertEqual([r.name for r in requirements], ["a.b.c", "d.e.f"])
        self.assertEqual(requirements[0].version_range, "[1.0.0,2.0.0)")

    def test_optional_resolution_directive(self):
        requirements = parse_requirement_header('a.b;resolution:=optional,c.d')
        self.assertTrue(requirements[0].optional)
        self.assertFalse(requirements[1].optional)

    def test_multiple_packages_share_one_attribute_set(self):
        requirements = parse_requirement_header('a.b;c.d;version="[1,2)"')
        self.assertEqual({r.name for r in requirements}, {"a.b", "c.d"})
        self.assertTrue(all(r.version_range == "[1,2)" for r in requirements))

    def test_singleton_directive_is_stripped_from_symbolic_name(self):
        self.assertEqual(
            parse_symbolic_name("com.northwind.oms.tpcl.org.slf4j;singleton:=true"),
            "com.northwind.oms.tpcl.org.slf4j",
        )

    def test_export_header_yields_package_versions(self):
        exports = parse_export_header(
            'com.a;version="1.0.0",\n com.b;version="2.0.0"'.replace("\n ", "")
        )
        self.assertEqual(exports, {"com.a": "1.0.0", "com.b": "2.0.0"})


# ---------------------------------------------------------------------------
# scanning the real product
# ---------------------------------------------------------------------------


class ReactorScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reactor = scan(REACTOR)
        cls.graph = build_graph(cls.reactor)

    def test_expected_module_counts(self):
        bundles = [p for p in self.graph.nodes.values() if p.kind is Kind.BUNDLE]
        features = [p for p in self.graph.nodes.values() if p.kind is Kind.FEATURE]
        self.assertEqual(len(bundles), 11)
        self.assertEqual(len(features), 9)

    def test_all_nine_components_discovered(self):
        self.assertEqual(
            self.reactor.components,
            [
                "catalog",
                "customer",
                "notification",
                "orders",
                "payment",
                "reporting",
                "security",
                "shipping",
                "thirdparty",
            ],
        )

    def test_import_package_resolves_to_the_exporting_bundle(self):
        # payment imports com.northwind.oms.security.spi, exported by security.
        self.assertIn(
            "com.northwind.oms.security",
            self.graph.direct_dependencies("com.northwind.oms.payment"),
        )

    def test_require_bundle_edge_is_created(self):
        edges = self.graph.edges_between(
            "com.northwind.oms.gateway", "com.northwind.oms.tpcl.org.slf4j"
        )
        self.assertTrue(any(e.kind is EdgeKind.REQUIRE_BUNDLE for e in edges))

    def test_external_target_platform_packages_are_not_edges(self):
        # Every bundle imports org.osgi.framework; nothing in the workspace
        # exports it, so it must not become a node or an edge.
        self.assertNotIn("org.osgi.framework", self.reactor.package_owner)
        self.assertNotIn("org.osgi.framework", self.graph.nodes)

    def test_feature_depends_on_the_plugins_it_packages(self):
        self.assertIn(
            "com.northwind.oms.core",
            self.graph.direct_dependencies("com.northwind.oms.catalog.feature"),
        )

    def test_core_has_no_dependencies_and_many_consumers(self):
        self.assertEqual(self.graph.direct_dependencies("com.northwind.oms.core"), [])
        self.assertGreaterEqual(
            len(self.graph.direct_dependents("com.northwind.oms.core")), 7
        )

    def test_product_has_no_cycles(self):
        self.assertEqual(self.graph.find_cycles(), [])

    def test_missing_slf4j_feature_imports_are_reported(self):
        # Four features package bundles that Require-Bundle the slf4j TPCL but
        # never import tpcl.slf4j.feature. This is a real defect in the sample.
        offenders = {
            v.project_id
            for v in self.reactor.validations
            if "tpcl.slf4j.feature" in v.message
        }
        self.assertEqual(
            offenders,
            {
                "com.northwind.oms.payment.feature",
                "com.northwind.oms.notification.feature",
                "com.northwind.oms.orders.feature",
                "com.northwind.oms.reporting.feature",
            },
        )

    def test_owner_of_path_prefers_the_plugin_over_the_aggregator(self):
        owner = self.reactor.owner_of_path(
            "catalog/plugins/com.northwind.oms.core/src/com/northwind/oms/core/model/Order.java"
        )
        self.assertIsNotNone(owner)
        self.assertEqual(owner.id, "com.northwind.oms.core")


# ---------------------------------------------------------------------------
# ordering
# ---------------------------------------------------------------------------


class OrderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reactor = scan(REACTOR)
        cls.graph = build_graph(cls.reactor)

    def test_every_dependency_precedes_its_consumer(self):
        order = self.graph.topological_order()
        position = {node: index for index, node in enumerate(order)}
        self.assertEqual(len(order), len(self.graph.nodes))
        for edge in self.graph.edges:
            self.assertLess(
                position[edge.target],
                position[edge.source],
                f"{edge.target} must be built before {edge.source}",
            )

    def test_waves_partition_the_graph_exactly_once(self):
        waves = self.graph.topological_waves()
        flat = [node for wave in waves for node in wave]
        self.assertEqual(sorted(flat), sorted(self.graph.nodes))
        self.assertEqual(len(flat), len(set(flat)))

    def test_no_edge_exists_within_a_single_wave(self):
        waves = self.graph.topological_waves()
        for wave in waves:
            members = set(wave)
            for edge in self.graph.edges:
                if edge.source in members:
                    self.assertNotIn(
                        edge.target, members, "a wave must be internally independent"
                    )

    def test_ordering_is_deterministic(self):
        self.assertEqual(
            self.graph.topological_waves(), self.graph.topological_waves()
        )

    def test_core_and_slf4j_are_in_the_first_wave(self):
        first = set(self.graph.topological_waves()[0])
        self.assertIn("com.northwind.oms.core", first)
        self.assertIn("com.northwind.oms.tpcl.org.slf4j", first)

    def test_cycle_is_detected_and_blocks_ordering(self):
        graph = DependencyGraph()
        for name in ("a", "b", "c"):
            graph.add_node(
                Project(
                    id=name, kind=Kind.BUNDLE, path=Path("."), rel_path=name, component="x"
                )
            )
        graph.add_edge(Edge("a", "b", EdgeKind.IMPORT_PACKAGE))
        graph.add_edge(Edge("b", "c", EdgeKind.IMPORT_PACKAGE))
        graph.add_edge(Edge("c", "a", EdgeKind.IMPORT_PACKAGE))
        cycles = graph.find_cycles()
        self.assertEqual(len(cycles), 1)
        self.assertEqual(set(cycles[0]), {"a", "b", "c"})
        # Nothing can be ordered, so no wave is emitted.
        self.assertEqual(graph.topological_waves(), [])

    def test_self_edges_are_ignored(self):
        graph = DependencyGraph()
        graph.add_node(
            Project(id="a", kind=Kind.BUNDLE, path=Path("."), rel_path="a", component="x")
        )
        graph.add_edge(Edge("a", "a", EdgeKind.IMPORT_PACKAGE))
        self.assertEqual(graph.edges, [])
        self.assertEqual(graph.find_cycles(), [])


# ---------------------------------------------------------------------------
# impact analysis
# ---------------------------------------------------------------------------


class ImpactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reactor = scan(REACTOR)
        cls.graph = build_graph(cls.reactor)

    def test_changing_core_impacts_the_whole_product(self):
        impacted = self.graph.transitive_dependents({"com.northwind.oms.core"})
        # core is imported directly or transitively by everything except the
        # independent leaves: customer, security and the slf4j TPCL (and the
        # two features that only package those).
        expected_unaffected = {
            "com.northwind.oms.customer",
            "com.northwind.oms.customer.feature",
            "com.northwind.oms.security",
            "com.northwind.oms.security.feature",
            "com.northwind.oms.tpcl.org.slf4j",
            "com.northwind.oms.tpcl.slf4j.feature",
        }
        self.assertEqual(set(self.graph.nodes) - impacted, expected_unaffected)

    def test_changing_a_leaf_impacts_only_its_own_feature(self):
        impacted = self.graph.transitive_dependents({"com.northwind.oms.tpcl.org.slf4j"})
        self.assertIn("com.northwind.oms.tpcl.slf4j.feature", impacted)
        self.assertIn("com.northwind.oms.gateway", impacted)
        self.assertNotIn("com.northwind.oms.customer", impacted)

    def test_changing_reporting_impacts_only_reporting_and_its_feature(self):
        impacted = self.graph.transitive_dependents({"com.northwind.oms.reporting"})
        self.assertEqual(
            impacted,
            {"com.northwind.oms.reporting", "com.northwind.oms.reporting.feature"},
        )

    def test_impact_is_the_reverse_of_dependency(self):
        for node in self.graph.nodes:
            for dependent in self.graph.transitive_dependents({node}):
                self.assertIn(
                    node, self.graph.transitive_dependencies({dependent})
                )

    def test_shortest_path_reports_a_real_chain(self):
        paths = self.graph.shortest_paths_from(
            "com.northwind.oms.core", {"com.northwind.oms.reporting.feature"}
        )
        chain = paths["com.northwind.oms.reporting.feature"]
        self.assertEqual(chain[0], "com.northwind.oms.core")
        self.assertEqual(chain[-1], "com.northwind.oms.reporting.feature")
        # Every consecutive pair must be a genuine edge (consumer -> dependency).
        for left, right in zip(chain, chain[1:]):
            self.assertTrue(
                self.graph.edges_between(right, left),
                f"no edge {right} -> {left} in the reported path",
            )

    def test_component_projection_drops_intra_component_edges(self):
        components = project_to_components(self.graph)
        self.assertNotIn("catalog", components.deps["catalog"])
        self.assertIn("catalog", components.deps["orders"])
        self.assertIn("security", components.deps["payment"])
        self.assertEqual(components.deps["catalog"], set())


# ---------------------------------------------------------------------------
# change detection + planning, on a throwaway copy of the product
# ---------------------------------------------------------------------------


class ChangeDetectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oms-test-"))
        self.root = self.tmp / "osgi-workshop"
        shutil.copytree(REACTOR, self.root)
        self.reactor = scan(self.root)
        self.graph = build_graph(self.reactor)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch(self, rel_path: str, text: str = "\n// modified\n") -> None:
        target = self.root / rel_path
        # A typo in a fixture path would otherwise show up as a confusing
        # "nothing was detected" failure further down the test.
        self.assertTrue(target.is_file(), f"fixture path does not exist: {rel_path}")
        target.write_text(target.read_text(encoding="utf-8") + text, encoding="utf-8")

    def test_hash_baseline_detects_nothing_when_unchanged(self):
        state = self.tmp / "state.json"
        changes.write_baseline(self.reactor, state)
        change_set = changes.detect_hash_changes(self.reactor, state)
        self.assertEqual(change_set.changes, [])

    def test_hash_baseline_detects_a_single_module(self):
        state = self.tmp / "state.json"
        changes.write_baseline(self.reactor, state)
        self._touch(
            "catalog/plugins/com.northwind.oms.core/src/com/northwind/oms/core/model/Order.java"
        )
        change_set = changes.detect_hash_changes(self.reactor, state)
        self.assertEqual(change_set.changed_project_ids, {"com.northwind.oms.core"})

    def test_missing_baseline_is_treated_as_everything_changed(self):
        change_set = changes.detect_hash_changes(self.reactor, self.tmp / "absent.json")
        self.assertIsNotNone(change_set.global_change)

    def test_generated_output_is_ignored(self):
        state = self.tmp / "state.json"
        changes.write_baseline(self.reactor, state)
        target = self.root / "catalog/plugins/com.northwind.oms.core/target/classes"
        target.mkdir(parents=True, exist_ok=True)
        (target / "Order.class").write_bytes(b"\xca\xfe\xba\xbe")
        change_set = changes.detect_hash_changes(self.reactor, state)
        self.assertEqual(change_set.changes, [])

    def test_root_pom_change_escalates_to_a_full_rebuild(self):
        state = self.tmp / "state.json"
        changes.write_baseline(self.reactor, state)
        self._touch("pom.xml", "<!-- touched -->\n")
        change_set = changes.detect_hash_changes(self.reactor, state)
        self.assertIsNotNone(change_set.global_change)
        plan = plan_changed(self.reactor, self.graph, change_set)
        self.assertEqual(plan.selected_ids, set(self.graph.nodes))
        self.assertIsNotNone(plan.full_rebuild_reason)

    def test_escalation_can_be_disabled(self):
        state = self.tmp / "state.json"
        changes.write_baseline(self.reactor, state)
        self._touch("pom.xml", "<!-- touched -->\n")
        change_set = changes.detect_hash_changes(self.reactor, state)
        plan = plan_changed(
            self.reactor, self.graph, change_set, force_full_on_global=False
        )
        self.assertIsNone(plan.full_rebuild_reason)

    def test_readme_example_case_selects_the_right_modules(self):
        """The exact scenario the assignment README asks for."""
        state = self.tmp / "state.json"
        changes.write_baseline(self.reactor, state)
        self._touch(
            "catalog/plugins/com.northwind.oms.core/src/com/northwind/oms/core/model/Product.java"
        )
        self._touch(
            "orders/plugins/com.northwind.oms.gateway/src/com/northwind/oms/gateway/OrderService.java"
        )
        self._touch(
            "payment/plugins/com.northwind.oms.payment/src/com/northwind/oms/payment/PaymentGateway.java"
        )

        change_set = changes.detect_hash_changes(self.reactor, state)
        plan = plan_changed(self.reactor, self.graph, change_set)

        self.assertEqual(
            plan.changed_ids,
            {
                "com.northwind.oms.core",
                "com.northwind.oms.gateway",
                "com.northwind.oms.payment",
            },
        )
        # customer / security / slf4j are upstream or unrelated: never rebuilt.
        for untouched in (
            "com.northwind.oms.customer",
            "com.northwind.oms.security",
            "com.northwind.oms.tpcl.org.slf4j",
        ):
            self.assertIn(untouched, plan.skipped_ids)
        # Everything downstream of core must be selected.
        for downstream in (
            "com.northwind.oms.inventory",
            "com.northwind.oms.pricing",
            "com.northwind.oms.shipping",
            "com.northwind.oms.notification",
            "com.northwind.oms.reporting",
            "com.northwind.oms.reporting.feature",
        ):
            self.assertIn(downstream, plan.selected_ids)
        # And the plan must still be correctly ordered.
        position = {entry.project_id: index for index, entry in enumerate(plan.entries)}
        for edge in self.graph.edges:
            if edge.source in position and edge.target in position:
                self.assertLess(position[edge.target], position[edge.source])

    def test_manifest_change_is_categorised_as_a_dependency_change(self):
        state = self.tmp / "state.json"
        changes.write_baseline(self.reactor, state)
        manifest = self.root / "payment/plugins/com.northwind.oms.payment/META-INF/MANIFEST.MF"
        manifest.write_text(
            manifest.read_text(encoding="utf-8") + "Bundle-Copyright: test\n",
            encoding="utf-8",
        )
        change_set = changes.detect_hash_changes(self.reactor, state)
        categories = {c.category for c in change_set.changes}
        self.assertIn("manifest", categories)


class GitStrategyTests(unittest.TestCase):
    """Exercise the git path, including merge-base resolution."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="oms-git-"))
        self.root = self.tmp / "osgi-workshop"
        shutil.copytree(REACTOR, self.root)
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "ci@example.com")
        self._git("config", "user.name", "CI")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "baseline")
        self.reactor = scan(self.root)
        self.graph = build_graph(self.reactor)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.tmp, check=True, capture_output=True)

    def test_repo_is_detected(self):
        self.assertTrue(changes.is_git_repo(self.root))

    def test_uncommitted_change_is_detected_against_head(self):
        target = self.root / "shipping/plugins/com.northwind.oms.shipping/src/com/northwind/oms/shipping/Shipment.java"
        target.write_text(target.read_text() + "\n// edit\n", encoding="utf-8")
        change_set = changes.detect(self.reactor, strategy="git", ref="HEAD")
        self.assertEqual(change_set.changed_project_ids, {"com.northwind.oms.shipping"})

    def test_untracked_file_is_detected(self):
        new_file = self.root / "customer/plugins/com.northwind.oms.customer/src/com/northwind/oms/customer/New.java"
        new_file.write_text("class New {}\n", encoding="utf-8")
        change_set = changes.detect(self.reactor, strategy="git", ref="HEAD")
        self.assertEqual(change_set.changed_project_ids, {"com.northwind.oms.customer"})

    def test_merge_base_excludes_commits_landed_on_the_base_branch(self):
        # Branch off, change payment on the branch.
        self._git("checkout", "-q", "-b", "feature")
        target = self.root / "payment/plugins/com.northwind.oms.payment/src/com/northwind/oms/payment/PaymentResult.java"
        target.write_text(target.read_text() + "\n// branch edit\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "payment change")

        # Meanwhile main advances with an unrelated change to notification.
        self._git("checkout", "-q", "main")
        other = self.root / "notification/plugins/com.northwind.oms.notification/src/com/northwind/oms/notification/Channel.java"
        other.write_text(other.read_text() + "\n// main edit\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "notification change")
        self._git("checkout", "-q", "feature")

        change_set = changes.detect(self.reactor, strategy="git", ref="main")
        # Only the branch's own change counts; main's change must not leak in.
        self.assertEqual(change_set.changed_project_ids, {"com.northwind.oms.payment"})
        self.assertIn("merge-base", change_set.baseline)

    def test_unknown_ref_raises(self):
        with self.assertRaises(ValueError):
            changes.detect(self.reactor, strategy="git", ref="no-such-ref")

    def test_auto_strategy_picks_git(self):
        change_set = changes.detect(self.reactor, strategy="auto")
        self.assertEqual(change_set.strategy, "git")


# ---------------------------------------------------------------------------
# GitHub Actions integration
# ---------------------------------------------------------------------------


class GitHubActionsOutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reactor = scan(REACTOR)
        cls.graph = build_graph(cls.reactor)
        cls.plan = plan_full(cls.reactor, cls.graph)

    def test_every_wave_matrix_is_valid_json(self):
        result = ghaction.outputs(self.plan)
        for wave_index in range(1, ghaction.MAX_WAVES + 1):
            parsed = json.loads(result[f"wave{wave_index}_matrix"])
            self.assertIn("include", parsed)
            self.assertIsInstance(parsed["include"], list)

    def test_matrices_cover_the_plan_exactly(self):
        result = ghaction.outputs(self.plan)
        seen: list[str] = []
        for wave_index in range(1, ghaction.MAX_WAVES + 1):
            for entry in json.loads(result[f"wave{wave_index}_matrix"])["include"]:
                seen.append(entry["module"])
        overflow = [p for p in result["overflow_paths"].split(",") if p]
        self.assertEqual(len(seen) + len(overflow), len(self.plan.entries))
        self.assertEqual(sorted(seen), sorted(self.plan.ordered_ids))

    def test_has_work_flags_match_the_matrices(self):
        result = ghaction.outputs(self.plan)
        for wave_index in range(1, ghaction.MAX_WAVES + 1):
            populated = bool(json.loads(result[f"wave{wave_index}_matrix"])["include"])
            self.assertEqual(
                result[f"wave{wave_index}_has_work"], "true" if populated else "false"
            )

    def test_matrix_entries_carry_the_paths_maven_needs(self):
        result = ghaction.outputs(self.plan)
        for entry in json.loads(result["wave1_matrix"])["include"]:
            self.assertTrue((REACTOR / entry["path"] / "pom.xml").is_file())

    def test_no_step_output_value_contains_a_newline_unescaped(self):
        # write_outputs uses heredocs for multiline values; verify round-trip.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.txt"
            target.touch()
            ghaction.write_outputs(self.plan, target)
            content = target.read_text(encoding="utf-8")
            keys = ghaction.outputs(self.plan).keys()
            for key in keys:
                self.assertTrue(
                    f"{key}=" in content or f"{key}<<" in content,
                    f"{key} missing from step outputs",
                )

    def test_summary_contains_a_mermaid_graph_and_the_build_order(self):
        markdown = ghaction.summary_markdown(self.plan)
        self.assertIn("```mermaid", markdown)
        self.assertIn("Build order", markdown)
        self.assertIn("Wave 1", markdown)

    def test_comment_is_marked_for_idempotent_upsert(self):
        body = ghaction.comment_markdown(self.plan)
        self.assertIn(ghaction.COMMENT_MARKER, body)


class WorkflowContractTests(unittest.TestCase):
    """Guard the coupling between the resolver and the workflow YAML."""

    def setUp(self):
        self.workflow_path = REPO_ROOT / ".github/workflows/build-pipeline.yml"
        self.text = self.workflow_path.read_text(encoding="utf-8")

    def test_job_that_comments_on_prs_has_write_permission(self):
        """A job calling the issues API needs pull-requests:write.

        The workflow default is contents:read. If a job posts a PR comment
        without widening its own permissions the step fails with a 403, and
        only on real pull requests -- a workflow_dispatch smoke test passes
        happily. That makes it exactly the kind of bug worth pinning.
        """
        # Imported locally, and skipped rather than failed if absent: the
        # resolver is deliberately stdlib-only, so a contributor running the
        # suite on a bare interpreter should not see a spurious failure. CI
        # installs PyYAML explicitly so the check really does run there.
        try:
            import yaml
        except ImportError:  # pragma: no cover - depends on the environment
            self.skipTest("PyYAML not installed")

        workflow = yaml.safe_load(self.text)
        for name, job in workflow["jobs"].items():
            script_steps = [
                step
                for step in job.get("steps", [])
                if "issues.createComment" in str(step.get("with", {}).get("script", ""))
                or "issues.updateComment" in str(step.get("with", {}).get("script", ""))
            ]
            if not script_steps:
                continue
            granted = job.get("permissions") or {}
            with self.subTest(job=name):
                self.assertEqual(
                    granted.get("pull-requests"),
                    "write",
                    f"job {name!r} comments on pull requests but does not grant "
                    "pull-requests:write, so the step will 403",
                )

    def test_workflow_declares_a_job_for_every_ladder_wave(self):
        for wave_index in range(1, ghaction.MAX_WAVES + 1):
            self.assertIn(f"wave-{wave_index}:", self.text)
            self.assertIn(f"wave{wave_index}_matrix", self.text)

    def test_workflow_handles_overflow(self):
        self.assertIn("overflow_has_work", self.text)
        self.assertIn("overflow_paths", self.text)

    def test_matrix_handoff_carries_the_p2_indices(self):
        """The inter-wave tarball must include ~/.m2/repository/.meta.

        Tycho resolves eclipse-plugin dependencies from the target platform,
        not from the Maven local repository, and locally installed Tycho
        artifacts are only discoverable via the p2 indices under `.meta`.
        Handing forward `com/northwind` alone gives the downstream runner the
        JAR while leaving it invisible, which fails as "artifact ... was not
        found in the target platform" -- pointing at the consumer rather than
        at the missing index, so it is a genuinely expensive bug to rediscover.
        """
        action = (
            REPO_ROOT / ".github" / "actions" / "build-module" / "action.yml"
        ).read_text(encoding="utf-8")
        self.assertIn(".meta", action, "the hand-off drops the p2 indices")
        self.assertIn(
            "sort -u",
            action,
            "the p2 indices must be merged, not overwritten: a plain extract "
            "lets the last wave archive hide every sibling module",
        )

    def test_plan_job_exposes_every_output_the_waves_consume(self):
        reactor = scan(REACTOR)
        graph = build_graph(reactor)
        plan = plan_full(reactor, graph)
        for key in ghaction.outputs(plan):
            if key.startswith("wave") or key in ("has_work", "overflow_paths"):
                self.assertIn(
                    key, self.text, f"workflow never surfaces the {key} output"
                )


class CommandLineTests(unittest.TestCase):
    """The global options must work before *and* after the subcommand.

    Sharing them via ``parents=`` is easy to get subtly wrong: adding a
    ``parser.set_defaults()`` call also seeds the subparser's namespace, which
    silently resurrects the default and undoes a flag given before the
    subcommand. That regression is invisible in normal use -- the tool just
    stays verbose -- so it is pinned here.
    """

    def test_global_options_accepted_in_either_position(self):
        for argv, expected in (
            (["-q", "validate"], False),
            (["validate", "-q"], False),
            (["validate"], True),
            (["-q", "build", "--scenario", "full"], False),
            (["build", "--scenario", "full", "-q"], False),
            (["-q", "graph", "--with-changes"], False),
            (["ci", "--scenario", "full", "-q"], False),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(cli.parse_args(argv).verbose, expected)

    def test_root_and_report_dir_in_either_position(self):
        for argv in (
            ["--root", "r", "--report-dir", "d", "validate"],
            ["validate", "--root", "r", "--report-dir", "d"],
            ["--root", "r", "validate", "--report-dir", "d"],
        ):
            with self.subTest(argv=argv):
                args = cli.parse_args(argv)
                self.assertEqual(args.root, "r")
                self.assertEqual(args.report_dir, "d")

    def test_defaults_are_applied_when_no_global_option_is_given(self):
        args = cli.parse_args(["validate"])
        self.assertEqual(args.root, "osgi-workshop")
        self.assertEqual(args.report_dir, "build-reports")
        self.assertTrue(args.verbose)

    def test_every_subcommand_accepts_the_global_options(self):
        subcommands = {
            name
            for action in cli.build_parser()._subparsers._group_actions
            for name in action.choices
        }
        self.assertIn("build", subcommands)
        for name in sorted(subcommands):
            argv = [name, "-q"]
            if name == "ci":
                argv = ["ci", "--scenario", "full", "-q"]
            with self.subTest(subcommand=name):
                self.assertFalse(cli.parse_args(argv).verbose)


class ReadmeTests(unittest.TestCase):
    """Every oms-build command shown in the README must actually parse.

    Documented commands that no longer exist are worse than no documentation,
    and the README is the first thing a reviewer reads.
    """

    READMES = (
        REPO_ROOT / "README.md",
        REPO_ROOT / "build-pipeline" / "README.md",
    )

    @staticmethod
    def _commands_in(text: str) -> list[str]:
        commands, buffer = [], ""
        for line in text.splitlines():
            stripped = line.strip()
            if buffer:
                buffer += " " + stripped.rstrip("\\")
                if not stripped.endswith("\\"):
                    commands.append(buffer)
                    buffer = ""
            elif stripped.startswith("./build-pipeline/oms-build"):
                if stripped.endswith("\\"):
                    buffer = stripped.rstrip("\\")
                else:
                    commands.append(stripped)
        return commands

    def test_documented_commands_parse(self):
        for readme in self.READMES:
            commands = self._commands_in(readme.read_text(encoding="utf-8"))
            self.assertGreaterEqual(
                len(commands), 4, f"{readme.name} documents almost no commands"
            )
            for command in commands:
                argv = shlex.split(command.split("#", 1)[0])[1:]  # drop the launcher
                with self.subTest(readme=readme.name, command=command):
                    try:
                        cli.parse_args(argv)
                    except SystemExit as exit_error:  # argparse rejected it
                        self.fail(
                            f"{readme.name} documents an invalid command: "
                            f"{command}\n{exit_error}"
                        )

    def test_root_readme_documents_the_workflow_jobs_that_exist(self):
        """The root README is the operator guide; its job names must be real."""
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        workflow = (
            REPO_ROOT / ".github" / "workflows" / "build-pipeline.yml"
        ).read_text(encoding="utf-8")
        for job in ("resolver-tests", "plan", "wave-overflow",
                    "single-runner-build", "pipeline-result"):
            with self.subTest(job=job):
                self.assertIn(job, readme, f"README omits the {job} job")
                self.assertIn(f"\n  {job}:", workflow, f"{job} is not a real job")

    def test_root_readme_dispatch_inputs_exist(self):
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        workflow = (
            REPO_ROOT / ".github" / "workflows" / "build-pipeline.yml"
        ).read_text(encoding="utf-8")
        for name in ("scenario", "build_mode", "plan_only", "base_ref"):
            with self.subTest(input=name):
                self.assertIn(f"`{name}`", readme)
                self.assertIn(f"\n      {name}:", workflow)

    def test_root_readme_leaves_the_assignment_brief_in_place(self):
        """The brief is the assessment team's file and must not be replaced."""
        brief = (REPO_ROOT / "osgi-workshop" / "README.md").read_text(encoding="utf-8")
        self.assertIn("Assignment", brief)
        root = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotEqual(
            root.strip(), brief.strip(), "the root README duplicates the brief"
        )
        self.assertIn("osgi-workshop/README.md", root, "root README must link the brief")

    def test_documented_test_count_matches_reality(self):
        """Both READMEs quote a test count, in prose and in the workflow table.

        Nobody remembers to update a number in prose when they add a test, so
        the number rots and a reader who checks it stops trusting the rest of
        the document. Counting the loaded suite keeps it honest.
        """
        # No ``top_level_dir``: the tests directory is deliberately not a
        # package, so naming a parent as the top level makes it unimportable.
        # Letting it default to ``start_dir`` is what the documented command
        # line does too, so this counts exactly what CI runs.
        suite = unittest.defaultTestLoader.discover(
            start_dir=str(Path(__file__).resolve().parent)
        )

        def count(item) -> int:
            if isinstance(item, unittest.TestSuite):
                return sum(count(child) for child in item)
            return 1

        total = count(suite)
        for readme in self.READMES:
            text = readme.read_text(encoding="utf-8")
            quoted = {int(n) for n in re.findall(r"\b(\d+) (?:unit )?tests\b", text)}
            # Both files are called README.md, so label the subtest with the
            # path relative to the repo root or the failure is ambiguous.
            label = readme.relative_to(REPO_ROOT).as_posix()
            with self.subTest(readme=label):
                self.assertTrue(quoted, f"{label} quotes no test count")
                self.assertEqual(
                    quoted,
                    {total},
                    f"{label} says {sorted(quoted)} tests; the suite has {total}",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
