"""Command-line interface for the Northwind OMS smart build pipeline."""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
from pathlib import Path

from . import changes as change_detection
from . import ghaction, render, runner
from .graph import build_graph
from .model import ChangeReason, Kind
from .plan import BuildPlan, plan_changed, plan_full
from .scan import scan

BANNER_WIDTH = 78


# ---------------------------------------------------------------------------
# logging helpers
# ---------------------------------------------------------------------------


class Log:
    def __init__(self, verbose: bool = True) -> None:
        self.verbose = verbose

    def banner(self, text: str) -> None:
        print()
        print("=" * BANNER_WIDTH)
        print(f"  {text}")
        print("=" * BANNER_WIDTH)

    def section(self, text: str) -> None:
        print()
        print(f"--- {text} " + "-" * max(0, BANNER_WIDTH - 6 - len(text)))

    def info(self, text: str = "") -> None:
        print(text)

    def detail(self, text: str) -> None:
        if self.verbose:
            print(text)

    def warn(self, text: str) -> None:
        print(f"  [WARN] {text}")

    def error(self, text: str) -> None:
        print(f"  [ERROR] {text}", file=sys.stderr)


# ---------------------------------------------------------------------------
# shared reporting
# ---------------------------------------------------------------------------


def _report_reactor(log: Log, reactor, graph) -> None:
    log.section("Product inventory")
    bundles = [p for p in graph.nodes.values() if p.kind is Kind.BUNDLE]
    features = [p for p in graph.nodes.values() if p.kind is Kind.FEATURE]
    log.info(f"  reactor root      : {reactor.root}")
    log.info(f"  components        : {len(reactor.components)} ({', '.join(reactor.components)})")
    log.info(f"  plugin bundles    : {len(bundles)}")
    log.info(f"  features          : {len(features)}")
    log.info(f"  dependency edges  : {len(graph.edges)}")
    log.info(f"  exported packages : {len(reactor.package_owner)}")

    if log.verbose:
        log.section("Resolved dependency edges (source depends on target)")
        for edge in sorted(graph.edges, key=lambda e: (e.source, str(e.kind), e.target)):
            log.info(f"  {edge.describe()}")


def _report_validations(log: Log, reactor) -> int:
    if not reactor.validations:
        log.section("Consistency checks")
        log.info("  no metadata inconsistencies found")
        return 0
    log.section(f"Consistency checks ({len(reactor.validations)} finding(s))")
    errors = 0
    for validation in reactor.validations:
        if validation.severity == "error":
            errors += 1
            log.error(f"{validation.project_id}: {validation.message}")
        else:
            log.warn(f"{validation.project_id}: {validation.message}")
    return errors


def _report_plan(log: Log, plan: BuildPlan) -> None:
    if plan.cycles:
        log.section("Dependency cycles")
        for cycle in plan.cycles:
            log.error("cycle: " + " -> ".join(cycle) + f" -> {cycle[0]}")

    if plan.change_set is not None:
        change_set = plan.change_set
        log.section("Change detection")
        log.info(f"  strategy : {change_set.strategy}")
        log.info(f"  baseline : {change_set.baseline}")
        log.info(f"  files    : {len(change_set.changes)}")
        if not change_set.changes:
            log.info("  (no changes detected)")
        for change in change_set.changes:
            owner = change.owner_id or "UNOWNED"
            log.info(f"    {change.status:<2} {change.rel_path}")
            log.detail(f"         category={change.category} owner={owner}")
        if change_set.unowned:
            log.warn(
                f"{len(change_set.unowned)} changed file(s) belong to no reactor module "
                "and cannot trigger a rebuild"
            )
        if change_set.global_change:
            log.warn(change_set.global_change)

        log.section(f"Directly changed modules ({len(plan.changed_ids)})")
        if not plan.changed_ids:
            log.info("  (none)")
        for project_id in sorted(plan.changed_ids):
            project = plan.graph.nodes[project_id]
            files = [c.rel_path for c in change_set.files_for(project_id)]
            log.info(f"  {project_id}  [{project.kind}, component={project.component}]")
            for file_path in files:
                log.info(f"      changed: {file_path}")

        log.section("Dependency chain resolution (impact analysis)")
        if plan.changed_ids:
            for line in render.impact_tree(plan):
                log.info("  " + line if line else "")
            for seed, paths in sorted(plan.impact_paths.items()):
                log.detail(f"  paths from {seed}:")
                for target, chain in sorted(paths.items()):
                    log.detail(f"    {' -> '.join(chain)}")
        else:
            log.info("  (nothing to propagate)")

    log.section(f"Build order ({len(plan.entries)} module(s), {len(plan.waves)} wave(s))")
    if not plan.entries:
        log.info("  (empty plan -- no module needs rebuilding)")
    position = 0
    for wave_index, wave in enumerate(plan.waves, start=1):
        log.info(f"  wave {wave_index}  (these can build in parallel)")
        for project_id in wave:
            position += 1
            entry = plan.entry(project_id)
            assert entry is not None
            reason = str(entry.reason)
            triggers = (
                f"  <- triggered by {', '.join(entry.triggers)}"
                if entry.triggers and entry.reason is not ChangeReason.DIRECTLY_CHANGED
                else ""
            )
            log.info(f"    {position:>3}. {project_id:<42} [{reason}]{triggers}")
            log.detail(f"         path: {entry.rel_path}")

    if plan.scenario == "changed":
        total = len(plan.graph.nodes)
        log.section("Rebuild summary")
        log.info(f"  modules in product : {total}")
        log.info(f"  directly changed   : {len(plan.changed_ids)}")
        log.info(f"  selected to rebuild: {len(plan.selected_ids)}")
        log.info(f"  skipped            : {len(plan.skipped_ids)}")
        if plan.skipped_ids:
            for project_id in sorted(plan.skipped_ids):
                log.info(f"    skip {project_id}  (no dependency path from any change)")
        log.info(f"  component order    : {' -> '.join(str(w) for w in plan.component_waves)}")


def _write_artifacts(
    log: Log,
    out_dir: Path,
    plans: list[tuple[str, BuildPlan]],
    maven_commands: dict[str, list[str]],
) -> Path:
    timestamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report_path = render.write_report(out_dir, plans, maven_commands, timestamp)
    markdown_path = render.write_markdown(out_dir, plans)
    for stem, plan in plans:
        (out_dir / f"{stem}.json").write_text(render.to_json(plan), encoding="utf-8")

    log.section("Generated artifacts")
    log.info(f"  {report_path}")
    log.info(f"  {markdown_path}")
    for stem, _ in plans:
        for suffix in ("dot", "svg", "png", "mmd", "json"):
            candidate = out_dir / f"{stem}.{suffix}"
            if candidate.is_file():
                log.info(f"  {candidate}")
    if not render.has_graphviz():
        log.warn("graphviz 'dot' not found: SVG/PNG were not rendered (DOT + Mermaid still written)")
    return report_path


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def _load(args) -> tuple[Log, object, object]:
    log = Log(verbose=args.verbose)
    reactor = scan(Path(args.root))
    graph = build_graph(reactor)
    return log, reactor, graph


def command_modules(args) -> int:
    log, reactor, graph = _load(args)
    log.banner("Northwind OMS -- reactor inventory")
    _report_reactor(log, reactor, graph)
    log.section("Modules")
    for project_id, project in sorted(graph.nodes.items()):
        log.info(f"  {project_id}")
        log.info(f"      kind      : {project.kind}")
        log.info(f"      component : {project.component}")
        log.info(f"      path      : {project.rel_path}")
        log.info(f"      version   : {project.version}")
        if project.exports:
            log.info(f"      exports   : {', '.join(sorted(project.exports))}")
        dependencies = graph.direct_dependencies(project_id)
        dependents = graph.direct_dependents(project_id)
        log.info(f"      depends on: {', '.join(dependencies) or '-'}")
        log.info(f"      used by   : {', '.join(dependents) or '-'}")
    log.section("Forward dependency trees")
    roots = [
        project_id
        for project_id in sorted(graph.nodes)
        if not graph.direct_dependents(project_id)
    ]
    for line in render.ascii_tree(graph, roots):
        log.info("  " + line if line else "")
    _report_validations(log, reactor)
    return 0


def command_validate(args) -> int:
    log, reactor, graph = _load(args)
    log.banner("Northwind OMS -- product consistency check")
    _report_reactor(log, reactor, graph)
    errors = _report_validations(log, reactor)
    cycles = graph.find_cycles()
    if cycles:
        log.section("Dependency cycles")
        for cycle in cycles:
            log.error("cycle: " + " -> ".join(cycle))
        errors += len(cycles)
    return 1 if (errors and args.strict) else 0


def command_baseline(args) -> int:
    log, reactor, _ = _load(args)
    state_path = Path(args.state) if args.state else reactor.root / change_detection.DEFAULT_STATE_FILE
    count = change_detection.write_baseline(reactor, state_path)
    log.banner("Northwind OMS -- change-detection baseline recorded")
    log.info(f"  fingerprinted {count} file(s)")
    log.info(f"  state file: {state_path}")
    log.info("  modify files and re-run 'build --scenario changed' to see the impact analysis")
    return 0


def _resolve_changed_plan(log: Log, args, reactor, graph) -> BuildPlan:
    state_path = Path(args.state) if args.state else None
    change_set = change_detection.detect(
        reactor, strategy=args.strategy, ref=args.ref, state_path=state_path
    )
    return plan_changed(
        reactor,
        graph,
        change_set,
        force_full_on_global=not args.no_global_escalation,
    )


def command_graph(args) -> int:
    log, reactor, graph = _load(args)
    log.banner("Northwind OMS -- dependency graph")
    _report_reactor(log, reactor, graph)

    plans: list[tuple[str, BuildPlan]] = [("graph-full", plan_full(reactor, graph))]
    _report_plan(log, plans[0][1])

    if args.with_changes:
        try:
            changed_plan = _resolve_changed_plan(log, args, reactor, graph)
        except ValueError as error:
            log.warn(f"changed-modules view unavailable: {error}")
        else:
            log.banner("Changed-modules scenario")
            _report_plan(log, changed_plan)
            plans.append(("graph-changed", changed_plan))

    _report_validations(log, reactor)
    report_path = _write_artifacts(log, Path(args.report_dir), plans, {})
    log.info()
    log.info(f"  open the report: {report_path}")
    return 0


def command_ci(args) -> int:
    """Plan-only entry point for CI: emit step outputs, a job summary and reports.

    Deliberately does not invoke Maven.  The workflow's ``plan`` job runs this
    to decide *what* to build; separate wave jobs do the building.  That split
    keeps the expensive JDK/Tycho setup out of the decision step.
    """
    log, reactor, graph = _load(args)
    log.banner(f"Northwind OMS build pipeline -- CI planning ({args.scenario})")
    _report_reactor(log, reactor, graph)
    _report_validations(log, reactor)

    if args.scenario == "full":
        plan = plan_full(reactor, graph)
        stem = "build-full"
    else:
        try:
            plan = _resolve_changed_plan(log, args, reactor, graph)
        except ValueError as error:
            log.error(str(error))
            return 2
        stem = "build-changed"

    _report_plan(log, plan)

    plans: list[tuple[str, BuildPlan]] = []
    if args.scenario == "changed":
        plans.append(("build-full", plan_full(reactor, graph)))
    plans.append((stem, plan))
    report_path = _write_artifacts(log, Path(args.report_dir), plans, {})

    log.section("GitHub Actions integration")
    output_path = ghaction.write_outputs(
        plan, Path(args.output_file) if args.output_file else None
    )
    log.info(f"  step outputs : {output_path or 'not written (no $GITHUB_OUTPUT)'}")

    summary_path = ghaction.write_summary(
        plan, Path(args.summary_file) if args.summary_file else None, args.run_url
    )
    log.info(f"  job summary  : {summary_path or 'not written (no $GITHUB_STEP_SUMMARY)'}")

    if args.comment_file:
        comment_path = Path(args.comment_file)
        comment_path.parent.mkdir(parents=True, exist_ok=True)
        comment_path.write_text(ghaction.comment_markdown(plan, args.run_url), encoding="utf-8")
        log.info(f"  pr comment   : {comment_path}")

    log.info(f"  html report  : {report_path}")

    if log.verbose:
        log.section("Resolved step outputs")
        for key, value in ghaction.outputs(plan).items():
            preview = value if len(value) <= 120 else value[:117] + "..."
            log.info(f"  {key}={preview}")

    if plan.cycles and args.fail_on_cycle:
        log.error("dependency cycles present; failing the planning job")
        return 3
    return 0


def command_build(args) -> int:
    log, reactor, graph = _load(args)
    scenario_label = "build all products" if args.scenario == "full" else "build only changed products"
    log.banner(f"Northwind OMS smart build pipeline -- {scenario_label}")
    _report_reactor(log, reactor, graph)
    _report_validations(log, reactor)

    if args.scenario == "full":
        plan = plan_full(reactor, graph)
        stem = "build-full"
    else:
        try:
            plan = _resolve_changed_plan(log, args, reactor, graph)
        except ValueError as error:
            log.error(str(error))
            return 2
        stem = "build-changed"

    _report_plan(log, plan)

    if plan.cycles and args.fail_on_cycle:
        log.error("refusing to build: dependency cycles present (pass --no-fail-on-cycle to override)")
        return 3

    options = runner.MavenOptions(
        goals=tuple(args.goals.split()),
        threads=args.threads,
        offline=args.offline,
        extra_args=args.maven_arg or [],
        maven_executable=args.maven,
    )

    log.section("Maven / Tycho invocation")
    log_path = Path(args.report_dir) / f"{stem}-maven.log"
    Path(args.report_dir).mkdir(parents=True, exist_ok=True)
    result = runner.run(plan, options, dry_run=args.dry_run, log_path=log_path, echo=log.info)

    plans: list[tuple[str, BuildPlan]] = []
    if args.scenario == "changed" and args.include_full_graph:
        plans.append(("build-full", plan_full(reactor, graph)))
    plans.append((stem, plan))

    report_path = _write_artifacts(
        log, Path(args.report_dir), plans, {stem: result.command}
    )

    log.section("Result")
    if result.skipped_reason:
        log.info(f"  {result.skipped_reason}: Maven was not executed")
    else:
        log.info(f"  exit code : {result.exit_code}")
        log.info(f"  duration  : {result.duration_seconds:.1f}s")
        if result.log_path and result.log_path.is_file():
            log.info(f"  maven log : {result.log_path}")
    log.info(f"  report    : {report_path}")

    return 0 if result.ok else 1


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    # The global options are declared once here and attached both to the
    # top-level parser and to every subparser via `parents=`.  Declaring them
    # only at the top level is the argparse default, but it makes
    # `oms-build validate -q` an error while `oms-build -q validate` works,
    # which is a trap nobody expects from a CLI.  Sharing them means either
    # position is accepted.
    #
    # SUPPRESS is essential: without it each subparser would apply its own
    # default and overwrite whatever the top-level parser already parsed, so
    # `oms-build -q validate` would silently come back verbose again.  The real
    # defaults are set once, via set_defaults below.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--root",
        default=argparse.SUPPRESS,
        help="reactor root containing the aggregator pom.xml (default: osgi-workshop)",
    )
    common.add_argument(
        "--report-dir",
        default=argparse.SUPPRESS,
        help="where to write the HTML/DOT/SVG/JSON artifacts (default: build-reports)",
    )
    common.add_argument(
        "-q",
        "--quiet",
        dest="verbose",
        action="store_false",
        default=argparse.SUPPRESS,
        help="less verbose logging (omit per-edge and per-file detail)",
    )

    parser = argparse.ArgumentParser(
        prog="oms-build",
        parents=[common],
        description=(
            "Smart build pipeline for the Northwind Order Management System. "
            "Derives the real product dependency graph from OSGi metadata "
            "(MANIFEST.MF, feature.xml) rather than from POM declarations, then builds "
            "either the whole product or only the modules affected by a change."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  oms-build graph\n"
            "  oms-build build --scenario full\n"
            "  oms-build baseline\n"
            "  oms-build build --scenario changed --dry-run\n"
            "  oms-build build --scenario changed --ref origin/main --threads 1C\n"
        ),
    )
    # NB: the real defaults are applied by apply_global_defaults() after
    # parsing, not with parser.set_defaults().  set_defaults() seeds the
    # subparser's namespace too, which resurrects the default and undoes a flag
    # given before the subcommand -- `oms-build -q validate` would come back
    # verbose.  There is a parser-level test covering both positions.

    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_parser(name: str, **kwargs) -> argparse.ArgumentParser:
        """Register a subcommand that also accepts the global options."""
        return subparsers.add_parser(name, parents=[common], **kwargs)

    def add_change_options(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--strategy",
            choices=("auto", "git", "hash"),
            default="auto",
            help="change detection strategy (default: auto -- git when available, else hash)",
        )
        target.add_argument(
            "--ref",
            default="HEAD",
            help="git ref to diff against when using the git strategy (default: HEAD)",
        )
        target.add_argument(
            "--state",
            default=None,
            help="path to the hash-baseline state file (default: <root>/.oms-build-state.json)",
        )
        target.add_argument(
            "--no-global-escalation",
            action="store_true",
            help="do not escalate to a full rebuild when a reactor-wide file (root pom) changes",
        )

    modules_parser = add_parser("modules", help="list every module and its dependencies")
    modules_parser.set_defaults(func=command_modules)

    validate_parser = add_parser(
        "validate", help="check OSGi metadata consistency and detect cycles"
    )
    validate_parser.add_argument(
        "--strict", action="store_true", help="exit non-zero when findings exist"
    )
    validate_parser.set_defaults(func=command_validate)

    baseline_parser = add_parser(
        "baseline", help="record a content-hash baseline for change detection"
    )
    baseline_parser.add_argument("--state", default=None, help="path to the state file")
    baseline_parser.set_defaults(func=command_baseline)

    graph_parser = add_parser(
        "graph", help="render the dependency graph without building"
    )
    graph_parser.add_argument(
        "--with-changes",
        action="store_true",
        help="also compute and render the changed-modules scenario",
    )
    add_change_options(graph_parser)
    graph_parser.set_defaults(func=command_graph)

    ci_parser = add_parser(
        "ci",
        help="plan for CI: emit GitHub Actions step outputs, job summary and reports",
    )
    ci_parser.add_argument(
        "--scenario", choices=("full", "changed"), default="changed",
        help="which scenario to plan (default: changed)",
    )
    ci_parser.add_argument(
        "--output-file",
        default=None,
        help="file to append step outputs to (default: $GITHUB_OUTPUT)",
    )
    ci_parser.add_argument(
        "--summary-file",
        default=None,
        help="file to append the job summary to (default: $GITHUB_STEP_SUMMARY)",
    )
    ci_parser.add_argument(
        "--comment-file", default=None, help="write a PR comment body to this path"
    )
    ci_parser.add_argument(
        "--run-url", default="", help="link back to the workflow run, used in the summary"
    )
    ci_parser.add_argument(
        "--no-fail-on-cycle",
        dest="fail_on_cycle",
        action="store_false",
        default=True,
        help="do not fail the planning job when dependency cycles are detected",
    )
    add_change_options(ci_parser)
    ci_parser.set_defaults(func=command_ci)

    build_parser_ = add_parser("build", help="run the build pipeline")
    build_parser_.add_argument(
        "--scenario",
        choices=("full", "changed"),
        default="full",
        help="full = build all products; changed = build only affected modules",
    )
    build_parser_.add_argument(
        "--dry-run",
        action="store_true",
        help="do everything except invoke Maven (prints the exact command)",
    )
    build_parser_.add_argument(
        "--goals",
        default="clean install",
        help=(
            "Maven goals (default: 'clean install'; install is the default so partial "
            "reactors can resolve previously built bundles from the local repository)"
        ),
    )
    build_parser_.add_argument(
        "-T", "--threads", default=None, help="value for Maven's -T flag, e.g. 1C"
    )
    build_parser_.add_argument("--offline", action="store_true", help="pass --offline to Maven")
    build_parser_.add_argument("--maven", default=None, help="path to the Maven executable")
    build_parser_.add_argument(
        "--maven-arg",
        action="append",
        help="extra argument passed through to Maven (repeatable)",
    )
    build_parser_.add_argument(
        "--no-fail-on-cycle",
        dest="fail_on_cycle",
        action="store_false",
        default=True,
        help="build anyway when dependency cycles are detected",
    )
    build_parser_.add_argument(
        "--include-full-graph",
        action="store_true",
        help="in the changed scenario, also render the full-build graph for comparison",
    )
    add_change_options(build_parser_)
    build_parser_.set_defaults(func=command_build)

    return parser


#: Defaults for the options shared by the top-level parser and every
#: subcommand.  Applied after parsing rather than through set_defaults() -- see
#: the note in build_parser().
GLOBAL_DEFAULTS = {
    "root": "osgi-workshop",
    "report_dir": "build-reports",
    "verbose": True,
}


def apply_global_defaults(args: argparse.Namespace) -> argparse.Namespace:
    """Fill in any global option the user did not give in either position."""
    for name, default in GLOBAL_DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, default)
    return args


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return apply_global_defaults(build_parser().parse_args(argv))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return int(args.func(args))
    except FileNotFoundError as error:
        print(f"[ERROR] {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
