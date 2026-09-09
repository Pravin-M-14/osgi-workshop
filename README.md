# Northwind OMS — smart build pipeline

A dependency-aware build pipeline for the Northwind Order Management System, an
Eclipse/Tycho OSGi product of 11 plugin bundles and 9 features across 9
functional components.

It supports the two required scenarios:

- **Scenario 1 — build all products**, in an order that respects every dependency.
- **Scenario 2 — build only changed products**, meaning the changed modules plus
  every module that transitively consumes them, in dependency order.

The assignment brief is unchanged at [`osgi-workshop/README.md`](osgi-workshop/README.md).
Design rationale and trade-offs are in [`build-pipeline/README.md`](build-pipeline/README.md).

---

## Where each deliverable is demonstrated

| Requirement | Where to see it |
|---|---|
| **Part 1** — both scenarios supported | Actions run, or `oms-build build --scenario full\|changed` |
| Detected changes | Log section `--- Change detection`, and the job summary table |
| Module dependency chain | Log section `--- Dependency chain resolution` |
| Build order and sequence | Log section `--- Build order`, and one Actions job per module |
| **Part 2** — dependency graph | `build-reports/index.html`, the job summary, the PR comment |
| Graph for **both** scenarios | `build-full.svg` and `build-changed.svg` |
| All modules and their dependencies | All 20 modules and 42 edges are drawn, including skipped ones |
| Direction of each dependency | Arrowheads; caption states arrows point at the dependency |
| Dependency path for every changed module | `--- Dependency chain resolution` tree, plus per-module path tables in the HTML report |
| Modules selected for rebuilding | Colour-coded in the graph; `--- Rebuild summary` lists selected vs skipped with reasons |

---

## What the GitHub Actions workflow does

The dependency information for this product **is not in the POMs** — every
plugin POM is a three-line `eclipse-plugin` stanza with no `<dependencies>`
block. The real graph lives in `META-INF/MANIFEST.MF` (`Import-Package`,
`Export-Package`, `Require-Bundle`) and `feature.xml` (`<plugin id>`,
`<import feature>`).

So the workflow keeps its YAML declarative by splitting the problem: **one job
decides, the rest obey.**

```
resolver-tests --> plan --> wave-1 --> wave-2 --> ... --> wave-6 --> pipeline-result
                    |                                                    ^
                    +-----------> single-runner-build --------------------+
```

| Job | What it does |
|---|---|
| `resolver-tests` | Runs the 63 unit tests. Gates `plan`, because a parser bug wouldn't fail the build — it would quietly build the *wrong subset* and report success. |
| `plan` | Parses the OSGi metadata, computes the graph, the impact set and the build waves. Emits one job matrix per wave, publishes the graph, comments on the PR. No JDK, no Maven. |
| `wave-1` … `wave-6` | Build only what the plan handed them, one runner per module, all modules in a wave in parallel. A wave with nothing to do skips itself. |
| `wave-overflow` | Safety valve if a future change produces more waves than the ladder. |
| `single-runner-build` | Alternative mode: all waves on one runner with `mvn -T`. Faster for a reactor this small. |
| `pipeline-result` | Single status check summarising the whole run. |

Waves exist because everything within a wave is provably independent, so it is
a safe parallel batch. This product resolves to 5 waves.

---

## How to trigger each scenario

### Scenario 1 — build all products

Either push to `main`:

```bash
git push origin main
```

…or trigger it by hand, which is the better demo because you choose the scenario
explicitly: **Actions → OMS Build Pipeline → Run workflow**, then set
`scenario = full`.

### Scenario 2 — build only changed products

Open a pull request. Change detection runs against the **merge base** of your
branch and the target branch, so commits that landed on `main` after you
branched don't inflate the rebuild set.

```bash
git checkout -b demo/changed-modules
printf '\n// demo\n' >> osgi-workshop/catalog/plugins/com.northwind.oms.core/src/com/northwind/oms/core/model/Product.java
printf '\n// demo\n' >> osgi-workshop/orders/plugins/com.northwind.oms.gateway/src/com/northwind/oms/gateway/OrderService.java
printf '\n// demo\n' >> osgi-workshop/payment/plugins/com.northwind.oms.payment/src/com/northwind/oms/payment/PaymentResult.java
git commit -am "demo: change three modules across three functional domains"
git push -u origin demo/changed-modules
```

Then open the PR. That is exactly the test case the brief asks for: three
modules in three different functional domains.

Or run it by hand with `scenario = changed` and optionally a `base_ref`.

### Manual dispatch inputs

| Input | Values | Purpose |
|---|---|---|
| `scenario` | `auto` / `full` / `changed` | `auto` = changed on PRs, full on `main` |
| `build_mode` | `matrix` / `single-runner` | one job per module, or all waves on one runner |
| `plan_only` | boolean | compute and publish the plan and graph **without compiling** |
| `base_ref` | git ref | what to diff against in the changed scenario |

> **Tip for a first run:** trigger it once with `plan_only` ticked. You get the
> full graph, impact analysis and build order with no Maven involved, which
> proves out Part 1 and Part 2 in about a minute.

---

## Where to check the graph

Four places, in rough order of convenience:

1. **The Actions job summary** — open the run, and the `plan` job summary renders
   the graph inline as Mermaid, with the stats table, detected changes, build
   order and skipped modules. Nothing to download.
2. **The PR comment** — on pull requests the pipeline posts (and updates in
   place) a comment with the changed modules, their downstream counts, the wave
   order and the graph.
3. **The `oms-dependency-graph` artifact** — attached to every run. Download and
   open `index.html` for the full report, or use the `.svg` / `.png` directly in
   a write-up.
4. **`build-reports/` locally** — written on every local run:

| File | Contents |
|---|---|
| `index.html` | Standalone report: graph, build order, per-module dependency paths, findings |
| `build-full.svg` / `.png` | Full-build graph |
| `build-changed.svg` / `.png` | Changed-modules graph, colour-coded |
| `build-*.mmd` | Mermaid source (renders natively in GitHub) |
| `build-*.json` | Machine-readable plan: waves, edges, impact paths |

**Reading the changed-modules graph:** red/pink nodes are directly changed,
orange are pulled in because they depend on a change, greyed-out are deliberately
not rebuilt. Dashed boxes group modules by component, every node carries its
wave number, and an arrow points **from a module to the module it depends on** —
so arrowheads point at things built earlier.

---

## What to look for in the logs

Open the `plan` job (or run locally) and you'll see these sections in order.
This is the verbose logging Part 1 asks for.

**`--- Change detection`** — which files changed, what kind of file each is, and
which module owns it. The `baseline` line records exactly what was diffed
against:

```
  strategy : git
  baseline : main (merge-base ca96d26de006)
  files    : 3
    M  catalog/plugins/com.northwind.oms.core/src/.../Product.java
         category=source owner=com.northwind.oms.core
    M  orders/plugins/com.northwind.oms.gateway/src/.../OrderService.java
         category=source owner=com.northwind.oms.gateway
    M  payment/plugins/com.northwind.oms.payment/src/.../PaymentResult.java
         category=source owner=com.northwind.oms.payment
```

**`--- Resolved dependency edges`** — every edge with the OSGi header that
justified it, so nothing is taken on trust:

```
  com.northwind.oms.gateway -> com.northwind.oms.core [import-package: com.northwind.oms.core.model [1.0.0,2.0.0)]
  com.northwind.oms.gateway -> com.northwind.oms.tpcl.org.slf4j [require-bundle: ...]
  com.northwind.oms.catalog.feature -> com.northwind.oms.core [feature-plugin: packages plugin]
```

**`--- Dependency chain resolution`** — for each changed module, the tree of what
it impacts, then the explicit path to every affected module. This is the
"dependency chain" and "dependency path for every changed module" requirement:

```
  core  <- CHANGED
  |-- catalog.feature
  |   |-- orders.feature
  |   |   `-- reporting.feature
  ...
  paths from com.northwind.oms.core:
    com.northwind.oms.core -> com.northwind.oms.catalog.feature -> com.northwind.oms.orders.feature
```

**`--- Build order`** — the sequence, grouped into parallel waves, with why each
module is present and which change triggered it:

```
  wave 1  (these can build in parallel)
      1. com.northwind.oms.core                [directly-changed]
  wave 4  (these can build in parallel)
     11. com.northwind.oms.reporting           [dependent]  <- triggered by core, gateway, payment
```

**`--- Rebuild summary`** — the headline numbers and, importantly, what was
*skipped* and why:

```
  modules in product : 20
  directly changed   : 3
  selected to rebuild: 14
  skipped            : 6
    skip com.northwind.oms.customer  (no dependency path from any change)
    skip com.northwind.oms.security  (no dependency path from any change)
```

The skipped modules are *upstream* of the change, so rebuilding them would be
pure waste — that's the saving the whole exercise is about.

**`--- Consistency checks`** — four pre-existing packaging inconsistencies in the
product; see the bottom of this file.

**`--- Maven / Tycho invocation`** — the exact command, including the explicit
`-pl` module list.

In the Actions UI each wave additionally appears as its own job named
`W3 gateway`, so the build order is legible from the run graph without reading
any logs at all.

---

## Running it locally

```bash
# What depends on what
./build-pipeline/oms-build --root osgi-workshop graph

# Scenario 1
./build-pipeline/oms-build --root osgi-workshop build --scenario full

# Scenario 2
./build-pipeline/oms-build --root osgi-workshop build \
    --scenario changed --strategy git --ref origin/main

# Either scenario, planning and reporting only (no Maven needed)
./build-pipeline/oms-build --root osgi-workshop build \
    --scenario changed --ref origin/main --dry-run

# Cross-check the product's own OSGi metadata
./build-pipeline/oms-build --root osgi-workshop validate
```

Python 3.9+ and no third-party packages. `graphviz` is optional and only needed
for `.svg` / `.png` rendering; without it you still get DOT, Mermaid, HTML and
JSON. `--dry-run` means you can demonstrate everything without a JDK or Maven
installed.

There is also `--strategy hash`, which fingerprints sources into
`.oms-build-state.json` so change detection works in an exported tree with no
git repository at all. Record a baseline with `oms-build baseline` first.

---

## Result on the brief's own test case

Changing three modules across three functional domains — `core` (catalog),
`gateway` (orders), `payment` (payment) — resolves to **14 of 20 modules rebuilt
across 5 waves, 6 skipped**, avoiding 30% of the reactor.

The derived graph:

```
                    tpcl.org.slf4j
                          |
   core --+-- inventory --+
          +-- pricing ----+-- gateway -- reporting
          +-- payment ---------------------+
          +-- shipping -- notification
   customer --+----------------+
   security -- payment
```

## Tests

```bash
PYTHONPATH=build-pipeline python3 -m unittest discover -s build-pipeline/tests -v
```

63 tests, stdlib only. They cover OSGi manifest parsing edge cases (72-byte line
folding, commas inside quoted version ranges), proof that the produced order
satisfies every edge, cycle detection, the impact closure, git merge-base change
detection, and a contract test keeping the resolver's wave outputs in step with
the workflow's job ladder.

The impact set was additionally cross-checked by re-deriving it with a separate
throwaway parser and an independent reachability search, so the resolver could
not validate itself. Both agree on all 14 modules.

## Known findings in the product

`oms-build validate` reports four genuine packaging inconsistencies. The
`payment`, `notification`, `orders` and `reporting` features each package a
bundle that declares `Require-Bundle: com.northwind.oms.tpcl.org.slf4j` but never
imports `com.northwind.oms.tpcl.slf4j.feature`. The reactor build passes anyway,
because the bundle is present in the workspace when Tycho resolves. It is an
installation-time problem: a p2 install of any of those features alone can pick a
different slf4j or fail to resolve. The pipeline reports these rather than
failing on them, since they are pre-existing.
