"""Per-package tests config from git and its effect on ALTS results.

The config of a package lives in the repository of the tests organization
named like the package repository in git.almalinux.org/rpms, e.g.
git.almalinux.org/tests/systemd for git.almalinux.org/rpms/systemd.
"""

import base64
import dataclasses
import fnmatch
import logging
import re
import time
import typing
import urllib.parse

import aiohttp
import hawkey
import yaml

from alws.config import settings
from alws.schemas.package_tests_schema import (
    THIRD_PARTY_TEST_PREFIX,
    TMT_TEST_PREFIX,
    TMT_TESTS,
    ExpectedFailureRule,
    MatchedRules,
    PackageTestsConfig,
    SkipRule,
)
from alws.utils.parsing import parse_rpm_nevra

__all__ = [
    'PackageTestsConfigLookup',
    'ResultVerdict',
    'TmtResult',
    'build_package_tests_payload',
    'collect_checks',
    'collect_tmt_results',
    'evaluate_test_result',
    'get_matched_rules',
    'get_package_spec',
    'get_package_tests_config',
    'get_source_package_name',
    'get_tests_commit',
    'is_tmt_enabled',
    'is_tmt_package',
    'selects',
]


# Test results wait for the lookup, a stuck Gitea shouldn't hold them long
FETCH_TIMEOUT = 10  # seconds
# Lookups that couldn't reach Gitea are retried sooner than the cache expires
FAILED_FETCH_CACHE_TTL = 60  # seconds
# ALTS reports these sections as {check: {'success': ...}} instead of being
# a single {'success': ...} check
TMT_RESULT_SECTION = 'tmt'
NESTED_RESULT_SECTIONS = ('tests', 'third_party', TMT_RESULT_SECTION)
SERVICE_RESULT_KEYS = ('logs', 'skipped_tests', 'revoked')
# Fails whenever any third-party test fails, or the tests couldn't be run
THIRD_PARTY_WRAPPER = 'third_party_tests_wrapper'
# Stands for all tests of a package in the reports
ALL_TESTS = 'all'
# A full git commit hash, what ALTS checks the tests out at
COMMIT_RE = re.compile(r'[0-9a-f]{40}|[0-9a-f]{64}')
# Outcomes of tmt tests (tmt results.yaml) that aren't failures: warn
# doesn't fail the test task (D-20), skip isn't a result at all. Anything
# else, fail, error or e.g. pending of a test the run never got to, fails.
TMT_NOT_FAILED_OUTCOMES = ('pass', 'info', 'warn', 'skip')
TMT_SKIPPED_OUTCOME = 'skip'
TMT_WARNING_OUTCOME = 'warn'
# Binary packages of a build that tmt tests don't get installed
TMT_IGNORED_PACKAGES = ('*-debuginfo', '*-debugsource')


@dataclasses.dataclass(frozen=True)
class PackageTestsConfigLookup:
    """Outcome of looking a package tests config up in git.

    ``url`` is None when the package has no config at all, ``error`` is set
    when the config exists but couldn't be fetched or is invalid.
    """

    url: typing.Optional[str] = None
    commit: typing.Optional[str] = None
    config: typing.Optional[PackageTestsConfig] = None
    error: typing.Optional[str] = None

    def to_dict(self) -> typing.Optional[dict]:
        if not self.url:
            return None
        info = {'url': self.url, 'commit': self.commit}
        if self.error:
            info['error'] = self.error
        return info


@dataclasses.dataclass(frozen=True)
class ResultVerdict:
    failed: bool
    # [{'test': <test from the config>, 'reason': <rule reason>}] for the
    # tests the config doesn't run for the package, the results of those
    # that ran anyway are ignored
    skipped: typing.List[dict]
    # [{'test': <check>, 'reason': <rule reason>}] for the checks that
    # failed as the config expects
    expected_failures: typing.List[dict]
    # Checks expected to fail which have passed, their rules may be obsolete
    unexpected_passes: typing.List[str]
    # What tmt did, when the result has tmt tests, see _summarize_tmt
    tmt: typing.Optional[dict] = None


@dataclasses.dataclass(frozen=True)
class TmtResult:
    """One tmt test from the ``tmt`` section of an ALTS result."""

    # Key of the result in the section, unique within it
    key: str
    # Test name as in results.yaml of tmt, e.g. /Sanity/basic-functionality
    name: str
    # Plan the test ran in, None if ALTS didn't say
    plan: typing.Optional[str]
    # result of the test in tmt results.yaml: pass, fail, error, warn,
    # info, skip, pending, ...; None if ALTS didn't say
    outcome: typing.Optional[str]
    passed: bool

    @property
    def skipped(self) -> bool:
        return self.outcome == TMT_SKIPPED_OUTCOME

    @property
    def troubled(self) -> bool:
        """Whether the test may be why ALTS failed the tmt stage."""
        return not self.passed or self.outcome == TMT_WARNING_OUTCOME

    @property
    def names(self) -> typing.Tuple[str, ...]:
        """What config rules can call the test.

        The key differs from tmt_test_<name> when the same test ran in
        several plans, rules are still written with the test name.
        """
        return tuple(dict.fromkeys((self.key, TMT_TEST_PREFIX + self.name)))


def get_source_package_name(ref_url: str) -> typing.Optional[str]:
    """Name of the package repository in git.almalinux.org/rpms."""
    name = urllib.parse.urlparse(ref_url).path.rstrip('/').split('/')[-1]
    if name.endswith('.src.rpm'):
        try:
            # The newer rpms repositories spell "+" so, e.g. libsigcplusplus20
            name = parse_rpm_nevra(name).name.replace('+', 'plus')
        except hawkey.ValueException:
            return None
    else:
        name = name.removesuffix('.git')
    # Not names of repositories, but would change the path of the request
    if name in ('', '.', '..'):
        return None
    return name


_lookups_cache: typing.Dict[
    str,
    typing.Tuple[float, PackageTestsConfigLookup],
] = {}


def _get_web_url(
    package_name: str,
    commit: typing.Optional[str] = None,
) -> str:
    ref = (
        f'commit/{commit}'
        if commit
        else f'branch/{settings.package_tests_branch}'
    )
    return urllib.parse.urljoin(
        settings.gitea_host,
        f'/{settings.package_tests_org}/{package_name}/src/{ref}/'
        f'{settings.package_tests_config_path}',
    )


def _get_api_url(package_name: str, *path: str) -> str:
    endpoint = '/'.join((
        'repos',
        urllib.parse.quote(settings.package_tests_org, safe=''),
        urllib.parse.quote(package_name, safe=''),
        *path,
    ))
    return urllib.parse.urljoin(settings.gitea_host, endpoint)


async def _get_json(url: str, **params) -> typing.Any:
    """Answer of the Gitea API, None when it's 404."""
    async with aiohttp.ClientSession(
        trust_env=True,
        timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT),
    ) as session:
        async with session.get(url, params=params) as response:
            # Gitea answers 404 for a missing repository, branch and file
            # alike
            if response.status == 404:
                return None
            response.raise_for_status()
            return await response.json()


async def _fetch_config_file(
    package_name: str,
    ref: typing.Optional[str] = None,
) -> typing.Any:
    """Gitea contents entry of the config, None if there is no such file.

    ``ref`` is a commit, the tests branch by default.
    """
    return await _get_json(
        _get_api_url(
            package_name,
            'contents',
            urllib.parse.quote(settings.package_tests_config_path),
        ),
        ref=ref or settings.package_tests_branch,
    )


async def _fetch_branch_commit(package_name: str) -> typing.Optional[str]:
    """Head of the tests branch, None if there is no such branch."""
    branch = await _get_json(
        _get_api_url(
            package_name,
            'branches',
            urllib.parse.quote(settings.package_tests_branch, safe=''),
        )
    )
    if branch is None:
        return None
    commit = branch['commit']['id']
    if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
        raise ValueError(f'Unexpected commit of the tests branch: {commit!r}')
    return commit


async def get_tests_commit(package_name: str) -> typing.Optional[str]:
    """Full hash of the head of the tests repository branch.

    None when there is no such repository or branch, or it can't be told
    right now: then the package has no tmt tests this time.
    """
    if not settings.package_tests_enabled:
        return None
    try:
        return await _fetch_branch_commit(package_name)
    except Exception:
        logging.exception(
            'Cannot get the head of %s of tests/%s',
            settings.package_tests_branch,
            package_name,
        )
        return None


async def _load_config(
    package_name: str,
    commit: typing.Optional[str] = None,
) -> PackageTestsConfigLookup:
    file_info = await _fetch_config_file(package_name, commit)
    if file_info is None:
        return PackageTestsConfigLookup()
    if not isinstance(file_info, dict) or file_info.get('type') != 'file':
        url = _get_web_url(package_name, commit)
        return PackageTestsConfigLookup(url=url, error=f'{url} is not a file')
    url = file_info.get('html_url') or _get_web_url(package_name, commit)
    commit = file_info.get('last_commit_sha')
    try:
        content = base64.b64decode(file_info['content']).decode('utf-8')
        config = PackageTestsConfig.model_validate(
            yaml.safe_load(content) or {}
        )
    # ValidationError of pydantic is a ValueError too
    except (KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
        logging.warning('Tests config %s is invalid: %s', url, exc)
        return PackageTestsConfigLookup(
            url=url,
            commit=commit,
            error=f'Invalid config: {exc}',
        )
    return PackageTestsConfigLookup(url=url, commit=commit, config=config)


async def get_package_tests_config(
    package_name: str,
    commit: typing.Optional[str] = None,
) -> PackageTestsConfigLookup:
    """The config of the package on the tests branch or at the commit."""
    if not settings.package_tests_enabled:
        return PackageTestsConfigLookup()
    cache_key = f'{package_name}@{commit}' if commit else package_name
    cached = _lookups_cache.get(cache_key)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    ttl = settings.package_tests_cache_ttl
    try:
        lookup = await _load_config(package_name, commit)
    # Whatever goes wrong here, test results must still be processed
    except Exception as exc:
        url = _get_web_url(package_name, commit)
        logging.exception('Cannot fetch tests config %s', url)
        # Not repr(): the one of aiohttp errors has the request headers, and
        # the lookup ends up in the public test results
        lookup = PackageTestsConfigLookup(
            url=url,
            error=f'Cannot fetch the config: {type(exc).__name__}: {exc}',
        )
        ttl = min(ttl, FAILED_FETCH_CACHE_TTL)
    _lookups_cache[cache_key] = (time.monotonic() + ttl, lookup)
    return lookup


def _parse_tmt_result(key: str, item: dict) -> TmtResult:
    # result, as in tmt results.yaml; outcome is what ALBS asked for first
    outcome = item.get('result', item.get('outcome'))
    outcome = outcome.lower() if isinstance(outcome, str) else None
    if outcome is None:
        passed = bool(item.get('success', False))
    else:
        passed = outcome in TMT_NOT_FAILED_OUTCOMES
    name = item.get('name')
    if not isinstance(name, str) or not name:
        name = key.removeprefix(TMT_TEST_PREFIX)
    plan = item.get('plan')
    return TmtResult(
        key=key,
        name=name,
        plan=plan if isinstance(plan, str) and plan else None,
        outcome=outcome,
        passed=passed,
    )


def collect_tmt_results(result: dict) -> typing.Dict[str, TmtResult]:
    """tmt tests of the ALTS result by their keys.

    The contract with ALTS (its tmt_tests stage) is::

        "tmt_tests": {"success": <false only if tmt hasn't run all plans>},
        "tmt": {
            "tmt_test_/Sanity/foo": {
                "name": "/Sanity/foo",
                "plan": "/plans/tier1/public",
                "result": "pass",
                "success": true,
                ...
            },
        },
        "skipped_tests": [..., "tmt_test_/Sanity/bar"],

    The key is tmt_test_<name>, tmt_test_<name>@<plan> when the same test
    ran in several plans. ``result`` is that of tmt results.yaml and
    decides over ``success``; ``outcome`` is read when there is no
    ``result``. Skipped tests are only in ``skipped_tests``.
    """
    section = result.get(TMT_RESULT_SECTION)
    if not isinstance(section, dict):
        return {}
    return {
        key: _parse_tmt_result(key, item)
        for key, item in section.items()
        if isinstance(item, dict)
    }


def collect_checks(result: dict) -> typing.Dict[str, bool]:
    """Map every check from the ALTS result to whether it has passed."""
    checks = {}
    for key, item in result.items():
        if key in SERVICE_RESULT_KEYS or not isinstance(item, dict):
            continue
        if key == TMT_RESULT_SECTION:
            # Skipped tmt tests didn't run, there is nothing to check
            checks.update(
                (name, tmt.passed)
                for name, tmt in collect_tmt_results(result).items()
                if not tmt.skipped
            )
            continue
        if key not in NESTED_RESULT_SECTIONS:
            checks[key] = bool(item.get('success', False))
            continue
        for name, check in item.items():
            if isinstance(check, dict):
                checks[name] = bool(check.get('success', False))
    return checks


def selects(test: str, check: str) -> bool:
    """Whether a test named in the config covers a check of ALTS result."""
    if test == 'third_party_tests':
        return check == THIRD_PARTY_WRAPPER or check.startswith(
            THIRD_PARTY_TEST_PREFIX
        )
    if test == TMT_TESTS:
        return check == TMT_TESTS or check.startswith(TMT_TEST_PREFIX)
    if test.startswith((THIRD_PARTY_TEST_PREFIX, TMT_TEST_PREFIX)):
        return fnmatch.fnmatchcase(check, test)
    return check == test


def _matches_setting(value: str, patterns: typing.Iterable[str]) -> bool:
    return any(
        fnmatch.fnmatchcase(value.lower(), pattern.lower())
        for pattern in patterns
    )


def is_tmt_enabled(platform_name: str, arch: str) -> bool:
    """Whether tmt plans are sent to ALTS for the platform and arch."""
    return (
        settings.package_tests_tmt_enabled
        and _matches_setting(
            platform_name, settings.package_tests_tmt_platforms
        )
        and _matches_setting(arch, settings.package_tests_tmt_arches)
    )


def _get_git_url(package_name: str) -> str:
    return urllib.parse.urljoin(
        settings.gitea_host,
        f'/{settings.package_tests_org}/{package_name}.git',
    )


def is_tmt_package(name: str) -> bool:
    """Whether tmt tests get the binary package of the build installed."""
    return not _matches_setting(name, TMT_IGNORED_PACKAGES)


def get_package_spec(
    name: str,
    epoch: typing.Optional[typing.Union[str, int]],
    version: str,
    release: str,
    arch: str,
) -> str:
    """dnf spec of exactly this package: name-[epoch:]version-release.arch"""
    epoch = f'{epoch}:' if epoch and str(epoch) != '0' else ''
    return f'{name}-{epoch}{version}-{release}.{arch}'


def build_package_tests_payload(
    package_name: str,
    commit: str,
    config: typing.Optional[PackageTestsConfig],
    platform_name: str,
    arch: str,
    packages: typing.Iterable[str] = (),
    skipped_tests: typing.Optional[typing.Iterable[str]] = None,
) -> typing.Optional[dict]:
    """``package_tests`` of a component test task, None if it runs no tmt.

    This is the whole contract with ALTS (PackageTests of its payload)::

        "package_tests": {
            "url": "https://git.almalinux.org/tests/<package>.git",
            "commit": "<full hash of the tests commit>",
            "packages": ["<name>-<version>-<release>.<arch>", ...],
            "tmt": {
                "plans": ["/plans/tier1/public", "/plans/others/public"],
                "filter": "tier: 1",
                "context": {"component": "<package>", ...},
                "environment": {"NAME": "value", ...}
            }
        }

    ``config`` is alts.yaml at ``commit``. ``plans`` are its plans for the
    platform and arch, in the config order; all of them must pass.
    ``filter`` is absent when the config has none. ``context`` is
    added to the one ALTS sets (distro, arch, initiator). ``packages`` are
    the binary packages of the build that ALTS installs before tmt. Tests
    named in ``skipped_tests`` of the task as tmt_test_<name> aren't run.

    ``package_name`` is the name of the tests repository, i.e. of the
    source package.
    """
    if not config or not config.tmt:
        return None
    if not is_tmt_enabled(platform_name, arch):
        return None
    # A skip rule switched the whole tmt stage off
    if TMT_TESTS in (skipped_tests or ()):
        return None
    plans = config.tmt.get_plans(platform_name, arch)
    if not plans:
        return None
    tmt = {
        'plans': plans,
        'context': {**config.tmt.context, 'component': package_name},
        'environment': dict(config.tmt.environment),
    }
    if config.tmt.filter is not None:
        tmt['filter'] = config.tmt.filter
    return {
        'url': _get_git_url(package_name),
        'commit': commit,
        'packages': list(packages),
        'tmt': tmt,
    }


def get_matched_rules(
    config: typing.Optional[PackageTestsConfig],
    package_name: str,
    platform_name: str,
    arch: str,
) -> MatchedRules:
    if not config:
        return MatchedRules()
    return config.get_rules(package_name, platform_name, arch)


def _get_reasons(
    rules: typing.Iterable[typing.Union[ExpectedFailureRule, SkipRule],],
    checks: typing.Iterable[str],
    aliases: typing.Optional[typing.Dict[str, typing.Tuple[str, ...]]] = None,
) -> typing.Dict[str, str]:
    """Map the checks covered by the rules to the reason of the first one.

    ``aliases`` are other names of a check that the rules may use.
    """
    aliases = aliases or {}
    reasons = {}
    for rule in rules:
        for test in rule.tests or ():
            for check in checks:
                if any(
                    selects(test, name) for name in aliases.get(check, (check,))
                ):
                    reasons.setdefault(check, rule.reason)
    return reasons


def _summarize_tmt(tmt_results: typing.Dict[str, TmtResult]) -> dict:
    """What tmt did, for tests_config of the test task."""
    summary = {}
    for tmt in tmt_results.values():
        outcome = tmt.outcome or ('pass' if tmt.passed else 'fail')
        summary[outcome] = summary.get(outcome, 0) + 1
    # The number of tmt tests by outcome
    return {'summary': summary}


def evaluate_test_result(
    result: dict,
    rules: typing.Optional[MatchedRules] = None,
) -> ResultVerdict:
    rules = rules or MatchedRules()
    skipped = [
        {'test': test, 'reason': rule.reason}
        for rule in rules.skip
        for test in rule.tests or (ALL_TESTS,)
    ]
    # The package isn't tested at all, its test task got here only because
    # it had been created before the config said so
    if rules.skipped_package:
        return ResultVerdict(
            failed=False,
            skipped=skipped,
            expected_failures=[],
            unexpected_passes=[],
        )
    checks = collect_checks(result)
    tmt_results = collect_tmt_results(result)
    aliases = {key: tmt.names for key, tmt in tmt_results.items()}
    skip_reasons = _get_reasons(rules.skip, checks, aliases)
    reasons = _get_reasons(rules.expected_failures, checks, aliases)
    failed = [name for name, passed in checks.items() if not passed]
    if THIRD_PARTY_WRAPPER in failed and any(
        name.startswith(THIRD_PARTY_TEST_PREFIX) for name in failed
    ):
        # Failed third-party tests make it fail, they decide on their own
        failed.remove(THIRD_PARTY_WRAPPER)
    if TMT_TESTS in failed and any(
        tmt.troubled for tmt in tmt_results.values()
    ):
        # So do tmt tests for the tmt stage: it fails only by itself when
        # all its tests are fine, e.g. the tests repository didn't clone
        failed.remove(TMT_TESTS)
    failed = [name for name in failed if name not in skip_reasons]
    # Every tmt plan is required, a failure in any of them counts as the
    # failure of any other test
    return ResultVerdict(
        failed=any(name not in reasons for name in failed),
        skipped=skipped,
        expected_failures=[
            {'test': name, 'reason': reasons[name]}
            for name in failed
            if name in reasons
        ],
        unexpected_passes=[
            name
            for name in reasons
            if checks[name] and name not in skip_reasons
        ],
        tmt=_summarize_tmt(tmt_results) if tmt_results else None,
    )
