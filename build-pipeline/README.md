# OMS smart build pipeline

A dependency-aware build pipeline for the Northwind Order Management System, an
Eclipse/Tycho OSGi product of 11 plugin bundles and 9 features across 9
functional components.

It answers the two questions the assignment asks:

1. **Build everything** in an order that respects every dependency.
2. **Build only what a change affects** — the changed modules plus every module
   that transitively consumes them, in dependency order, with the reasoning
   shown rather than asserted.

---

## Why this needed writing

The obvious approach is to let Maven work out the order. That does not work
here, because **the dependency information is not in the POMs**. Every plugin
POM in this product is a three-line stanza:

```xml
<artifactId>com.northwind.oms.gateway</artifactId>
<packaging>eclipse-plugin</packaging>
```

There is no `<dependencies>` block anywhere. The real graph lives in OSGi
metadata that Maven itself never reads — Tycho resolves it at build time
against a p2 target platform:

| Where | Header | Meaning |
|---|---|---|
| `META-INF/MANIFEST.MF` | `Import-Package` | needs a package; the exporter must be built first |
| `META-INF/MANIFEST.MF` | `Require-Bundle` | needs a whole bundle |
| `META-INF/MANIFEST.MF` | `Export-Package` | supplies a package to others |
| `feature.xml` | `<plugin id=…>` | a feature packages this bundle |
| `feature.xml` | `<import feature=…>` | a feature needs another feature |

This is also why off-the-shelf tooling can't do the job. `dorny/paths-filter`
and friends know which *files* changed but nothing about what *consumes* them,
so they cannot answer "what else must I rebuild". The `affected`-graph tools
from the JS world (Nx, Turborepo) assume the graph is declared in build config.
Neither assumption holds, so the graph has to be derived from the OSGi headers
directly. That derivation is what `omsbuild/` is.

Two Maven flags deserve a note, because reaching for them is the natural
instinct and both are wrong here:

- **`-am` / `-amd` are deliberately not used.** Maven derives "also make
  dependencies/dependents" from POM dependencies, which don't exist in this
  product, so both flags would silently under-build. The resolver computes the
  closure itself and passes it as an explicit `-pl` list.
- **The default goal is `install`, not `verify`.** A partial reactor has to
  resolve the modules it *isn't* building from somewhere; `install` puts each
  wave's output in the local repository where the next wave can find it.

## The derived product graph

```
                    tpcl.org.slf4j
                          │
   core ──┬── inventory ──┤
          ├── pricing ────┼── gateway ── reporting
          ├── payment ────────────────────┘
          └── shipping ── notification
   customer ──┴──────────────┘
   security ── payment
```

20 modules, 42 edges, no cycles, resolving to 5 build waves. Everything in a
wave is independent of everything else in that wave, so a wave is a legal
parallel batch.

---

## Using it locally

```bash
# What is in the product, and what depends on what
./build-pipeline/oms-build --root osgi-workshop modules
./build-pipeline/oms-build --root osgi-workshop graph

# Render both scenarios side by side without building anything
./build-pipeline/oms-build --root osgi-workshop graph --with-changes --ref origin/main

# Scenario 1 — build everything, in order
./build-pipeline/oms-build --root osgi-workshop build --scenario full

# Scenario 2 — build only what changed since a git ref
./build-pipeline/oms-build --root osgi-workshop build \
    --scenario changed --strategy git --ref origin/main

# Same, but plan and report without invoking Maven
./build-pipeline/oms-build --root osgi-workshop build \
    --scenario changed --ref origin/main --dry-run

# Cross-check the product's own OSGi metadata for inconsistencies
./build-pipeline/oms-build --root osgi-workshop validate
```

The global options `--root`, `--report-dir` and `-q` are accepted **either
before or after** the subcommand, so `oms-build -q validate` and
`oms-build validate -q` both work. Plain argparse only allows the first form;
`CommandLineTests` pins both, because the failure mode when this regresses is
silent (the tool just ignores the flag rather than erroring).

### Working without git

`--strategy hash` fingerprints every source file with SHA-256 into
`.oms-build-state.json`, so change detection works in an exported tree with no
repository at all:

```bash
./build-pipeline/oms-build --root osgi-workshop baseline     # record
# ... edit files ...
./build-pipeline/oms-build --root osgi-workshop build --scenario changed --strategy hash
```

`--strategy auto` (the default) picks git when the reactor is inside a work
tree and falls back to hashing when it isn't.

### What a run produces

Verbose stdout logging covering the product inventory, every resolved
dependency edge with the header that justified it, the detected changes with
their owning module, the dependency chain from each changed module to each
impacted one, the wave-by-wave build order with the trigger for every entry,
and the skip list with the reason. Plus, in `build-reports/`:

| File | Contents |
|---|---|
| `index.html` | Standalone report: graph, build order, impact tables, findings |
| `build-*.svg` / `.png` | Rendered dependency graph, coloured by role |
| `build-*.mmd` | Mermaid source (renders natively in GitHub) |
| `build-*.json` | Machine-readable plan — waves, edges, impact paths |
| `report.md` | Markdown version of the report |

---

## In GitHub Actions

`.github/workflows/build-pipeline.yml` is the headline deliverable. It keeps
the YAML declarative — the workflow never parses OSGi metadata — by splitting
the problem in two: one job decides, the rest obey.

```
resolver-tests ─► plan ─► wave-1 ─► wave-2 ─► … ─► wave-6 ─► pipeline-result
                    │                                            ▲
                    └──────────► single-runner-build ─────────────┘
```

| Job | Role |
|---|---|
| `resolver-tests` | Runs the unit suite. `plan` gates on it — see below. |
| `plan` | Runs the resolver, emits one job matrix per wave, publishes the graph and upserts the PR comment. No JDK, no Maven. |
| `wave-1` … `wave-6` | `matrix: fromJSON(needs.plan.outputs.waveN_matrix)`; one runner per module. Skips itself when its matrix is empty. |
| `wave-overflow` | Safety valve: builds any wave past the ladder on one runner. |
| `single-runner-build` | Alternative mode; all waves on one runner with `mvn -T`. |
| `pipeline-result` | One status check reflecting the whole pipeline. |

**Triggers.** Pull requests run scenario 2 against the merge base. Pushes to
`main` run scenario 1. `workflow_dispatch` exposes `scenario`, `build_mode`,
`plan_only` and `base_ref` so either scenario can be demonstrated on demand.

### Three design decisions worth explaining

**Why `resolver-tests` gates `plan`.** If the manifest parser or the
topological sort is wrong, the pipeline does not fail — it quietly builds the
*wrong subset* and reports success, which is far worse than a red build. The
test suite is the only thing standing between a parser bug and a corrupt
artifact, so nothing is allowed to build until it passes. It is stdlib
`unittest` only, needs no `pip install`, and costs a couple of seconds.

**Why a fixed ladder of wave jobs.** Actions can fan a job *out* dynamically
(`matrix: ${{ fromJSON(…) }}`) but cannot create a dynamic *chain* of jobs —
`needs:` is static. So the workflow declares six wave jobs and each one skips
itself when the plan gave it nothing:

```yaml
if: >
  !cancelled() && !contains(needs.*.result, 'failure') &&
  needs.plan.outputs.wave3_has_work == 'true'
```

`!cancelled() && !contains(…, 'failure')` rather than `success()` is the point:
a wave must still run when an *earlier* wave was skipped as empty, which
`success()` would treat as a reason to stop. `MAX_WAVES` in `ghaction.py` and
the ladder length in the YAML are two halves of one contract, so
`WorkflowContractTests` asserts a job exists for every ladder wave — the tests
fail rather than the pipeline silently dropping a wave.

**Why the two build modes.** In `matrix` mode each module gets a clean runner,
which gives per-module logs and true parallelism, but every module must then
hand its output to the next wave: the composite action tars
`~/.m2/repository/com/northwind` and uploads it, and downstream jobs unpack
every `oms-m2-w*` bundle they can find. The p2 target platform — by far the
slowest part of a Tycho build and identical for every module — travels
separately via `actions/cache` keyed on the root POM hash.

For 20 tiny bundles that hand-off overhead probably costs more than the
parallelism saves, and `single-runner-build` (one runner, waves sequential,
`mvn -T 1C` within each) is likely faster. Both are implemented and selectable
via the `build_mode` dispatch input, so the trade-off can be **measured on the
real runners rather than argued about**. Matrix mode is the default because
this assignment is about making the dependency reasoning visible, and one job
per module is the clearest possible display of it.

---

## Tests

```bash
PYTHONPATH=build-pipeline python3 -m unittest discover -s build-pipeline/tests -v
```

63 tests, no third-party dependencies. The ones that matter most:

- **Manifest parsing** — 72-byte line folding, CRLF, commas inside quoted
  version ranges (`version="[1.0.0,2.0.0)"` is one clause, not two),
  `;singleton:=true` stripping, shared attribute sets.
- **Ordering** — every edge is checked against the produced order, waves are
  proved to partition the graph exactly once with no intra-wave edge, ordering
  is deterministic, and a synthetic cycle is detected and blocks ordering.
- **Impact** — the assignment's own example (change `core`, `gateway` and
  `payment`) is reproduced and asserted to select 14 of 20 modules and leave
  `customer`, `security` and `slf4j` alone, because those are *upstream* of the
  change and rebuilding them would be waste.
- **Git strategy** — against a real temp repository, including that the merge
  base is used so commits landed on `main` after branching don't inflate the
  rebuild set.
- **Workflow contract** — the resolver's wave outputs and the YAML job ladder
  cannot drift apart.

### End-to-end verification performed

Scenario 1 plans all 20 modules in 5 waves. Scenario 2 was verified against a
throwaway git repository with a real branch and a commit touching three modules
in three different functional domains, exactly as the assignment's test method
prescribes:

```
catalog/plugins/com.northwind.oms.core/…/Product.java
orders/plugins/com.northwind.oms.gateway/…/OrderService.java
payment/plugins/com.northwind.oms.payment/…/PaymentResult.java
```

Result: 3 directly changed → **14 of 20 modules rebuilt across 5 waves, 6
skipped (30% of the reactor avoided)**. The impact set was then re-derived by a
second, independent script that re-parses the raw manifests with a throwaway
regex parser and runs its own reachability search, so the resolver could not
validate itself. Both agree on all 14 modules, and every wave boundary in the
plan was confirmed to respect a real dependency edge.

---

## Findings in the product itself

`validate` reports four genuine packaging inconsistencies. Four features
package bundles that declare `Require-Bundle: com.northwind.oms.tpcl.org.slf4j`
but never `<import feature="com.northwind.oms.tpcl.slf4j.feature"/>`:

- `com.northwind.oms.payment.feature`
- `com.northwind.oms.notification.feature`
- `com.northwind.oms.orders.feature`
- `com.northwind.oms.reporting.feature`

The reactor build passes regardless, because the bundle is present in the
workspace when Tycho resolves. It is an installation-time problem: a p2
install of any of those features alone can pick a different slf4j, or fail to
resolve. Worth fixing in the feature definitions; the pipeline reports it
rather than failing on it, since it is pre-existing.

## Layout

```
build-pipeline/
  oms-build              # launcher (sets PYTHONPATH, runs from the repo root)
  omsbuild/
    model.py             # dataclasses and enums; no logic
    scan.py              # MANIFEST.MF / feature.xml / POM parsing, reactor walk
    graph.py             # edges, Kahn waves, Tarjan cycles, BFS impact paths
    changes.py           # git and hash change detection
    plan.py              # scenario 1 and 2 planners
    runner.py            # Maven invocation
    render.py            # DOT/SVG, Mermaid, HTML, ASCII
    ghaction.py          # job matrices, step outputs, job summary, PR comment
    cli.py               # command line
  tests/test_pipeline.py # 63 tests, stdlib only
.github/
  workflows/build-pipeline.yml
  actions/build-module/action.yml
```

The scan walks the `<modules>` tree from the root POM rather than globbing the
filesystem, so the resolver sees exactly the module set Maven sees — a
directory that exists on disk but isn't wired into the reactor is correctly
ignored.
