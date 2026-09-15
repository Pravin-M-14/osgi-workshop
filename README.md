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
resolver-tests --> plan --> wave-1 --> wave-2 --> ... --> wave-5 --> pipeline-result
                    |                                                    ^
                    +-----------> single-runner-build --------------------+
```

| Job | What it does |
|---|---|
| `resolver-tests` | Runs the 71 unit tests. Gates `plan`, because a parser bug wouldn't fail the build — it would quietly build the *wrong subset* and report success. |
| `plan` | Parses the OSGi metadata, computes the graph, the impact set and the build waves. Emits one job matrix per wave, publishes the graph, comments on the PR. No JDK, no Maven. |
| `wave-1` … `wave-5` | Build only what the plan handed them, one runner per module, all modules in a wave in parallel. A wave with nothing to do skips itself. |
| `deep-graph-fallback` | Not a wave. Idle on every normal run. If a future change makes the graph deeper than the five-rung ladder, it builds those extra waves in order on one runner, so they can never be silently dropped. |
| `single-runner-build` | Alternative mode: all waves on one runner with `mvn -T`. Faster for a reactor this small. |
| `pipeline-result` | Single status check summarising the whole run. |

Waves exist because everything within a wave is provably independent, so it is
a safe parallel batch. This product resolves to 5 waves.

---

## The four dispatch parameters, exactly

**Actions → OMS Build Pipeline → Run workflow** exposes four inputs. They are
independent of each other, and each one is resolved by a specific rule rather
than guessed at, so this section states each rule precisely.

| Input | Type | Default | Decides |
|---|---|---|---|
| `scenario` | `auto` / `full` / `changed` | `auto` | **what** gets built |
| `build_mode` | `matrix` / `single-runner` | `matrix` | **how** it gets built |
| `plan_only` | boolean | `false` | **whether** anything is compiled at all |
| `base_ref` | string (git ref) | `""` (blank) | what `changed` compares **against** |

### `scenario` — what gets built

| Value | Meaning |
|---|---|
| `full` | **Scenario 1.** Every module in the reactor, in dependency order. Change detection is not consulted at all. Always 20 modules. |
| `changed` | **Scenario 2.** Only the modules that changed since `base_ref`, plus every module that transitively depends on them. Between 0 and 20 modules. |
| `auto` | Derive it from the event that started the run — see the table below. |

`auto` is not a third behaviour; it resolves to `full` or `changed` before
anything else happens. The rule is a three-line `case` on the event name:

| Event that triggered the run | `auto` resolves to | Why |
|---|---|---|
| `pull_request` | `changed` | A PR *is* a set of changes with a well-defined base. |
| `push` to `main` | `full` | `main` must be known-good in its entirety, so nothing is skipped. |
| `workflow_dispatch` (the **Run workflow** button) | `changed` | Falls into the `*)` catch-all. |

> **The one trap worth knowing.** Pressing **Run workflow** on `main` and
> leaving `scenario = auto` resolves to `changed`, not `full` — and with no PR
> to supply a base, `base_ref` falls back to `HEAD~1`. If the last commit on
> `main` touched only the README, the workflow, or `build-pipeline/`, then
> nothing under `osgi-workshop/` changed, so **0 of 20 modules are selected and
> no build job runs at all.** The run goes green having compiled nothing. That
> is correct behaviour, but it looks like a broken pipeline.
>
> So when dispatching by hand, **always set `scenario` explicitly.** `auto` is
> there for the automatic triggers, where the event makes the intent obvious.

Whatever it resolves to is echoed in the `plan` job:

```
Resolved scenario=full, base ref=HEAD~1
```

### `base_ref` — what `changed` compares against

Ignored entirely when the scenario is `full`. When it is `changed` and you
leave it blank, the workflow walks a four-step fallback, taking the first that
applies:

| # | Condition | Base used |
|---|---|---|
| 1 | You typed a value | exactly that, verbatim |
| 2 | Running on a pull request | `origin/<target branch>`, e.g. `origin/main` |
| 3 | Running on a push with a known previous SHA | `github.event.before` — the commit `main` was on before the push |
| 4 | Anything else (including manual dispatch) | `HEAD~1` |

On a pull request the resolver then diffs against the **merge base** of your
branch and the target, not the target's current tip. That matters: without it,
every commit someone else lands on `main` after you branch would show up as
your change and inflate the rebuild set.

Accepts anything `git rev-parse` accepts — `origin/main`, `HEAD~3`, a tag, or a
full SHA. Named remote branches are fetched first; SHAs and `HEAD~n` need no
fetching. An unresolvable ref produces a warning rather than a hard failure,
and the resolver falls back.

### `build_mode` — how it gets built

Both modes build exactly the same module set in exactly the same dependency
order. Only the runner topology differs, and the choice is a genuine trade-off
rather than a right answer.

| | `matrix` (default) | `single-runner` |
|---|---|---|
| Jobs | one per module, `W1 core`, `W2 payment`, … | one job total |
| Parallelism | every module in a wave at once, across runners | `mvn -T 1C` within each wave |
| Waves | separate jobs chained by `needs:` | sequential loop in one script |
| Artifact hand-off | tar `~/.m2/repository/com/northwind` **and `.meta`**, upload, download, merge | none — one local repository throughout |
| Logs | per-module, isolated | one log, grouped per wave |
| Measured here | ~19–29s *per module*, plus JDK + cache + tar per runner | **59s for all 20 modules** |

Matrix mode is the default because this assignment is about making the
dependency reasoning *visible*, and one job per module is the clearest possible
display of it — you can read the build order off the Actions graph without
opening a log. Single-runner is faster for a reactor this small, because for
twenty tiny bundles the per-runner setup and the artifact hand-off cost more
than the parallelism saves. Both are implemented so the trade-off can be
measured on the real runners rather than argued about.

### `plan_only` — whether anything is compiled

Cuts the run off after the `plan` job. You still get the full dependency graph,
the impact analysis, the build order, the job summary, the PR comment and the
`oms-dependency-graph` artifact — with no JDK, no Maven and no Tycho target
platform resolution. Roughly one minute instead of several.

It is orthogonal to the other three: `plan_only` with `scenario = changed`
shows you exactly what *would* be rebuilt.

> **Best first run:** `scenario = full`, `plan_only = ticked`. That exercises
> both Part 1 and Part 2 of the brief in about a minute, and it cannot fail for
> any reason to do with Maven.

### How the inputs combine

`scenario` and `build_mode` are fully independent, giving four compile
combinations, all of which do the same amount of work in a different shape:

| `scenario` | `build_mode` | Result |
|---|---|---|
| `full` | `matrix` | 20 modules, 20 jobs across 5 waves |
| `full` | `single-runner` | 20 modules, 1 job, 5 sequential waves |
| `changed` | `matrix` | *N* modules, *N* jobs, only non-empty waves run |
| `changed` | `single-runner` | *N* modules, 1 job, empty waves logged and skipped |

Two things can stop a build regardless of those choices: `plan_only = true`,
and `has_work = false` (the `changed` scenario found nothing affected). Both
are reported as a `::notice::` in the `plan` job's **Decide whether to
compile** step, so a run that deliberately compiled nothing always says so:

```
plan_only requested -- publishing the plan and graph without compiling
no module is affected by this change -- nothing to compile
```

---

## Test scenarios

Five runnable scenarios covering every parameter. Each states what to do, what
to expect, and what to check — the expectations are exact numbers, so a
mismatch is a real finding rather than something to squint at.

### T1 — Scenario 1, plan only (start here)

Nothing to set up. **Run workflow** with:

| Input | Value |
|---|---|
| `scenario` | `full` |
| `build_mode` | *(irrelevant — nothing compiles)* |
| `plan_only` | ✅ ticked |
| `base_ref` | *(leave blank)* |

**Expect:** `resolver-tests` and `plan` green; every wave job grey; total ≈ 1
minute.

**Check in the `plan` log:**

```
Resolved scenario=full, base ref=HEAD~1     <- base is ignored for full
modules in product : 20
selected to rebuild: 20
skipped            : 0
waves              : 5
plan_only requested -- publishing the plan and graph without compiling
```

Then open the **job summary** for the Mermaid graph, the stats table and the
build order, and download the `oms-dependency-graph` artifact for
`index.html`.

### T2 — Scenario 1, full compile, both modes

Run twice, changing only `build_mode`:

| Input | Value |
|---|---|
| `scenario` | `full` |
| `build_mode` | `matrix`, then `single-runner` |
| `plan_only` | ☐ unticked |
| `base_ref` | *(blank)* |

**Expect, `matrix`:** 20 build jobs in five ranks — 4 in W1, 7 in W2, 3 in W3,
4 in W4, 2 in W5 — each named for its module. `deep-graph-fallback` and
`single-runner-build` grey.

**Expect, `single-runner`:** one build job, `deep-graph-fallback` and all wave
jobs grey.

**Check in `single-runner-build`** — this is the mode where module accounting
is not visible in the UI, so the log asserts it instead. It opens by declaring
the contract:

```
--- Modules this job must build (20) ---
  1  catalog/plugins/com.northwind.oms.core
  2  customer/plugins/com.northwind.oms.customer
  ...
 20  reporting/features/com.northwind.oms.reporting.feature
```

then, per collapsible wave group, `queued:` lines before Maven runs and
`built:` lines after, each naming the JAR produced. It closes with a separate
**Verify every planned module produced an artifact** step:

```
--- Build verification ---
   1  ok       catalog/plugins/com.northwind.oms.core -> com.northwind.oms.core-1.0.0.jar
  ...
  planned : 20
  built   : 20
  missing : 0
```

That step **fails the run** if any planned module produced no JAR, so "did all
20 actually build?" is answered by the run's own status rather than by counting
log lines. The same table is written to the job summary.

### T3 — Scenario 2 via pull request (the brief's own test case)

This is the only path that exercises merge-base detection, the PR comment and
`scenario = auto` → `changed` together, so it is the one that matters most.

Run each command on its own line:

```bash
git checkout -b demo/changed-modules
printf '\n// demo\n' >> osgi-workshop/catalog/plugins/com.northwind.oms.core/src/com/northwind/oms/core/model/Product.java
printf '\n// demo\n' >> osgi-workshop/orders/plugins/com.northwind.oms.gateway/src/com/northwind/oms/gateway/OrderService.java
printf '\n// demo\n' >> osgi-workshop/payment/plugins/com.northwind.oms.payment/src/com/northwind/oms/payment/PaymentResult.java
git commit -am "demo: change three modules across three functional domains"
git push -u origin demo/changed-modules
```

Then open a PR against `main`. No dispatch inputs are involved — the
`pull_request` trigger fires and `auto` resolves to `changed` on its own.

Three modules in three different functional domains, exactly as the brief
prescribes.

**Expect: 14 of 20 modules rebuilt across 5 waves, 6 skipped.**

**Check the `plan` log.** It prints eleven `---` sections, always in this
order:

| # | Log section | What proves out |
|--:|---|---|
| 1 | `--- Product inventory` | `components: 9`, `plugin bundles: 11`, `features: 9`, `dependency edges: 42` |
| 2 | `--- Resolved dependency edges (source depends on target)` | all 42 edges, each tagged with the OSGi header that justified it — `import-package`, `require-bundle`, `feature-plugin`, `feature-requires` |
| 3 | `--- Consistency checks (4 finding(s))` | the four pre-existing packaging findings (see the bottom of this file) |
| 4 | `--- Change detection` | `strategy: git`, `baseline: main (merge-base …)`, `files: 3`, each file with `category=source` and its `owner=` module |
| 5 | `--- Directly changed modules (3)` | the three modules, with kind and component, and which file changed in each |
| 6 | `--- Dependency chain resolution (impact analysis)` | one tree per changed module, then the explicit path to every module it impacts |
| 7 | `--- Build order (14 module(s), 5 wave(s))` | the waves, each entry tagged `[directly-changed]` or `[dependent] <- triggered by …`, with its reactor-relative path |
| 8 | `--- Rebuild summary` | `directly changed: 3`, `selected to rebuild: 14`, `skipped: 6`, each skip with its reason, plus the component order |
| 9 | `--- Maven / Tycho invocation` | the exact command, with the explicit 14-module `-pl` list |
| 10 | `--- Generated artifacts` | the report files written to `build-reports/` |
| 11 | `--- Result` | the outcome line |

Sections 4, 5, 6 and 8 are absent in the `full` scenario, because nothing is
being diffed and nothing is skipped — a `full` run prints seven sections and
its build order header reads `--- Build order (20 module(s), 5 wave(s))`.

The six skipped modules must be `customer`, `customer.feature`, `security`,
`security.feature`, `tpcl.org.slf4j` and `tpcl.slf4j.feature` — all *upstream*
of the change, each reported as `no dependency path from any change`.
Rebuilding them would be pure waste, and that 30% is the saving the whole
exercise is about.

**Also check the PR itself:** the pipeline posts a comment with the changed
modules, their downstream counts, the wave order and the graph. Push a second
commit and it is **updated in place** rather than duplicated.

### T4 — Scenario 2 by hand, with an explicit base

Demonstrates `base_ref` without needing a PR. Push the three edits from T3 to a
branch, then **Run workflow** *from that branch* with:

| Input | Value |
|---|---|
| `scenario` | `changed` |
| `build_mode` | either |
| `plan_only` | ✅ ticked (or unticked to compile) |
| `base_ref` | `origin/main` |

**Expect** the same 14 of 20. Confirm the log echoes your value rather than a
fallback:

```
Resolved scenario=changed, base ref=origin/main
```

Vary `base_ref` to see the impact set move: `HEAD~1` (the last commit only)
versus `origin/main` (the whole branch).

### T5 — Scenario 2 with nothing to do

Worth running once, because a pipeline that reports "nothing to build" must be
distinguishable from one that is broken. On `main`, **Run workflow** with
`scenario = changed`, `base_ref` blank.

**Expect:** `resolver-tests` and `plan` green, every build job grey, and
`pipeline-result` green. The reason is explicit:

```
files    : 0
selected to rebuild: 0
skipped            : 20
no module is affected by this change -- nothing to compile
```

If the last commit on `main` touched only docs, the workflow or
`build-pipeline/`, this is the correct answer: none of those paths is inside
`osgi-workshop/`, so no product module changed. This is the trap described
under `scenario` above, deliberately triggered.

### Running the same scenarios locally

Every scenario above has a local equivalent that needs no runner, and
`--dry-run` means no JDK or Maven either:

```bash
# T1
./build-pipeline/oms-build --root osgi-workshop build --scenario full --dry-run

# T3 / T4
./build-pipeline/oms-build --root osgi-workshop build \
    --scenario changed --strategy git --ref origin/main --dry-run

# Exactly what the plan job runs in CI
./build-pipeline/oms-build --root osgi-workshop ci \
    --scenario changed --strategy git --ref origin/main
```

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

This is the verbose logging Part 1 asks for. [T3 above](#t3--scenario-2-via-pull-request-the-briefs-own-test-case)
indexes all eleven sections in the order they are printed; what follows is an
annotated sample of each of the ones that carry the argument, in that same
order. Open the `plan` job, or run any of the local commands below.

**`--- Product inventory`** — what the resolver found before it reasoned about
any of it:

```
  components        : 9 (catalog, customer, notification, orders, payment, reporting, security, shipping, thirdparty)
  plugin bundles    : 11
  features          : 9
  dependency edges  : 42
  exported packages : 13
```

**`--- Resolved dependency edges (source depends on target)`** — every edge with
the OSGi header that justified it, so nothing is taken on trust:

```
  com.northwind.oms.gateway -> com.northwind.oms.core [import-package: com.northwind.oms.core.model [1.0.0,2.0.0)]
  com.northwind.oms.gateway -> com.northwind.oms.tpcl.org.slf4j [require-bundle: com.northwind.oms.tpcl.org.slf4j [1.0.0,2.0.0)]
  com.northwind.oms.catalog.feature -> com.northwind.oms.core [feature-plugin: packages plugin]
  com.northwind.oms.orders.feature -> com.northwind.oms.catalog.feature [feature-requires: com.northwind.oms.catalog.feature 1.0.0]
```

**`--- Consistency checks`** — four pre-existing packaging inconsistencies in the
product; see the bottom of this file.

**`--- Change detection`** — which files changed, what kind of file each is, and
which module owns it. The `baseline` line records exactly what was diffed
against, including the merge-base SHA it resolved to:

```
  strategy : git
  baseline : main (merge-base fa2291b1f709)
  files    : 3
    M  catalog/plugins/com.northwind.oms.core/src/.../Product.java
         category=source owner=com.northwind.oms.core
    M  orders/plugins/com.northwind.oms.gateway/src/.../OrderService.java
         category=source owner=com.northwind.oms.gateway
    M  payment/plugins/com.northwind.oms.payment/src/.../PaymentResult.java
         category=source owner=com.northwind.oms.payment
```

**`--- Directly changed modules`** — the same information rolled up per module,
with each one's kind and component:

```
  com.northwind.oms.core  [bundle, component=catalog]
      changed: catalog/plugins/com.northwind.oms.core/src/.../Product.java
```

**`--- Dependency chain resolution (impact analysis)`** — for each changed module,
the tree of what it impacts, then the explicit path to every affected module.
This is the "dependency chain" and "dependency path for every changed module"
requirement:

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
module is present, which change triggered it, and its reactor-relative path:

```
  wave 1  (these can build in parallel)
      1. com.northwind.oms.core                     [directly-changed]
         path: catalog/plugins/com.northwind.oms.core
  wave 4  (these can build in parallel)
     11. com.northwind.oms.reporting                 [dependent]  <- triggered by com.northwind.oms.core, com.northwind.oms.gateway, com.northwind.oms.payment
         path: reporting/plugins/com.northwind.oms.reporting
```

**`--- Rebuild summary`** — the headline numbers and, importantly, what was
*skipped* and why. Printed in the `changed` scenario only, since a `full` run
skips nothing:

```
  modules in product : 20
  directly changed   : 3
  selected to rebuild: 14
  skipped            : 6
    skip com.northwind.oms.customer  (no dependency path from any change)
    skip com.northwind.oms.security  (no dependency path from any change)
  component order    : ['catalog'] -> ['orders', 'payment', 'shipping'] -> ['notification', 'reporting']
```

The skipped modules are *upstream* of the change, so rebuilding them would be
pure waste — that's the saving the whole exercise is about.

**`--- Maven / Tycho invocation`** — the exact command, including the explicit
`-pl` module list, so the claim and the build cannot diverge.

In the Actions UI each module additionally appears as its own job named
`W3 gateway`, so in `matrix` mode the build order is legible from the run graph
without reading any logs at all. In `single-runner` mode that per-module view
does not exist, which is why that job prints its own `--- Modules this job must
build` roster and then asserts against it — see T2.

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

71 tests, stdlib only. They cover OSGi manifest parsing edge cases (72-byte line
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

## Scaling: adding new modules is automatic, not configured

Nothing about this pipeline is aware of the product's 20 modules by name.
There is no config file, constant, or list anywhere that enumerates
`core`, `gateway`, `payment`, and so on — every module, edge, and wave is
**derived fresh from the manifests on every run**:

- **Discovery** (`scan.py`) walks the `<modules>` tree recursively from the
  root POM. Add a new plugin or feature and wire it into its parent's
  `<modules>` list with a `MANIFEST.MF` or `feature.xml` in place, and the
  very next `oms-build modules` / `graph` / `build` invocation picks it up —
  no code change.
- **Edges** (`graph.py`) are recomputed the same way: every `Import-Package`
  / `Require-Bundle` / `<import feature=…>` header is re-read and re-indexed
  against every `Export-Package` on every run, so a new module's
  dependencies (and anything that starts depending on it) are captured
  automatically.
- **Waves** fall out of that graph via Kahn's algorithm — they are an
  *output*, not a target. Today wave 1 happens to hold 4 modules
  (`core`, `customer`, `security`, `tpcl.org.slf4j`) purely because those
  are the only four with zero dependencies inside this reactor. A new
  dependency-free module would join wave 1 and make it 5; a new module
  with a fresh dependency chain could add a 6th wave. Nothing about the
  resolver assumes 4, or 20 modules, or 5 waves.

**The one place with a fixed number is the CI ladder, and it's a GitHub
Actions limitation, not a resolver limitation.** Actions can fan a job
*out* dynamically (`matrix: fromJSON(...)`) but cannot create a dynamic
*chain* of jobs, so the workflow declares a fixed ladder of wave jobs sized
to how deep this product's graph resolves today (`MAX_WAVES = 5` in
`ghaction.py`). If new modules push the real graph past 5 waves:

- Waves 1–5 still run on the dynamic matrix ladder, exactly as now.
- Any wave beyond 5 is handled by `deep-graph-fallback`, which builds the
  overflow waves in order on a single runner — correct, just not
  parallelised.
- `WorkflowContractTests` in the test suite asserts the YAML ladder length
  and `MAX_WAVES` stay in sync, so a mismatch fails a test instead of
  silently dropping a wave.

Raising `MAX_WAVES` (and adding the matching `wave-N` block to the
workflow YAML) restores full parallel fan-out at greater depth; it is the
only manual step scaling this product further would ever require, and it
is about CI job topology, not about the dependency resolution itself.
