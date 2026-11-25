"""The tmt section of package tests configs and tmt results of ALTS."""

import typing

import pytest
import yaml
from pydantic import ValidationError

from alws import models
from alws.config import settings
from alws.schemas import build_schema
from alws.schemas.package_tests_schema import MatchedRules, PackageTestsConfig
from alws.utils import package_tests
from alws.utils.package_tests import (
    build_package_tests_payload,
    collect_checks,
    collect_tmt_results,
    evaluate_test_result,
    get_package_spec,
    get_package_tests_config,
    get_tests_commit,
    is_tmt_enabled,
    is_tmt_package,
    selects,
)
from scripts import check_package_tests_config
from tests.fixtures.tests import PackageTestsConfigs, passed_result

pytestmark = pytest.mark.anyio

# The fixtures replace it with a fake Gitea for every test
ORIGINAL_FETCH_BRANCH = package_tests._fetch_branch_commit

# As in git.almalinux.org/tests/grep and tests/rsync, every plan required
GREP_CONFIG_YAML = """
version: 1
tmt:
  plans:
    - /plans/tier1/public
    - /plans/tier2-tier3/public
    - /plans/others/public
"""
# The first drafts of the format had plans that don't gate
GATING_CONFIG_YAML = (
    GREP_CONFIG_YAML
    + """  gating:
    - /plans/tier1/public
"""
)
# The example of the plan (CFG-1): plans that differ between platforms
BIND_CONFIG_YAML = """
version: 1
tmt:
  plans:
    - name: /plans/bind/tier1/public
      platforms: [AlmaLinux-9]
    - name: /plans/tier1/public
      platforms: [AlmaLinux-10, AlmaLinux-Kitten-10]
    - /plans/others/public
  filter: 'tier: 1'
  context:
    trigger: build
  environment:
    TEST_PROFILE: container
    TIMEOUT: 30
skip:
  - tests: [tmt_tests]
    arches: [s390x]
    reason: Example, tmt is not run on s390x yet
expected_failures:
  - tests: ['tmt_test_/Sanity/bz2015182-*']
    reason: Known upstream bug, fails on CentOS Stream 10 too
"""
# Like the configs of the 8 repositories made before tmt
SYSTEMD_CONFIG_YAML = """
version: 1
expected_failures:
  - tests: [install_package]
    packages: [systemd-standalone-*]
    reason: Conflicts with systemd from the test image
"""
TIER1 = '/plans/tier1/public'
OTHERS = '/plans/others/public'


def parse(config_yaml: str) -> PackageTestsConfig:
    return PackageTestsConfig.model_validate(yaml.safe_load(config_yaml))


def tmt_config(**tmt) -> dict:
    return {'tmt': {'plans': [TIER1, OTHERS], **tmt}}


def tmt_test(
    name: str,
    outcome: typing.Optional[str] = 'pass',
    plan: typing.Optional[str] = TIER1,
    **extra,
) -> dict:
    item = {'name': name, **extra}
    if plan is not None:
        item['plan'] = plan
    if outcome is not None:
        # As ALTS reports it: the result of tmt results.yaml
        item['result'] = outcome
        item['success'] = outcome in ('pass', 'info', 'warn')
    return item


def tmt_result(
    *tests: dict,
    stage_success: bool = True,
    keys: typing.Optional[typing.List[str]] = None,
) -> dict:
    """ALTS result where tmt ran the given tests."""
    keys = keys or [f'tmt_test_{test["name"]}' for test in tests]
    return {
        **passed_result(),
        'tmt_tests': {'success': stage_success},
        'tmt': dict(zip(keys, tests)),
    }


def config_rules(**rules) -> MatchedRules:
    config = PackageTestsConfig.model_validate(rules)
    return MatchedRules(
        skip=config.skip,
        expected_failures=config.expected_failures,
    )


class TestTmtConfig:
    async def test_grep_config(self):
        config = parse(GREP_CONFIG_YAML)
        assert [plan.name for plan in config.tmt.plans] == [
            TIER1,
            '/plans/tier2-tier3/public',
            OTHERS,
        ]
        assert all(
            plan.platforms is None and plan.arches is None
            for plan in config.tmt.plans
        )
        assert config.tmt.filter is None
        assert config.tmt.context == {}
        assert config.tmt.environment == {}
        rules = config.get_rules('grep', 'AlmaLinux-9', 'x86_64')
        assert rules.skip == rules.expected_failures == []

    async def test_bind_config(self):
        config = parse(BIND_CONFIG_YAML)
        assert config.tmt.filter == 'tier: 1'
        assert config.tmt.context == {'trigger': 'build'}
        # Numbers are strings for tmt
        assert config.tmt.environment == {
            'TEST_PROFILE': 'container',
            'TIMEOUT': '30',
        }
        assert config.skip[0].tests == ['tmt_tests']
        assert config.expected_failures[0].tests == [
            'tmt_test_/Sanity/bz2015182-*'
        ]

    @pytest.mark.parametrize(
        'platform, arch, plans',
        [
            ('AlmaLinux-9', 'x86_64', ['/plans/bind/tier1/public', OTHERS]),
            ('almalinux-9', 'x86_64', ['/plans/bind/tier1/public', OTHERS]),
            ('AlmaLinux-10', 'aarch64', [TIER1, OTHERS]),
            ('AlmaLinux-Kitten-10', 'x86_64_v2', [TIER1, OTHERS]),
            ('AlmaLinux-8', 'x86_64', [OTHERS]),
        ],
    )
    async def test_plans_by_platform(
        self,
        platform: str,
        arch: str,
        plans: typing.List[str],
    ):
        config = parse(BIND_CONFIG_YAML)
        assert config.tmt.get_plans(platform, arch) == plans

    async def test_plans_by_arch(self):
        config = PackageTestsConfig.model_validate({
            'tmt': {
                'plans': [
                    {'name': TIER1, 'arches': 'x86_64*'},
                    TIER1,
                    {'name': OTHERS, 'arches': ['aarch64']},
                ],
            },
        })
        # The same plan twice is run once
        assert config.tmt.get_plans('AlmaLinux-9', 'x86_64_v2') == [TIER1]
        assert config.tmt.get_plans('AlmaLinux-9', 'aarch64') == [
            TIER1,
            OTHERS,
        ]

    async def test_single_values_become_lists(self):
        config = PackageTestsConfig.model_validate({
            'tmt': {'plans': TIER1},
        })
        assert [plan.name for plan in config.tmt.plans] == [TIER1]

    async def test_empty_tmt_section(self):
        config = PackageTestsConfig.model_validate({'tmt': {}})
        assert config.tmt.plans == []
        assert config.tmt.get_plans('AlmaLinux-9', 'x86_64') == []

    async def test_config_without_tmt(self):
        config = parse(SYSTEMD_CONFIG_YAML)
        assert config.tmt is None
        rules = config.get_rules(
            'systemd-standalone-sysusers',
            'AlmaLinux-9',
            'x86_64',
        )
        assert [rule.tests for rule in rules.expected_failures] == [
            ['install_package']
        ]

    async def test_gating_is_rejected(self):
        with pytest.raises(ValidationError) as error:
            parse(GATING_CONFIG_YAML)
        assert 'gating is not supported: every plan in plans is required' in (
            str(error.value)
        )

    @pytest.mark.parametrize(
        'test',
        [
            'tmt_tests',
            'tmt_test_/Sanity/basic-functionality',
            'tmt_test_/Regression/RHEL-9973-*',
            'tmt_test_*',
        ],
    )
    async def test_valid_tmt_test_name(self, test: str):
        PackageTestsConfig.model_validate({
            'skip': [{'tests': [test], 'reason': 'x'}],
            'expected_failures': [{'tests': [test], 'reason': 'x'}],
        })

    @pytest.mark.parametrize(
        'test',
        ['tmt_test_', 'tmt_test', 'tmt', 'tmt_tests_wrapper'],
    )
    async def test_invalid_tmt_test_name(self, test: str):
        with pytest.raises(ValidationError, match='unknown test'):
            PackageTestsConfig.model_validate({
                'skip': [{'tests': [test], 'reason': 'x'}],
            })

    @pytest.mark.parametrize(
        'config, error',
        [
            pytest.param({'tmt': [TIER1]}, 'valid dictionary', id='list'),
            pytest.param(
                {'tmt': {'plan': [TIER1]}},
                'Extra inputs',
                id='unknown-key',
            ),
            pytest.param(
                {'tmt': {'plans': [{'name': TIER1, 'platform': 'x'}]}},
                'Extra inputs',
                id='unknown-plan-key',
            ),
            pytest.param(
                {'tmt': {'plans': [{'platforms': ['AlmaLinux-9']}]}},
                'Field required',
                id='plan-without-name',
            ),
            pytest.param(
                {'tmt': {'plans': ['plans/tier1/public']}},
                'not a tmt plan name',
                id='relative-plan',
            ),
            pytest.param(
                {'tmt': {'plans': ['/plans/tier1/']}},
                'not a tmt plan name',
                id='trailing-slash',
            ),
            pytest.param(
                {'tmt': {'plans': ['/']}},
                'not a tmt plan name',
                id='root',
            ),
            pytest.param(
                {'tmt': {'plans': ['/plans/tier 1']}},
                'has spaces',
                id='spaces',
            ),
            pytest.param(
                {'tmt': {'plans': [{'name': TIER1, 'platforms': []}]}},
                'at least 1 item',
                id='no-platforms',
            ),
            pytest.param(
                tmt_config(gating=[TIER1]),
                'gating is not supported',
                id='gating',
            ),
            pytest.param(
                {'tmt': {'gating': [TIER1]}},
                'gating is not supported',
                id='gating-without-plans',
            ),
            pytest.param(
                tmt_config(gating=[]),
                'gating is not supported',
                id='empty-gating',
            ),
            pytest.param(
                tmt_config(filter='  '),
                'filter is empty',
                id='blank-filter',
            ),
            pytest.param(
                tmt_config(context={'distro': 'rhel-9'}),
                'is set by ALTS and ALBS',
                id='reserved-context',
            ),
            pytest.param(
                tmt_config(context={'component': 'other'}),
                'is set by ALTS and ALBS',
                id='component-context',
            ),
            pytest.param(
                tmt_config(context={'a=b': 'c'}),
                'not a tmt context dimension',
                id='bad-context-name',
            ),
            pytest.param(
                tmt_config(context={'trigger': True}),
                'quote the value',
                id='boolean-context',
            ),
            pytest.param(
                tmt_config(environment={'1ABC': 'x'}),
                'not an environment variable',
                id='bad-environment-name',
            ),
            pytest.param(
                tmt_config(environment={'A': ['x']}),
                'valid string',
                id='list-environment-value',
            ),
            pytest.param(
                tmt_config(environment=['A=x']),
                'valid dictionary',
                id='environment-list',
            ),
        ],
    )
    async def test_invalid_tmt_section(self, config: dict, error: str):
        with pytest.raises(ValidationError, match=error):
            PackageTestsConfig.model_validate(config)


COMMIT = '0123456789abcdef0123456789abcdef01234567'
GREP_PACKAGES = ['grep-3.11-10.el10.x86_64']


def payload(
    config: typing.Optional[PackageTestsConfig],
    platform: str = 'AlmaLinux-9',
    arch: str = 'x86_64',
    package: str = 'grep',
    skipped_tests: typing.Optional[typing.List[str]] = None,
) -> typing.Optional[dict]:
    return build_package_tests_payload(
        package,
        COMMIT,
        config,
        platform,
        arch,
        packages=GREP_PACKAGES,
        skipped_tests=skipped_tests,
    )


class TestPackageTestsPayload:
    @pytest.fixture(autouse=True)
    def tmt_enabled(self, monkeypatch):
        monkeypatch.setattr(settings, 'package_tests_tmt_enabled', True)
        monkeypatch.setattr(
            settings,
            'package_tests_tmt_platforms',
            ['AlmaLinux-*'],
        )
        monkeypatch.setattr(
            settings,
            'package_tests_tmt_arches',
            ['x86_64', 'aarch64'],
        )

    async def test_grep(self):
        # Exactly what PackageTests of ALTS takes: no branch
        assert payload(parse(GREP_CONFIG_YAML)) == {
            'url': 'https://git.almalinux.org/tests/grep.git',
            'commit': COMMIT,
            'packages': GREP_PACKAGES,
            'tmt': {
                'plans': [TIER1, '/plans/tier2-tier3/public', OTHERS],
                'context': {'component': 'grep'},
                'environment': {},
            },
        }

    async def test_plans_of_the_platform(self):
        config = parse(BIND_CONFIG_YAML)
        assert payload(config, arch='aarch64', package='bind')['tmt'] == {
            'plans': ['/plans/bind/tier1/public', OTHERS],
            'filter': 'tier: 1',
            'context': {'trigger': 'build', 'component': 'bind'},
            'environment': {'TEST_PROFILE': 'container', 'TIMEOUT': '30'},
        }
        assert payload(config, 'AlmaLinux-8')['tmt']['plans'] == [OTHERS]

    async def test_no_plans_for_the_platform(self):
        config = PackageTestsConfig.model_validate({
            'tmt': {'plans': [{'name': TIER1, 'platforms': 'AlmaLinux-10'}]},
        })
        assert not payload(config)

    @pytest.mark.parametrize(
        'config',
        [
            pytest.param(None, id='no-config'),
            pytest.param(SYSTEMD_CONFIG_YAML, id='no-tmt'),
            pytest.param('tmt: {}', id='no-plans'),
        ],
    )
    async def test_nothing_to_run(self, config: typing.Optional[str]):
        assert not payload(parse(config) if config else None)

    async def test_stage_skipped(self):
        config = parse(GREP_CONFIG_YAML)
        assert not payload(
            config,
            skipped_tests=['uninstall_package', 'tmt_tests'],
        )
        # Single tests are skipped by ALTS, through skipped_tests
        assert payload(config, skipped_tests=['tmt_test_/Sanity/foo'])

    @pytest.mark.parametrize(
        'enabled, platforms, arches, platform, arch, sent',
        [
            (False, ['*'], ['*'], 'AlmaLinux-9', 'x86_64', False),
            (True, [], ['*'], 'AlmaLinux-9', 'x86_64', False),
            (True, ['AlmaLinux-9'], ['*'], 'AlmaLinux-9', 'x86_64', True),
            (True, ['almalinux-9'], ['*'], 'AlmaLinux-9', 'x86_64', True),
            (True, ['AlmaLinux-9'], ['*'], 'AlmaLinux-10', 'x86_64', False),
            (True, ['*'], ['x86_64'], 'AlmaLinux-9', 'x86_64', True),
            (True, ['*'], ['x86_64'], 'AlmaLinux-9', 's390x', False),
            (True, ['*'], [], 'AlmaLinux-9', 'x86_64', False),
        ],
    )
    async def test_settings(
        self,
        monkeypatch,
        enabled: bool,
        platforms: typing.List[str],
        arches: typing.List[str],
        platform: str,
        arch: str,
        sent: bool,
    ):
        monkeypatch.setattr(settings, 'package_tests_tmt_enabled', enabled)
        monkeypatch.setattr(
            settings,
            'package_tests_tmt_platforms',
            platforms,
        )
        monkeypatch.setattr(settings, 'package_tests_tmt_arches', arches)
        assert is_tmt_enabled(platform, arch) is sent
        assert bool(payload(parse(GREP_CONFIG_YAML), platform, arch)) is sent


@pytest.mark.parametrize(
    'epoch, spec',
    [
        (None, 'bind-9.18.33-5.el10.x86_64'),
        ('0', 'bind-9.18.33-5.el10.x86_64'),
        (0, 'bind-9.18.33-5.el10.x86_64'),
        ('32', 'bind-32:9.18.33-5.el10.x86_64'),
    ],
)
async def test_get_package_spec(epoch, spec: str):
    assert (
        get_package_spec('bind', epoch, '9.18.33', '5.el10', 'x86_64') == spec
    )


@pytest.mark.parametrize(
    'name, installed',
    [
        ('grep', True),
        ('bind-libs', True),
        ('bind-debuginfo', False),
        ('bind-libs-debuginfo', False),
        ('bind-debugsource', False),
    ],
)
async def test_is_tmt_package(name: str, installed: bool):
    assert is_tmt_package(name) is installed


class TestTestsCommit:
    async def test_head(self, monkeypatch):
        calls = []

        async def get_json(url: str, **params):
            calls.append((url, params))
            return {'name': 'main', 'commit': {'id': COMMIT}}

        # The fake Gitea of the fixtures stands in for the branch lookup
        monkeypatch.setattr(
            package_tests, '_fetch_branch_commit', ORIGINAL_FETCH_BRANCH
        )
        monkeypatch.setattr(package_tests, '_get_json', get_json)
        assert await get_tests_commit('grep') == COMMIT
        assert calls == [(
            'https://git.almalinux.org/api/v1/repos/tests/grep/branches/main',
            {},
        )]

    @pytest.mark.parametrize(
        'answer',
        [
            pytest.param(None, id='no-branch'),
            pytest.param({'commit': {'id': 'main'}}, id='not-a-hash'),
            pytest.param({'commit': {'id': COMMIT[:12]}}, id='short-hash'),
            pytest.param({'name': 'main'}, id='garbage'),
            pytest.param(RuntimeError('down'), id='error'),
        ],
    )
    async def test_no_head(self, monkeypatch, answer):
        async def get_json(url: str, **params):
            if isinstance(answer, Exception):
                raise answer
            return answer

        monkeypatch.setattr(
            package_tests, '_fetch_branch_commit', ORIGINAL_FETCH_BRANCH
        )
        monkeypatch.setattr(package_tests, '_get_json', get_json)
        assert await get_tests_commit('grep') is None

    async def test_lookups_off(self, monkeypatch):
        monkeypatch.setattr(settings, 'package_tests_enabled', False)
        assert await get_tests_commit('grep') is None

    async def test_config_at_commit(
        self,
        package_tests_configs: PackageTestsConfigs,
    ):
        package_tests_configs['grep'] = GREP_CONFIG_YAML
        package_tests_configs.at_commit[('grep', COMMIT)] = SYSTEMD_CONFIG_YAML
        at_commit = await get_package_tests_config('grep', COMMIT)
        on_branch = await get_package_tests_config('grep')
        assert at_commit.config.tmt is None
        assert on_branch.config.tmt is not None
        # A commit doesn't change, its config is looked up once
        await get_package_tests_config('grep', COMMIT)
        assert package_tests_configs.lookups == [f'grep@{COMMIT}', 'grep']


@pytest.mark.parametrize(
    'platform, arch, sent',
    [
        *[
            (platform, arch, True)
            for platform in ('AlmaLinux-8', 'AlmaLinux-9')
            for arch in ('x86_64', 'aarch64', 'ppc64le', 's390x')
        ],
        *[
            (platform, arch, True)
            for platform in ('AlmaLinux-10', 'AlmaLinux-Kitten-10')
            # x86_64_v2: the arch of the altarch builds of AlmaLinux 10
            for arch in ('x86_64', 'x86_64_v2', 'aarch64', 'ppc64le', 's390x')
        ],
        # No tmt on them
        *[
            (platform, arch, False)
            for platform in (
                'AlmaLinux-8',
                'AlmaLinux-9',
                'AlmaLinux-10',
                'AlmaLinux-Kitten-10',
            )
            for arch in ('i686', 'riscv64')
        ],
        # Patterns match whole names
        ('AlmaLinux-10', 'x86_64_v3', False),
        ('CentOS-7', 'x86_64', False),
        ('AlmaLinux-9-beta', 'x86_64', False),
    ],
)
async def test_tmt_settings_by_default(platform: str, arch: str, sent: bool):
    assert settings.package_tests_tmt_enabled is True
    assert is_tmt_enabled(platform, arch) is sent
    # The tmt plans reach ALTS exactly where tmt is enabled
    assert bool(payload(parse('tmt: {plans: [/plans/a]}'), platform, arch)) is (
        sent
    )


async def test_collect_tmt_results():
    result = tmt_result(
        tmt_test('/a'),
        tmt_test('/b', 'fail', plan=OTHERS),
        # Older results may have only success, a broken one nothing at all
        {'success': True},
        {},
        keys=['tmt_test_/a', 'tmt_test_/b', 'tmt_test_/c', 'tmt_test_/d'],
    )
    result['tmt']['not a test'] = 'garbage'
    tests = collect_tmt_results(result)
    assert list(tests) == [
        'tmt_test_/a',
        'tmt_test_/b',
        'tmt_test_/c',
        'tmt_test_/d',
    ]
    assert (tests['tmt_test_/a'].name, tests['tmt_test_/a'].plan) == (
        '/a',
        TIER1,
    )
    assert tests['tmt_test_/a'].passed
    assert (tests['tmt_test_/b'].outcome, tests['tmt_test_/b'].passed) == (
        'fail',
        False,
    )
    assert tests['tmt_test_/c'].name == '/c'
    assert tests['tmt_test_/c'].plan is None
    assert tests['tmt_test_/c'].outcome is None
    assert tests['tmt_test_/c'].passed
    assert not tests['tmt_test_/d'].passed
    assert collect_tmt_results(passed_result()) == {}
    assert collect_tmt_results({'tmt': 'garbage'}) == {}


@pytest.mark.parametrize(
    'outcome, passed',
    [
        ('pass', True),
        ('info', True),
        ('warn', True),
        ('PASS', True),
        ('fail', False),
        ('error', False),
        ('pending', False),
        ('something new', False),
    ],
)
async def test_tmt_outcomes(outcome: str, passed: bool):
    # The result decides, not success
    test = {'name': '/a', 'result': outcome, 'success': not passed}
    checks = collect_checks(tmt_result(test))
    assert checks['tmt_test_/a'] is passed
    # outcome is read when there is no result
    test = {'name': '/a', 'outcome': outcome, 'success': not passed}
    checks = collect_checks(tmt_result(test))
    assert checks['tmt_test_/a'] is passed


async def test_result_over_outcome():
    test = {'name': '/a', 'result': 'fail', 'outcome': 'pass'}
    assert collect_tmt_results(tmt_result(test))['tmt_test_/a'].outcome == (
        'fail'
    )


async def test_collect_checks_with_tmt():
    checks = collect_checks(
        tmt_result(
            tmt_test('/a'),
            tmt_test('/b', 'error'),
            tmt_test('/c', 'skip'),
            stage_success=False,
        )
    )
    assert 'tmt' not in checks
    assert checks['tmt_tests'] is False
    assert checks['tmt_test_/a'] is True
    assert checks['tmt_test_/b'] is False
    # Didn't run, nothing to check
    assert 'tmt_test_/c' not in checks


@pytest.mark.parametrize(
    'test, check, covered',
    [
        ('tmt_tests', 'tmt_tests', True),
        ('tmt_tests', 'tmt_test_/Sanity/foo', True),
        ('tmt_tests', 'third_party_test_foo', False),
        ('tmt_tests', 'install_package', False),
        ('tmt_test_/Sanity/foo', 'tmt_test_/Sanity/foo', True),
        ('tmt_test_/Sanity/foo', 'tmt_test_/Sanity/foo-bar', False),
        ('tmt_test_/Sanity/foo', 'tmt_tests', False),
        ('tmt_test_/Sanity/*', 'tmt_test_/Sanity/foo', True),
        ('tmt_test_/Sanity/*', 'tmt_test_/Regression/foo', False),
        ('tmt_test_*', 'tmt_tests', False),
        ('tmt_test_*', 'third_party_test_foo', False),
        ('third_party_tests', 'tmt_test_/Sanity/foo', False),
        ('third_party_test_*', 'tmt_test_/Sanity/foo', False),
    ],
)
async def test_selects_tmt(test: str, check: str, covered: bool):
    assert selects(test, check) is covered


class TestEvaluateTmtResult:
    async def test_passed(self):
        verdict = evaluate_test_result(
            tmt_result(tmt_test('/a'), tmt_test('/b', plan=OTHERS)),
            config_rules(),
        )
        assert not verdict.failed
        assert verdict.tmt == {'summary': {'pass': 2}}

    @pytest.mark.parametrize('plan', [TIER1, OTHERS, None])
    async def test_failure_in_any_plan(self, plan: typing.Optional[str]):
        # Every plan is required, also when ALTS didn't say which one ran
        verdict = evaluate_test_result(
            tmt_result(
                tmt_test('/a'),
                tmt_test('/b', 'fail', plan=plan),
                stage_success=False,
            ),
            config_rules(),
        )
        assert verdict.failed
        assert verdict.tmt == {'summary': {'pass': 1, 'fail': 1}}

    async def test_error(self):
        verdict = evaluate_test_result(
            tmt_result(tmt_test('/a', 'error', plan=OTHERS)),
            config_rules(),
        )
        assert verdict.failed

    async def test_without_config(self):
        for rules in (None, MatchedRules()):
            verdict = evaluate_test_result(
                tmt_result(tmt_test('/b', 'fail', plan=OTHERS)),
                rules,
            )
            assert verdict.failed
            assert verdict.tmt == {'summary': {'fail': 1}}

    async def test_warn_doesnt_fail(self):
        # D-20, even when ALTS fails the stage for it
        verdict = evaluate_test_result(
            tmt_result(tmt_test('/a', 'warn'), stage_success=False),
            config_rules(),
        )
        assert not verdict.failed
        assert verdict.tmt['summary'] == {'warn': 1}

    async def test_skip_outcome(self):
        verdict = evaluate_test_result(
            tmt_result(tmt_test('/a', 'skip'), tmt_test('/b')),
            config_rules(),
        )
        assert not verdict.failed
        assert verdict.tmt['summary'] == {'skip': 1, 'pass': 1}

    async def test_pending_fails(self):
        # The run broke before it got to the test
        verdict = evaluate_test_result(
            tmt_result(tmt_test('/a', 'pending')),
            config_rules(),
        )
        assert verdict.failed

    async def test_broken_stage(self):
        # E.g. the tests repository didn't clone, no test ran
        verdict = evaluate_test_result(
            tmt_result(stage_success=False),
            config_rules(),
        )
        assert verdict.failed
        assert verdict.tmt is None

    async def test_broken_stage_with_passed_tests(self):
        verdict = evaluate_test_result(
            tmt_result(tmt_test('/a'), stage_success=False),
            config_rules(),
        )
        assert verdict.failed

    async def test_success_only(self):
        # A result without outcomes and plans: success decides
        result = tmt_result({'success': False}, keys=['tmt_test_/a'])
        verdict = evaluate_test_result(result, config_rules())
        assert verdict.failed
        assert verdict.tmt['summary'] == {'fail': 1}

    @pytest.mark.parametrize('plan', [TIER1, OTHERS])
    async def test_expected_failure(self, plan: str):
        verdict = evaluate_test_result(
            tmt_result(
                tmt_test('/Sanity/bz2015182-x', 'fail', plan=plan),
                tmt_test('/Sanity/other'),
                stage_success=False,
            ),
            config_rules(
                expected_failures=[{
                    'tests': ['tmt_test_/Sanity/bz2015182-*'],
                    'reason': 'Known upstream bug',
                }],
            ),
        )
        assert not verdict.failed
        assert verdict.expected_failures == [{
            'test': 'tmt_test_/Sanity/bz2015182-x',
            'reason': 'Known upstream bug',
        }]
        assert verdict.unexpected_passes == []
        assert verdict.tmt == {'summary': {'fail': 1, 'pass': 1}}

    async def test_unexpected_failure_next_to_expected_one(self):
        verdict = evaluate_test_result(
            tmt_result(
                tmt_test('/a', 'fail'),
                tmt_test('/b', 'fail', plan=OTHERS),
            ),
            config_rules(
                expected_failures=[{'tests': ['tmt_test_/a'], 'reason': 'x'}],
            ),
        )
        assert verdict.failed
        assert verdict.expected_failures == [
            {'test': 'tmt_test_/a', 'reason': 'x'}
        ]

    async def test_expected_failures_of_the_whole_stage(self):
        verdict = evaluate_test_result(
            tmt_result(
                tmt_test('/a', 'fail'),
                tmt_test('/b'),
                stage_success=False,
            ),
            config_rules(
                expected_failures=[{
                    'tests': ['tmt_tests'],
                    'reason': 'Broken',
                }],
            ),
        )
        assert not verdict.failed
        assert verdict.expected_failures == [
            {'test': 'tmt_test_/a', 'reason': 'Broken'}
        ]
        assert verdict.unexpected_passes == ['tmt_test_/b']

    async def test_unexpected_pass(self):
        verdict = evaluate_test_result(
            tmt_result(tmt_test('/a')),
            config_rules(
                expected_failures=[{'tests': ['tmt_test_/a'], 'reason': 'x'}],
            ),
        )
        assert not verdict.failed
        assert verdict.unexpected_passes == ['tmt_test_/a']

    async def test_skipped_test(self):
        # ALTS ran it anyway, e.g. before it supported skipping
        verdict = evaluate_test_result(
            tmt_result(
                tmt_test('/Sanity/reboot', 'error'),
                stage_success=False,
            ),
            config_rules(
                skip=[{
                    'tests': ['tmt_test_/Sanity/reboot'],
                    'reason': 'container: calls reboot',
                }],
            ),
        )
        assert not verdict.failed
        assert verdict.skipped == [{
            'test': 'tmt_test_/Sanity/reboot',
            'reason': 'container: calls reboot',
        }]
        assert verdict.expected_failures == []

    async def test_other_failure_next_to_skipped_test(self):
        verdict = evaluate_test_result(
            tmt_result(
                tmt_test('/Sanity/reboot', 'error'),
                tmt_test('/Sanity/other', 'fail'),
            ),
            config_rules(
                skip=[{'tests': ['tmt_test_/Sanity/reboot'], 'reason': 'x'}],
            ),
        )
        assert verdict.failed

    async def test_test_in_two_plans(self):
        # ALTS keys the results apart, rules still name the test
        result = tmt_result(
            tmt_test('/Sanity/foo', 'pass', plan=TIER1),
            tmt_test('/Sanity/foo', 'fail', plan=OTHERS),
            keys=[
                f'tmt_test_/Sanity/foo@{TIER1}',
                f'tmt_test_/Sanity/foo@{OTHERS}',
            ],
        )
        verdict = evaluate_test_result(result, config_rules())
        assert verdict.failed
        verdict = evaluate_test_result(
            result,
            config_rules(
                expected_failures=[
                    {'tests': ['tmt_test_/Sanity/foo'], 'reason': 'Known'},
                ],
            ),
        )
        assert not verdict.failed
        assert verdict.expected_failures == [
            {'test': f'tmt_test_/Sanity/foo@{OTHERS}', 'reason': 'Known'},
        ]
        assert verdict.unexpected_passes == [f'tmt_test_/Sanity/foo@{TIER1}']

    async def test_other_stages_still_count(self):
        result = tmt_result(tmt_test('/a'))
        result['install_package'] = {'success': False}
        verdict = evaluate_test_result(result, config_rules())
        assert verdict.failed

    async def test_result_without_tmt(self):
        verdict = evaluate_test_result(passed_result(), config_rules())
        assert not verdict.failed
        assert verdict.tmt is None


class TestCheckConfigCommand:
    @pytest.fixture
    def write(self, tmp_path):
        def func(name: str, content: str) -> str:
            path = tmp_path / name
            path.write_text(content)
            return str(path)

        return func

    async def test_valid(self, write, capsys):
        paths = [
            write('grep.yaml', GREP_CONFIG_YAML),
            write('bind.yaml', BIND_CONFIG_YAML),
            write('systemd.yaml', SYSTEMD_CONFIG_YAML),
            write('empty.yaml', ''),
        ]
        assert check_package_tests_config.main(paths) == 0
        output = capsys.readouterr().out
        assert output.count(': OK') == 4
        assert '3 tmt plan(s)' in output
        assert 'gating' not in output

    async def test_invalid(self, write, capsys, tmp_path):
        paths = [
            write('grep.yaml', GREP_CONFIG_YAML),
            write(
                'typo.yaml',
                'tmt:\n  plans: [plans/tier1]\n  gatting: [/plans/x]\n',
            ),
            write('broken.yaml', 'tmt: [\n'),
            str(tmp_path / 'missing.yaml'),
        ]
        assert check_package_tests_config.main(paths) == 1
        output = capsys.readouterr().out
        assert 'grep.yaml: OK' in output
        assert 'typo.yaml: INVALID' in output
        assert 'tmt.plans[0].name: ' in output
        assert 'is not a tmt plan name' in output
        assert 'tmt.gatting: Extra inputs are not permitted' in output
        assert 'broken.yaml: INVALID' in output
        assert 'not valid YAML' in output
        assert 'missing.yaml: INVALID' in output
        assert 'cannot read' in output


class TestBuildTaskTestTask:
    """kind of test tasks in builds."""

    @staticmethod
    def shown(kind: str, alts_response: typing.Optional[dict]) -> dict:
        task = models.TestTask(
            id=1,
            status=3,
            revision=1,
            kind=kind,
            alts_response=alts_response,
            performance_stats=[],
        )
        return build_schema.BuildTaskTestTask.model_validate(task).model_dump()

    @pytest.mark.parametrize('kind', ['package', 'component'])
    def test_kind(self, kind: str):
        result = {'result': {}, 'tests_config': {'tmt': {'summary': {}}}}
        for alts_response in (None, result):
            assert self.shown(kind, alts_response) == {
                'id': 1,
                'status': 3,
                'revision': 1,
                'performance_stats': [],
                'kind': kind,
            }

    def test_old_schema_defaults(self):
        # A payload without kind, e.g. an old cached response
        shown = build_schema.BuildTaskTestTask(id=1, status=3, revision=1)
        assert shown.kind == 'package'
