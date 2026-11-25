"""Component test tasks, which run the tmt tests of tests/<package>.

``build_done`` builds chan from git.almalinux.org/rpms/chan.git for
AlmaLinux-8 i686 and x86_64. tmt is switched on for AlmaLinux-8 x86_64
here, so only the x86_64 build task gets a component test task.
"""

import typing

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alws import models
from alws.config import settings
from alws.constants import TestTaskKind
from alws.constants import TestTaskStatus as TaskStatus
from alws.crud.test import (
    complete_test_task,
    create_test_tasks_for_build_id,
    get_available_test_tasks,
    restart_build_tests,
)
from alws.schemas import test_schema
from tests.fixtures.tests import (
    TESTS_COMMIT,
    PackageTestsConfigs,
    passed_result,
)

pytestmark = pytest.mark.anyio

TIER1 = '/plans/tier1/public'
OTHERS = '/plans/others/public'
CHAN_TMT_CONFIG = """
tmt:
  plans:
    - /plans/tier1/public
    - name: /plans/others/public
      platforms: AlmaLinux-8
    - name: /plans/el9/public
      platforms: AlmaLinux-9
  context:
    trigger: build
skip:
  - tests: ['tmt_test_/Sanity/reboot']
    reason: 'container: calls reboot'
  - packages: chan
    arches: i686
    reason: chan isn't tested on i686
"""
CHAN_FLAKY_EXPECTED = """
expected_failures:
  - tests: ['tmt_test_/Other/flaky']
    reason: Flaky
"""
CHAN_PACKAGE_TESTS = {
    'url': 'https://git.almalinux.org/tests/chan.git',
    'commit': TESTS_COMMIT,
    'packages': ['chan-0.0.4-3.el8.x86_64'],
    'tmt': {
        'plans': [TIER1, OTHERS],
        'context': {'trigger': 'build', 'component': 'chan'},
        'environment': {},
    },
}


@pytest.fixture(autouse=True)
def tmt_enabled(monkeypatch):
    monkeypatch.setattr(settings, 'package_tests_tmt_enabled', True)
    monkeypatch.setattr(
        settings,
        'package_tests_tmt_platforms',
        ['AlmaLinux-8'],
    )
    monkeypatch.setattr(settings, 'package_tests_tmt_arches', ['x86_64'])


async def latest_test_tasks(
    session: AsyncSession,
    build: models.Build,
) -> typing.List[models.TestTask]:
    tasks = (
        (
            await session.execute(
                select(models.TestTask)
                .join(models.BuildTask)
                .where(models.BuildTask.build_id == build.id)
                .order_by(models.TestTask.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    revision = max(task.revision for task in tasks)
    return [task for task in tasks if task.revision == revision]


async def recreate_test_tasks(
    session: AsyncSession,
    build: models.Build,
) -> typing.List[models.TestTask]:
    """The next revision of the test tasks, as restarting tests does."""
    await create_test_tasks_for_build_id(session, build.id)
    await session.commit()
    return await latest_test_tasks(session, build)


def component_tasks(
    tasks: typing.List[models.TestTask],
) -> typing.List[models.TestTask]:
    return [task for task in tasks if task.kind == TestTaskKind.COMPONENT]


async def poll_payloads(session: AsyncSession) -> typing.Dict[int, dict]:
    """Payloads of all test tasks waiting for ALTS, by test task."""
    payloads = {}
    while True:
        batch = await get_available_test_tasks(session)
        await session.commit()
        if not batch:
            break
        payloads.update((payload['bs_task_id'], payload) for payload in batch)
    return payloads


def tmt_alts_result(
    *tests: typing.Tuple[str, str, str],
) -> test_schema.TestTaskResult:
    """What ALTS answers for a component test task.

    The tests are (name, plan, result), as ALTS reports them.
    """
    result = {
        key: value
        for key, value in passed_result().items()
        if key not in ('install_package', 'uninstall_package')
        and not key.startswith('third_party')
    }
    return test_schema.TestTaskResult(
        api_version='20210512',
        result={
            **result,
            'skipped_tests': ['tmt_test_/Sanity/reboot'],
            'tmt_tests': {'success': True, 'counts': {}},
            'tmt': {
                f'tmt_test_{name}': {
                    'success': outcome in ('pass', 'info', 'warn'),
                    'output': f'Result: {outcome}',
                    'name': name,
                    'plan': plan,
                    'result': outcome,
                    'duration': '00:00:01',
                    'note': '',
                }
                for name, plan, outcome in tests
            },
        },
        stats={},
    )


async def complete_component_task(
    session: AsyncSession,
    build: models.Build,
    *tests: typing.Tuple[str, str, str],
) -> models.TestTask:
    task = component_tasks(await latest_test_tasks(session, build))[0]
    await complete_test_task(session, task.id, tmt_alts_result(*tests))
    await session.commit()
    return await session.get(models.TestTask, task.id, populate_existing=True)


async def test_component_test_task(
    async_session: AsyncSession,
    regular_build: models.Build,
    build_done,
    package_tests_configs: PackageTestsConfigs,
):
    package_tests_configs['chan'] = CHAN_TMT_CONFIG
    tasks = await recreate_test_tasks(async_session, regular_build)
    # The package task of x86_64 and the component one; chan isn't tested
    # on i686 and tmt isn't switched on there
    assert [
        (task.kind, task.package_name, task.env_arch) for task in tasks
    ] == [
        ('package', 'chan', 'x86_64'),
        ('component', 'chan', 'x86_64'),
    ]
    component = tasks[1]
    assert component.status == TaskStatus.CREATED
    # Named after the source package
    assert (component.package_version, component.package_release) == (
        '0.0.4',
        '3.el8',
    )
    assert component.skipped_tests == ['tmt_test_/Sanity/reboot']
    assert component.package_tests == CHAN_PACKAGE_TESTS
    # The config is read at the very commit that goes to ALTS
    assert f'chan@{TESTS_COMMIT}' in package_tests_configs.lookups
    assert tasks[0].package_tests is None


async def test_component_task_payload(
    async_session: AsyncSession,
    regular_build: models.Build,
    build_done,
    package_tests_configs: PackageTestsConfigs,
):
    package_tests_configs['chan'] = CHAN_TMT_CONFIG
    package, component = await recreate_test_tasks(
        async_session,
        regular_build,
    )
    payloads = await poll_payloads(async_session)
    payload = payloads[component.id]
    assert payload['package_name'] == 'chan'
    assert payload['package_version'] == '0.0.4-3.el8'
    assert payload['dist_arch'] == 'x86_64'
    assert payload['skipped_tests'] == ['tmt_test_/Sanity/reboot']
    assert payload['package_tests'] == CHAN_PACKAGE_TESTS
    assert 'test_configuration' not in payload
    # Package test tasks don't run tmt
    assert 'package_tests' not in payloads[package.id]


@pytest.mark.parametrize(
    'config',
    [
        pytest.param(None, id='no-config'),
        pytest.param(
            'skip: [{tests: [uninstall_package], reason: x}]',
            id='no-tmt',
        ),
        pytest.param(
            'tmt: {plans: [{name: /plans/a, platforms: AlmaLinux-9}]}',
            id='no-plans-for-the-platform',
        ),
        pytest.param(
            'tmt: {plans: [/plans/a]}\n'
            'skip: [{tests: [tmt_tests], arches: x86_64, reason: x}]',
            id='tmt-skipped',
        ),
        pytest.param(
            'tmt: {plans: [/plans/a]}\nskip: [{reason: not tested}]',
            id='package-skipped',
        ),
        pytest.param(
            'tmt: {plans: [/plans/a]}\n'
            'skip: [{packages: chan, tests: [tmt_tests], reason: x}]',
            id='tmt-skipped-for-the-source-package',
        ),
        pytest.param('tmt: [', id='invalid-config'),
    ],
)
async def test_no_component_test_task(
    async_session: AsyncSession,
    regular_build: models.Build,
    build_done,
    package_tests_configs: PackageTestsConfigs,
    config: typing.Optional[str],
):
    if config is not None:
        package_tests_configs['chan'] = config
    tasks = await recreate_test_tasks(async_session, regular_build)
    assert not component_tasks(tasks)


def set_default_tmt_settings(monkeypatch):
    """What a deployment without PACKAGE_TESTS_TMT_* gets."""
    for name in (
        'package_tests_tmt_enabled',
        'package_tests_tmt_platforms',
        'package_tests_tmt_arches',
    ):
        monkeypatch.setattr(
            settings,
            name,
            type(settings).model_fields[name].get_default(),
        )


@pytest.mark.parametrize('has_tests_repository', [True, False])
async def test_default_tmt_settings(
    async_session: AsyncSession,
    regular_build: models.Build,
    build_done,
    package_tests_configs: PackageTestsConfigs,
    monkeypatch,
    has_tests_repository: bool,
):
    set_default_tmt_settings(monkeypatch)
    if has_tests_repository:
        package_tests_configs['chan'] = 'tmt: {plans: [/plans/a]}'
    tasks = await recreate_test_tasks(async_session, regular_build)
    # Only x86_64: tmt isn't run on i686, and a package without
    # tests/<package> has no component test task at all
    assert [task.env_arch for task in component_tasks(tasks)] == (
        ['x86_64'] if has_tests_repository else []
    )


async def test_tmt_settings_leave_package_test_tasks_alone(
    async_session: AsyncSession,
    regular_build: models.Build,
    build_done,
    package_tests_configs: PackageTestsConfigs,
    monkeypatch,
):
    package_tests_configs['chan'] = 'tmt: {plans: [/plans/a]}'

    async def package_tasks() -> typing.List[tuple]:
        await recreate_test_tasks(async_session, regular_build)
        payloads = await poll_payloads(async_session)
        tasks = await latest_test_tasks(async_session, regular_build)
        return [
            (
                task.package_name,
                task.env_arch,
                task.status,
                task.skipped_tests,
                task.package_tests,
                {
                    key: value
                    for key, value in payloads[task.id].items()
                    if key not in ('bs_task_id', 'callback_href')
                },
            )
            for task in tasks
            if task.kind == TestTaskKind.PACKAGE
        ]

    monkeypatch.setattr(settings, 'package_tests_tmt_enabled', False)
    without_tmt = await package_tasks()
    # Package test tasks on every arch, i686 included
    assert [(name, arch) for name, arch, *_ in without_tmt] == [
        ('chan', 'i686'),
        ('chan', 'x86_64'),
    ]
    # All of them sent to ALTS
    assert {status for _, _, status, *_ in without_tmt} == {TaskStatus.STARTED}
    set_default_tmt_settings(monkeypatch)
    assert await package_tasks() == without_tmt
    monkeypatch.setattr(settings, 'package_tests_tmt_platforms', ['*'])
    monkeypatch.setattr(settings, 'package_tests_tmt_arches', ['*'])
    assert await package_tasks() == without_tmt


async def test_rule_for_binary_package_does_not_skip_component(
    async_session: AsyncSession,
    regular_build: models.Build,
    build_done,
    package_tests_configs: PackageTestsConfigs,
):
    # CFG-4: a rule with packages applies to the component test task only
    # when it names the source package
    package_tests_configs['chan'] = (
        'tmt: {plans: [/plans/a]}\n' 'skip: [{packages: chan-devel, reason: x}]'
    )
    tasks = await recreate_test_tasks(async_session, regular_build)
    assert len(component_tasks(tasks)) == 1


async def test_no_component_test_task_when_tmt_is_off(
    async_session: AsyncSession,
    regular_build: models.Build,
    build_done,
    package_tests_configs: PackageTestsConfigs,
    monkeypatch,
):
    monkeypatch.setattr(settings, 'package_tests_tmt_enabled', False)
    package_tests_configs['chan'] = CHAN_TMT_CONFIG
    tasks = await recreate_test_tasks(async_session, regular_build)
    assert not component_tasks(tasks)
    # Not even a look at the tests commit
    assert not any('@' in lookup for lookup in package_tests_configs.lookups)
    payloads = await poll_payloads(async_session)
    assert all('package_tests' not in payload for payload in payloads.values())


async def test_tests_branch_unavailable(
    async_session: AsyncSession,
    regular_build: models.Build,
    build_done,
    package_tests_configs: PackageTestsConfigs,
):
    package_tests_configs['chan'] = CHAN_TMT_CONFIG
    package_tests_configs.heads['chan'] = RuntimeError('Gitea is down')
    tasks = await recreate_test_tasks(async_session, regular_build)
    # The packages are tested anyway
    assert [(task.kind, task.package_name) for task in tasks] == [
        ('package', 'chan')
    ]


async def test_tmt_switched_off_before_dispatch(
    async_session: AsyncSession,
    regular_build: models.Build,
    build_done,
    package_tests_configs: PackageTestsConfigs,
    monkeypatch,
):
    package_tests_configs['chan'] = CHAN_TMT_CONFIG
    package, component = await recreate_test_tasks(
        async_session,
        regular_build,
    )
    monkeypatch.setattr(settings, 'package_tests_tmt_enabled', False)
    payloads = await poll_payloads(async_session)
    assert component.id not in payloads
    assert package.id in payloads
    await async_session.refresh(component)
    assert component.status == TaskStatus.CANCELLED


async def test_all_plans_passed(
    async_session: AsyncSession,
    regular_build: models.Build,
    build_done,
    package_tests_configs: PackageTestsConfigs,
):
    package_tests_configs['chan'] = CHAN_TMT_CONFIG
    await recreate_test_tasks(async_session, regular_build)
    task = await complete_component_task(
        async_session,
        regular_build,
        ('/Sanity/basic', TIER1, 'pass'),
        ('/Sanity/slow', TIER1, 'warn'),
        ('/Other/basic', OTHERS, 'pass'),
    )
    assert task.status == TaskStatus.COMPLETED
    tests_config = task.alts_response['tests_config']
    assert tests_config['tmt'] == {'summary': {'pass': 2, 'warn': 1}}
    assert tests_config['skipped'] == [
        {'test': 'tmt_test_/Sanity/reboot', 'reason': 'container: calls reboot'}
    ]


@pytest.mark.parametrize('plan', [TIER1, OTHERS])
async def test_failure_in_any_plan(
    async_session: AsyncSession,
    regular_build: models.Build,
    build_done,
    package_tests_configs: PackageTestsConfigs,
    plan: str,
):
    # Every plan in alts.yaml is required
    package_tests_configs['chan'] = CHAN_TMT_CONFIG
    await recreate_test_tasks(async_session, regular_build)
    task = await complete_component_task(
        async_session,
        regular_build,
        ('/Sanity/basic', TIER1, 'pass'),
        ('/Other/flaky', plan, 'fail'),
    )
    assert task.status == TaskStatus.FAILED
    assert task.alts_response['tests_config']['tmt'] == {
        'summary': {'fail': 1, 'pass': 1},
    }


async def test_expected_failure(
    async_session: AsyncSession,
    regular_build: models.Build,
    build_done,
    package_tests_configs: PackageTestsConfigs,
):
    package_tests_configs['chan'] = CHAN_TMT_CONFIG + CHAN_FLAKY_EXPECTED
    await recreate_test_tasks(async_session, regular_build)
    task = await complete_component_task(
        async_session,
        regular_build,
        ('/Sanity/basic', TIER1, 'pass'),
        ('/Other/flaky', OTHERS, 'fail'),
    )
    assert task.status == TaskStatus.COMPLETED
    assert task.alts_response['tests_config']['expected_failures'] == [
        {'test': 'tmt_test_/Other/flaky', 'reason': 'Flaky'}
    ]


async def test_judged_by_config_at_the_tested_commit(
    async_session: AsyncSession,
    regular_build: models.Build,
    build_done,
    package_tests_configs: PackageTestsConfigs,
):
    package_tests_configs['chan'] = CHAN_TMT_CONFIG + CHAN_FLAKY_EXPECTED
    await recreate_test_tasks(async_session, regular_build)
    # The tests branch moves on while ALTS tests the old commit: the rule
    # that expects the failure is gone at the head
    package_tests_configs.at_commit[('chan', TESTS_COMMIT)] = (
        CHAN_TMT_CONFIG + CHAN_FLAKY_EXPECTED
    )
    package_tests_configs['chan'] = CHAN_TMT_CONFIG
    task = await complete_component_task(
        async_session,
        regular_build,
        ('/Other/flaky', OTHERS, 'fail'),
    )
    assert task.status == TaskStatus.COMPLETED
    tests_config = task.alts_response['tests_config']
    assert tests_config['expected_failures'] == [
        {'test': 'tmt_test_/Other/flaky', 'reason': 'Flaky'}
    ]


async def test_restart_only_failed_tests(
    async_session: AsyncSession,
    regular_build: models.Build,
    build_done,
    package_tests_configs: PackageTestsConfigs,
):
    package_tests_configs['chan'] = CHAN_TMT_CONFIG
    package, component = await recreate_test_tasks(
        async_session,
        regular_build,
    )
    await complete_component_task(
        async_session,
        regular_build,
        ('/Sanity/basic', TIER1, 'pass'),
    )
    await complete_test_task(
        async_session,
        package.id,
        test_schema.TestTaskResult(
            api_version='20210512',
            result={**passed_result(), 'install_package': {'success': False}},
            stats={},
        ),
    )
    await async_session.commit()
    # The tests branch has moved on, the passed component task keeps its
    # commit and result
    package_tests_configs.heads['chan'] = 'f' * 40
    await restart_build_tests(async_session, regular_build.id, only_failed=True)
    await async_session.commit()
    tasks = await latest_test_tasks(async_session, regular_build)
    by_kind = {task.kind: task for task in tasks if task.env_arch == 'x86_64'}
    assert by_kind['package'].status == TaskStatus.CREATED
    assert by_kind['component'].status == TaskStatus.COMPLETED
    assert by_kind['component'].id != component.id
    assert by_kind['component'].package_tests['commit'] == TESTS_COMMIT


async def test_restart_failed_component_task(
    async_session: AsyncSession,
    regular_build: models.Build,
    build_done,
    package_tests_configs: PackageTestsConfigs,
):
    package_tests_configs['chan'] = CHAN_TMT_CONFIG
    await recreate_test_tasks(async_session, regular_build)
    await complete_component_task(
        async_session,
        regular_build,
        ('/Sanity/basic', TIER1, 'fail'),
    )
    new_head = 'f' * 40
    package_tests_configs.heads['chan'] = new_head
    await restart_build_tests(async_session, regular_build.id, only_failed=True)
    await async_session.commit()
    tasks = await latest_test_tasks(async_session, regular_build)
    (component,) = component_tasks(tasks)
    assert component.status == TaskStatus.CREATED
    # A new run tests the head of the tests branch
    assert component.package_tests['commit'] == new_head
