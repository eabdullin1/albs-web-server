## Package tests config

ALTS tests every binary package that ALBS builds, one test task per package
and architecture. A task goes through these stages:

| Stage | What it does | Configurable |
|-------|--------------|--------------|
| `initialize_terraform`, `start_environment`, `initial_provision` | prepare a clean container of the target platform | no |
| `system_info` | collect information about the environment | no |
| `install_package` | install the package | yes |
| `package_integrity_tests` | check files, dependencies and RPATHs of the package (only when integrity tests are enabled in ALTS) | yes |
| `third_party_tests` | run the third-party tests set up for the build, each test file is reported as `third_party_test_<file>` | yes |
| `tmt_tests` | run the tmt plans of the tests repository (see [tmt plans](#tmt-plans)), each test is reported as `tmt_test_<test name>` | yes |
| `uninstall_package` | remove the package | yes |
| `stop_environment` | remove the container | no |

Some packages fail these tests by design. For example,
`systemd-standalone-tmpfiles` and `systemd-standalone-sysusers` conflict with
`systemd`, which the test container always has, so they can never be
installed there. A package tests config tells ALBS which tests not to run for
a package and which failures to expect.

### Where the config lives

The config of a package is `alts.yaml` in the root of the `main` branch of
`https://git.almalinux.org/tests/<package>`, where `<package>` is the name of
the package repository in `https://git.almalinux.org/rpms`. For example, the
config of systemd is
`https://git.almalinux.org/tests/systemd/src/branch/main/alts.yaml`. The name
comes from the git URL of the build; for a build from an SRPM it's the name
of the source package with `+` spelled as `plus`, e.g. `libsigcplusplus20`.

There is nothing to register in ALBS: it looks the config up by itself.
Changes are picked up within 5 minutes (lookups are cached). Packages
without a tests repository or without `alts.yaml` are tested as before.

The organization, branch, file name and cache lifetime are set with the
`PACKAGE_TESTS_ORG`, `PACKAGE_TESTS_BRANCH`, `PACKAGE_TESTS_CONFIG_PATH` and
`PACKAGE_TESTS_CACHE_TTL` settings; `PACKAGE_TESTS_ENABLED=false` turns the
lookups off. The tmt plans are sent to ALTS only where they are switched on,
see [tmt plans](#tmt-plans).

### Format

```yaml
version: 1

# Tests that aren't run
skip:
  # No "tests": these packages aren't tested at all
  - packages: ['*-debuginfo', '*-debugsource']
    reason: Debug information, nothing to test
  - tests: [uninstall_package]
    packages: [foo-core]
    reason: Removing foo-core breaks the test container

# Tests that run, but their failures don't fail the test task
expected_failures:
  - tests: [install_package]
    packages:
      - systemd-standalone-sysusers
      - systemd-standalone-tmpfiles
    reason: >-
      The standalone sysusers/tmpfiles builds conflict with systemd, which
      the test container always has installed.
```

A `skip` rule with neither `tests` nor `packages` switches the tests off for
the whole source package:

```yaml
skip:
  - reason: Can't be tested in a container
```

Both `skip` and `expected_failures` are lists of rules with these fields:

| Field | Required | Meaning |
|-------|----------|---------|
| `tests` | yes for `expected_failures`; in `skip`, no field means that the package isn't tested at all | `install_package`, `package_integrity_tests`, `third_party_tests`, `tmt_tests`, `uninstall_package`, a single third-party test as `third_party_test_<file>` (a pattern is allowed: `third_party_test_*.bats`), or a single tmt test as `tmt_test_<test name>` (`tmt_test_/Sanity/bz2015182-*`) |
| `reason` | yes | why, keep a link to the build or the issue here |
| `packages` | no | binary packages the rule applies to; all packages built from the source package by default |
| `platforms` | no | ALBS platforms the rule applies to, e.g. `AlmaLinux-9`, `AlmaLinux-Kitten-10`; all platforms by default |
| `arches` | no | architectures the rule applies to, e.g. `x86_64`, `aarch64`; all architectures by default |

`packages`, `platforms` and `arches` take shell-style patterns (`*`, `?`,
`[...]`), matched case-insensitively. Quote a pattern that starts with `*`,
otherwise YAML reads it as an alias: `packages: ['*-debuginfo']`. The list
fields also accept a single value instead of a list.

The config is validated strictly: an unknown key, an unknown test name or a
rule without `reason` makes ALBS ignore the whole config, so the failures stay
failures until the config is fixed. Check it before pushing, with the same
model ALBS uses (needs only pydantic and PyYAML):

```sh
python3 scripts/check_package_tests_config.py path/to/alts.yaml
```

### tmt plans

The tests repository may also hold tmt tests, e.g. the ones of CentOS Stream.
The `tmt` section tells ALBS which of its plans ALTS runs. Every listed plan
is required: a failure in any of them fails the test task, unless a rule
expects it. List only the plans that must pass:

```yaml
version: 1
tmt:
  # A string is a plan for all platforms and architectures
  plans:
    - /plans/tier1/public
    - /plans/others/public
    # Plans that differ between platforms
    - name: /plans/bind/tier1/public
      platforms: [AlmaLinux-9]
      arches: [x86_64, aarch64]
  # Optional: select tests as tmt run test --filter does
  filter: 'tier: 1'
  # Optional: added to the tmt context ALTS sets (distro, arch, initiator)
  # and ALBS sets (component)
  context:
    trigger: build
  # Optional: environment of the tests
  environment:
    TEST_PROFILE: container
```

| Field | Meaning |
|-------|---------|
| `plans` | plan names (they start with `/`), or `{name, platforms, arches}` with shell-style patterns as in the rules. No plans for a platform and architecture means no tmt there |
| `filter` | a tmt filter of the tests |
| `context` | tmt context dimensions; `distro`, `arch`, `initiator` and `component` can't be set |
| `environment` | environment variables; quote values YAML would read as booleans |

There is no `gating` key, a config with it is invalid. The `warn` outcome of
tmt never fails the test task. Single tmt tests are named in the rules as
`tmt_test_<test name>`, with the name as in `results.yaml` of tmt; `tmt_tests`
covers the stage and all of its tests:

```yaml
skip:
  # Not run at all, a test that reboots would abort the whole tmt run
  - tests: ['tmt_test_/Sanity/cups-systemd-units-smoke']
    reason: 'container: calls reboot, which stops the container'
  - tests: [tmt_tests]
    arches: [s390x]
    reason: tmt isn't run on s390x
expected_failures:
  - tests: ['tmt_test_/Sanity/bz2015182-*']
    reason: Known upstream bug, fails on CentOS Stream 10 too
```

The tmt tests run in a separate **component test task**, one per build task,
i.e. per build, platform and architecture, named after the source package.
When ALBS creates the test tasks of a build task, it takes the head of the
`main` branch of the tests repository, reads `alts.yaml` at that commit and,
if it has plans for the platform and architecture, adds the component test
task. The task keeps the commit: ALTS checks out exactly this commit, and the
results are judged by `alts.yaml` at it, so pushes made in the meantime don't
change the run. ALTS installs all binary packages of the build task except
`*-debuginfo` and `*-debugsource` before running tmt. The task runs no other
tests, and the package test tasks don't run tmt. Restarting only the failed
tests keeps a passed component test task; a new one tests the head of `main`
again.

Rules apply to the component test task as to a package named after the
source package: a rule without `packages`, or one whose `packages` match the
source package name. A `skip` rule without `tests`, or with `tmt_tests`,
leaves the build task without a component test task.

A build task gets a component test task only when the source package has a
`tests/<pkg>` repository with a `main` branch whose `alts.yaml` has tmt plans
for the platform and architecture; other packages are tested as before.

Component test tasks are created when `PACKAGE_TESTS_TMT_ENABLED` is true (the
default), for the platforms and architectures matching the shell-style
patterns of `PACKAGE_TESTS_TMT_PLATFORMS` and `PACKAGE_TESTS_TMT_ARCHES`, both
JSON lists; an empty list matches nothing. By default these are every
AlmaLinux platform (`["AlmaLinux-8", "AlmaLinux-9", "AlmaLinux-10",
"AlmaLinux-Kitten-10"]`) and every architecture they are built for except
i686 and riscv64 (`["x86_64", "x86_64_v2", "aarch64", "ppc64le", "s390x"]`).
These settings affect component test tasks only: package test tasks are
created on every architecture, i686 and riscv64 included, as before.

`PACKAGE_TESTS_TMT_ENABLED=false` switches tmt off: component test tasks that
still wait for ALTS are cancelled, and the `tmt` section changes nothing but
is still validated.

A failed component test task fails the tests of the build task only: like any
test task, it doesn't block signing or releasing the build.

### What it changes

- **Packages skipped completely** (a `skip` rule without `tests`) get no test
  task, so ALTS doesn't test them at all.
- **Skipped tests** are sent to ALTS as `skipped_tests` of the test task and
  are also stored with it. Until ALTS supports this, it still runs them, but
  their results never fail the test task.
- **Expected failures** don't fail the test task. A failed third-party test
  also fails `third_party_tests_wrapper`, which is then judged by that test.
  Failures of the test environment can't be skipped or expected.

The ALTS result itself isn't changed: an expected failure still shows up as a
failed stage in the test logs. What the config did is stored next to it in the
`tests_config` field of the test task's `alts_response`
(`GET /api/v1/tests/<build_task_id>/latest`):

```json
{
  "url": "https://git.almalinux.org/tests/systemd/src/branch/main/alts.yaml",
  "commit": "<last commit of alts.yaml>",
  "skipped": [{"test": "uninstall_package", "reason": "dnf refuses ..."}],
  "expected_failures": [
    {"test": "install_package", "reason": "The standalone sysusers/tmpfiles ..."}
  ],
  "unexpected_passes": ["install_package"],
  "tmt": {
    "summary": {"pass": 40, "fail": 2, "warn": 1, "skip": 3}
  },
  "error": "Invalid config: ..."
}
```

`unexpected_passes` lists the tests that a rule expects to fail but that have
passed; such a rule may be obsolete. `tmt` is there when ALTS ran tmt tests:
the number of tests by outcome. `error` explains why the
config couldn't be used. The field is absent when the package has no config
and ran no tmt tests.

The build (`GET /api/v1/builds/<build_id>/`, also in the list of builds) shows
for each test task its `kind`: `package` or `component`.

A config applies to the test tasks created after it and to the test results
that come after it. To re-check a build that has already been tested,
restart its tests. Restarting only the failed tests re-tests just the packages
that haven't passed, the others keep their results in the new revision.
