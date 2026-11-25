"""The tmt tests of tests/<package> through the API, as ALTS sees it.

The scenarios of the end-to-end check of ALBS -> ALTS -> ALBS: ALTS takes
the test tasks with ``GET /api/v1/tests/get_test_tasks/`` and posts their
results to ``POST /api/v1/tests/<id>/result/``. Gitea is faked by the
``package_tests_configs`` fixture, ALTS by the payloads and results it
really sent in that check (``tests/samples/package_tests``, see the README
there). They were captured for a build of grep on AlmaLinux-9; here
``build_done`` builds chan on AlmaLinux-8, which only changes names.
"""

import json
import re
import typing
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from alws import dramatiq, models
from alws.config import settings
from alws.constants import BuildTaskStatus, TestTaskKind
from alws.constants import TestTaskStatus as TaskStatus
from alws.crud import test as test_crud
from alws.crud.build import get_builds
from alws.crud.test import complete_test_task, create_test_tasks_for_build_id
from alws.dramatiq.build import _build_done
from alws.schemas.build_node_schema import BuildDone
from alws.schemas.test_schema import TestTaskResult
from alws.utils.pulp_client import PulpClient
from tests.fixtures.dramatiq import prepare_build_done_payload
from tests.fixtures.tests import TESTS_COMMIT, PackageTestsConfigs
from tests.mock_classes import BaseAsyncTestCase

SAMPLES = Path(__file__).parents[1] / 'samples' / 'package_tests'
ALTS_FORMAT = SAMPLES / 'alts_format'
TIER1 = '/plans/tier1/public'
TIER23 = '/plans/tier2-tier3/public'
E2E_GATING = '/plans/e2e/gating'
E2E_INFO = '/plans/e2e/info'


def sample(name: str) -> typing.Any:
    return json.loads((SAMPLES / name).read_text())


def alts_format(name: str) -> typing.Any:
    """A fixture of the ALTS side (alts_format/README.md)."""
    return json.loads((ALTS_FORMAT / name).read_text())


def alts_result(name: str) -> dict:
    """An ALTS result fixture as ALTS posts it to ALBS."""
    return {'api_version': '20210512', 'result': alts_format(name), 'stats': {}}


# alts.yaml giving the plans of alts_format/e2e_plans_payload.json
E2E_PLANS_CONFIG = """
tmt:
  plans: [/plans/e2e/gating, /plans/e2e/info, /plans/e2e/internal]
  context:
    trigger: build
skip:
  - tests: ['tmt_test_/e2e/skipme']
    reason: 'e2e: must not be run'
"""


def sample_text(name: str) -> str:
    return (SAMPLES / name).read_text()


# The e2e copy of tests/grep has /e2e/fail, which fails, in /plans/e2e/info
E2E_FAIL_EXPECTED = """expected_failures:
  - tests: ['tmt_test_/e2e/fail']
    reason: 'e2e: fails on purpose'
"""


def only_plans(result: dict, plans: typing.List[str]) -> dict:
    """The result as if ALTS had run only these plans."""
    result = json.loads(json.dumps(result))
    result['result']['tmt'] = {
        key: test
        for key, test in result['result']['tmt'].items()
        if test['plan'] in plans
    }
    return result


GREP_FAILURES = [
    'tmt_test_/Regression/bz1050916-malformed-output-when-coloring-is-on',
    'tmt_test_/Regression/inconsistent-w-and-alnum-behaviour',
]
GREP_FAILURES_EXPECTED = """
expected_failures:
  - tests:
      - tmt_test_/Regression/bz1050916-malformed-output-when-coloring-is-on
      - tmt_test_/Regression/inconsistent-w-and-alnum-*
    reason: Fail in a container without a UTF-8 locale
"""
# alts.yaml of tests/grep that runs only tier1
GREP_TIER1_CONFIG = """version: 1
tmt:
  plans:
    - /plans/tier1/public
"""


# What ALTS (its scheduler) posts for a task it has cancelled before
# it ran, see post_revoked_test_task_result of alts/scheduler/app.py
REVOKED_RESULT = {
    'api_version': '20210512',
    'result': {'revoked': True},
    'stats': {},
}


@pytest.fixture
async def build_done_with_debug(
    async_session: AsyncSession,
    regular_build: models.Build,
    start_build,
    create_entity,
    get_rpm_packages_info,
    mock_get_pulp_packages,
    get_packages_info_from_pulp,
):
    """build_done, but the build tasks have debug packages too, as grep."""
    build = await get_builds(db=async_session, build_id=regular_build.id)
    await async_session.close()
    for build_task in build.tasks:
        await _build_done(
            BuildDone(
                **prepare_build_done_payload(
                    build_task.id,
                    [
                        'chan-0.0.4-3.el8.src.rpm',
                        f'chan-0.0.4-3.el8.{build_task.arch}.rpm',
                        f'chan-debuginfo-0.0.4-3.el8.{build_task.arch}.rpm',
                        f'chan-debugsource-0.0.4-3.el8.{build_task.arch}.rpm',
                    ],
                )
            )
        )
        await async_session.commit()
    build = await get_builds(db=async_session, build_id=regular_build.id)
    for build_task in build.tasks:
        assert build_task.status == BuildTaskStatus.COMPLETED
    await async_session.close()


def captured_payload(name: str, kind: str) -> dict:
    """A payload ALBS really sent to ALTS, of a package or component task."""
    payloads = sample(name)
    component = [p for p in payloads if p['package_tests']]
    packages = [p for p in payloads if not p['package_tests']]
    return (component if kind == 'component' else packages)[0]


class TestPackageTestsFlow(BaseAsyncTestCase):
    @pytest.fixture(autouse=True)
    def tmt_enabled(self, monkeypatch):
        monkeypatch.setattr(settings, 'package_tests_tmt_enabled', True)
        monkeypatch.setattr(
            settings,
            'package_tests_tmt_platforms',
            ['AlmaLinux-8'],
        )
        monkeypatch.setattr(settings, 'package_tests_tmt_arches', ['x86_64'])

    @pytest.fixture(autouse=True)
    def pulp_logs(self, monkeypatch):
        """Pulp takes the logs ALTS uploaded, as it does for real."""

        async def create_file(self, file_name: str, artifact_href: str):
            return f'/pulp/api/v3/content/file/files/{uuid.uuid4()}/'

        async def modify_repository(self, *args, **kwargs):
            return {}

        monkeypatch.setattr(PulpClient, 'create_file', create_file)
        monkeypatch.setattr(PulpClient, 'modify_repository', modify_repository)

    async def recreate_test_tasks(
        self,
        session: AsyncSession,
        build: models.Build,
    ) -> typing.Dict[str, models.TestTask]:
        """Test tasks of the next revision by (kind, package, arch)."""
        await create_test_tasks_for_build_id(session, build.id)
        await session.commit()
        tasks = (
            (
                await session.execute(
                    select(models.TestTask)
                    .join(models.BuildTask)
                    .where(models.BuildTask.build_id == build.id)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        revision = max(task.revision for task in tasks)
        return {
            f'{task.kind}:{task.package_name}:{task.env_arch}': task
            for task in tasks
            if task.revision == revision
        }

    async def take_test_tasks(self) -> typing.Dict[int, dict]:
        """Payloads of all waiting test tasks, as the ALTS scheduler does."""
        payloads = {}
        while True:
            response = await self.make_request(
                'get',
                '/api/v1/tests/get_test_tasks/',
            )
            assert response.status_code == 200, response.text
            if not response.json():
                return payloads
            payloads.update(
                (payload['bs_task_id'], payload) for payload in response.json()
            )

    async def post_result(
        self,
        session: AsyncSession,
        monkeypatch,
        task_id: int,
        result: dict,
    ) -> models.TestTask:
        """Post the result as ALTS does and process it as dramatiq does."""
        sent = []
        monkeypatch.setattr(
            dramatiq.tests.complete_test_task,
            'send',
            lambda *args: sent.append(args),
        )
        response = await self.make_request(
            'post',
            f'/api/v1/tests/{task_id}/result/',
            json=result,
        )
        assert response.status_code == 200, response.text
        ((sent_task_id, payload),) = sent
        await complete_test_task(
            session,
            sent_task_id,
            TestTaskResult(**payload),
        )
        await session.commit()
        return (
            (
                await session.execute(
                    select(models.TestTask)
                    .where(models.TestTask.id == task_id)
                    .options(selectinload(models.TestTask.artifacts))
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .one()
        )

    async def test_component_test_task_payload(
        self,
        async_session: AsyncSession,
        regular_build: models.Build,
        build_done,
        package_tests_configs: PackageTestsConfigs,
    ):
        package_tests_configs['chan'] = sample_text('grep_alts.yaml')
        tasks = await self.recreate_test_tasks(async_session, regular_build)
        assert sorted(tasks) == [
            'component:chan:x86_64',
            'package:chan:i686',
            'package:chan:x86_64',
        ]
        payloads = await self.take_test_tasks()
        component = payloads[tasks['component:chan:x86_64'].id]
        captured = captured_payload('payloads_grep.json', 'component')
        # The same fields as ALTS got in the real run
        assert sorted(component) == sorted(captured)
        assert sorted(component['package_tests']) == sorted(
            captured['package_tests']
        )
        assert component['package_tests'] == {
            'url': 'https://git.almalinux.org/tests/chan.git',
            'commit': TESTS_COMMIT,
            'packages': ['chan-0.0.4-3.el8.x86_64'],
            'tmt': {
                # The plans of the real tests/grep
                'plans': captured['package_tests']['tmt']['plans'],
                'context': {'component': 'chan'},
                'environment': {},
            },
        }
        assert component['package_name'] == 'chan'
        assert component['package_version'] == '0.0.4-3.el8'
        assert component['test_configuration'] is None
        assert component['skipped_tests'] is None

    async def test_package_test_tasks_payload_unchanged(
        self,
        async_session: AsyncSession,
        regular_build: models.Build,
        build_done,
        package_tests_configs: PackageTestsConfigs,
    ):
        package_tests_configs['chan'] = sample_text('grep_alts.yaml')
        tasks = await self.recreate_test_tasks(async_session, regular_build)
        payloads = await self.take_test_tasks()
        captured = captured_payload('payloads_grep.json', 'package')
        for key in ('package:chan:x86_64', 'package:chan:i686'):
            payload = payloads[tasks[key].id]
            assert sorted(payload) == sorted(captured)
            assert payload['package_tests'] is None
            assert payload['package_name'] == 'chan'
            assert payload['skipped_tests'] is None

    async def test_real_component_result_failure(
        self,
        async_session: AsyncSession,
        regular_build: models.Build,
        build_done,
        package_tests_configs: PackageTestsConfigs,
        monkeypatch,
    ):
        package_tests_configs['chan'] = sample_text('grep_alts.yaml')
        tasks = await self.recreate_test_tasks(async_session, regular_build)
        await self.take_test_tasks()
        task = await self.post_result(
            async_session,
            monkeypatch,
            tasks['component:chan:x86_64'].id,
            sample('result_grep_component.json'),
        )
        # Two tests of tier2-tier3 fail, and every plan of alts.yaml is
        # required. tmt itself exited with 1 for them, the stage is still a
        # success: it ran all the plans; the tests decide
        stage = sample('result_grep_component.json')['result']['tmt_tests']
        assert (stage['success'], stage['tmt_exit_code']) == (True, 1)
        assert task.status == TaskStatus.FAILED
        tests_config = task.alts_response['tests_config']
        assert tests_config['tmt'] == {'summary': {'pass': 25, 'fail': 2}}
        # Judged by alts.yaml at the commit ALTS tested
        assert tests_config['commit'] == 'c0ffee'
        assert f'chan@{TESTS_COMMIT}' in package_tests_configs.lookups
        # The result itself is kept as ALTS sent it, with all its logs
        assert task.alts_response['result'] == (
            sample('result_grep_component.json')['result']
        )
        assert len(task.artifacts) == 34

    async def test_real_component_result_passed(
        self,
        async_session: AsyncSession,
        regular_build: models.Build,
        build_done,
        package_tests_configs: PackageTestsConfigs,
        monkeypatch,
    ):
        # alts.yaml lists only tier1, so ALTS runs only it
        package_tests_configs['chan'] = (
            f'version: 1\ntmt:\n  plans:\n    - {TIER1}\n'
        )
        tasks = await self.recreate_test_tasks(async_session, regular_build)
        payloads = await self.take_test_tasks()
        component = tasks['component:chan:x86_64']
        assert payloads[component.id]['package_tests']['tmt']['plans'] == [
            TIER1
        ]
        task = await self.post_result(
            async_session,
            monkeypatch,
            component.id,
            only_plans(sample('result_grep_component.json'), [TIER1]),
        )
        assert task.status == TaskStatus.COMPLETED
        assert task.alts_response['tests_config']['tmt'] == {
            'summary': {'pass': 6}
        }

    async def test_skip_rule_leaves_tmt_test_out(
        self,
        async_session: AsyncSession,
        regular_build: models.Build,
        build_done,
        package_tests_configs: PackageTestsConfigs,
        monkeypatch,
    ):
        # The failure of /e2e/fail is expected, the task passes
        package_tests_configs['chan'] = (
            sample_text('grep_e2e_alts.yaml') + E2E_FAIL_EXPECTED
        )
        tasks = await self.recreate_test_tasks(async_session, regular_build)
        component = tasks['component:chan:x86_64']
        payloads = await self.take_test_tasks()
        payload = payloads[component.id]
        captured = captured_payload('payloads_e2e.json', 'component')
        # ALTS gets the test to leave out of the tmt run, as in the real run
        assert (
            payload['skipped_tests']
            == captured['skipped_tests']
            == ['tmt_test_/e2e/skipme']
        )
        assert payload['package_tests']['tmt']['plans'] == [
            E2E_GATING,
            E2E_INFO,
        ]
        result = sample('result_e2e_component.json')
        # ALTS didn't run it: it would fail
        assert 'tmt_test_/e2e/skipme' not in result['result']['tmt']
        assert result['result']['skipped_tests'] == ['tmt_test_/e2e/skipme']
        task = await self.post_result(
            async_session,
            monkeypatch,
            component.id,
            result,
        )
        assert task.status == TaskStatus.COMPLETED
        tests_config = task.alts_response['tests_config']
        assert tests_config['skipped'] == [{
            'test': 'tmt_test_/e2e/skipme',
            'reason': 'e2e: must not be run',
        }]
        assert tests_config['expected_failures'] == [{
            'test': 'tmt_test_/e2e/fail',
            'reason': 'e2e: fails on purpose',
        }]
        assert tests_config['tmt'] == {'summary': {'pass': 1, 'fail': 1}}

    async def test_skipped_tmt_test_would_fail_the_task(
        self,
        async_session: AsyncSession,
        regular_build: models.Build,
        build_done,
        package_tests_configs: PackageTestsConfigs,
        monkeypatch,
    ):
        # Without the skip rule ALTS would run the test, and its failure
        # would fail the task
        result = sample('result_e2e_component.json')
        result['result']['skipped_tests'] = []
        result['result']['tmt']['tmt_test_/e2e/skipme'] = dict(
            result['result']['tmt']['tmt_test_/e2e/fail'],
            name='/e2e/skipme',
            plan=E2E_GATING,
        )
        package_tests_configs['chan'] = (
            sample_text('grep_e2e_alts.yaml').replace(
                "skip:\n  - tests: ['tmt_test_/e2e/skipme']\n"
                "    reason: 'e2e: must not be run'\n",
                'skip:\n',
            )
            + E2E_FAIL_EXPECTED
        )
        tasks = await self.recreate_test_tasks(async_session, regular_build)
        await self.take_test_tasks()
        task = await self.post_result(
            async_session,
            monkeypatch,
            tasks['component:chan:x86_64'].id,
            result,
        )
        assert task.status == TaskStatus.FAILED

    @pytest.mark.parametrize('plan', [E2E_GATING, E2E_INFO])
    async def test_failure_in_any_plan(
        self,
        async_session: AsyncSession,
        regular_build: models.Build,
        build_done,
        package_tests_configs: PackageTestsConfigs,
        monkeypatch,
        plan: str,
    ):
        # Every plan of alts.yaml is required, whichever /e2e/fail is in
        package_tests_configs['chan'] = sample_text('grep_e2e_alts.yaml')
        result = sample('result_e2e_component.json')
        result['result']['tmt']['tmt_test_/e2e/fail']['plan'] = plan
        tasks = await self.recreate_test_tasks(async_session, regular_build)
        await self.take_test_tasks()
        task = await self.post_result(
            async_session,
            monkeypatch,
            tasks['component:chan:x86_64'].id,
            result,
        )
        assert task.status == TaskStatus.FAILED
        tests_config = task.alts_response['tests_config']
        assert tests_config['tmt'] == {'summary': {'pass': 1, 'fail': 1}}
        assert 'expected_failures' not in tests_config

    async def test_judged_by_config_of_the_tested_commit(
        self,
        async_session: AsyncSession,
        regular_build: models.Build,
        build_done,
        package_tests_configs: PackageTestsConfigs,
        monkeypatch,
    ):
        # The failure is expected at the commit ALTS got; while it tests,
        # main moves on to a commit without the rule
        config = sample_text('grep_e2e_alts.yaml')
        at_tested_commit = config + E2E_FAIL_EXPECTED
        package_tests_configs['chan'] = at_tested_commit
        tasks = await self.recreate_test_tasks(async_session, regular_build)
        await self.take_test_tasks()
        package_tests_configs.at_commit[('chan', TESTS_COMMIT)] = (
            at_tested_commit
        )
        package_tests_configs['chan'] = config
        task = await self.post_result(
            async_session,
            monkeypatch,
            tasks['component:chan:x86_64'].id,
            sample('result_e2e_component.json'),
        )
        assert task.status == TaskStatus.COMPLETED
        assert task.alts_response['tests_config']['expected_failures'] == [
            {'test': 'tmt_test_/e2e/fail', 'reason': 'e2e: fails on purpose'}
        ]

    async def test_tmt_switched_off_cancels_waiting_component_task(
        self,
        async_session: AsyncSession,
        regular_build: models.Build,
        build_done,
        package_tests_configs: PackageTestsConfigs,
        monkeypatch,
    ):
        package_tests_configs['chan'] = sample_text('grep_alts.yaml')
        tasks = await self.recreate_test_tasks(async_session, regular_build)
        monkeypatch.setattr(settings, 'package_tests_tmt_enabled', False)
        payloads = await self.take_test_tasks()
        component = tasks['component:chan:x86_64']
        assert component.id not in payloads
        assert tasks['package:chan:x86_64'].id in payloads
        await async_session.refresh(component)
        assert component.status == TaskStatus.CANCELLED
        # And no new component test task while it's off
        tasks = await self.recreate_test_tasks(async_session, regular_build)
        assert not [
            task
            for task in tasks.values()
            if task.kind == TestTaskKind.COMPONENT
        ]

    @pytest.mark.parametrize(
        'config',
        [
            pytest.param(None, id='no-config'),
            pytest.param('grep_alts.yaml', id='config-with-tmt'),
        ],
    )
    async def test_real_package_result_unchanged(
        self,
        async_session: AsyncSession,
        regular_build: models.Build,
        build_done,
        package_tests_configs: PackageTestsConfigs,
        monkeypatch,
        config: typing.Optional[str],
    ):
        if config:
            package_tests_configs['chan'] = sample_text(config)
        tasks = await self.recreate_test_tasks(async_session, regular_build)
        await self.take_test_tasks()
        result = sample('result_grep_package.json')
        task = await self.post_result(
            async_session,
            monkeypatch,
            tasks['package:chan:x86_64'].id,
            result,
        )
        # The package integrity tests failed in the real run, as for
        # every package on that stand: the docs aren't installed there
        assert not result['result']['tests']['package_integrity_tests'][
            'success'
        ]
        assert task.status == TaskStatus.FAILED
        assert task.alts_response['result'] == result['result']
        tests_config = task.alts_response.get('tests_config')
        if config:
            assert 'tmt' not in tests_config
        else:
            assert tests_config is None

    async def test_debug_packages_are_not_installed_for_tmt(
        self,
        async_session: AsyncSession,
        regular_build: models.Build,
        build_done_with_debug,
        package_tests_configs: PackageTestsConfigs,
    ):
        package_tests_configs['chan'] = sample_text('grep_alts.yaml')
        tasks = await self.recreate_test_tasks(async_session, regular_build)
        # Debug packages are tested one by one as before, like the
        # grep-debuginfo and grep-debugsource tasks of the real run
        assert sorted(key for key in tasks if key.endswith('x86_64')) == [
            'component:chan:x86_64',
            'package:chan-debuginfo:x86_64',
            'package:chan-debugsource:x86_64',
            'package:chan:x86_64',
        ]
        payloads = await self.take_test_tasks()
        component = payloads[tasks['component:chan:x86_64'].id]
        captured = captured_payload('payloads_grep.json', 'component')
        assert captured['package_tests']['packages'] == [
            'grep-3.6-5.el9.x86_64'
        ]
        assert component['package_tests']['packages'] == [
            'chan-0.0.4-3.el8.x86_64'
        ]

    async def test_latest_results_api_shows_component_task(
        self,
        async_session: AsyncSession,
        regular_build: models.Build,
        build_done,
        package_tests_configs: PackageTestsConfigs,
        monkeypatch,
    ):
        package_tests_configs['chan'] = sample_text('grep_alts.yaml')
        tasks = await self.recreate_test_tasks(async_session, regular_build)
        component = tasks['component:chan:x86_64']
        await self.take_test_tasks()
        await self.post_result(
            async_session,
            monkeypatch,
            component.id,
            sample('result_grep_component.json'),
        )
        response = await self.make_request(
            'get',
            f'/api/v1/tests/{component.build_task_id}/latest',
        )
        assert response.status_code == 200, response.text
        by_id = {task['id']: task for task in response.json()}
        shown = by_id[component.id]
        assert shown['kind'] == 'component'
        assert shown['package_name'] == 'chan'
        assert shown['status'] == TaskStatus.FAILED
        assert shown['package_tests']['commit'] == TESTS_COMMIT
        assert shown['package_tests']['tmt']['plans'] == [
            TIER1,
            TIER23,
            '/plans/others/public',
        ]
        assert shown['alts_response']['tests_config']['tmt']['summary'] == {
            'pass': 25,
            'fail': 2,
        }
        package = by_id[tasks['package:chan:x86_64'].id]
        assert package['kind'] == 'package'
        assert package['package_tests'] is None

    async def test_revision_results_api_shows_component_task(
        self,
        async_session: AsyncSession,
        regular_build: models.Build,
        build_done,
        package_tests_configs: PackageTestsConfigs,
        monkeypatch,
    ):
        # build_done tested chan without a config (revision 1), the
        # tmt section comes before the tests are restarted (a later one)
        package_tests_configs['chan'] = sample_text('grep_alts.yaml')
        tasks = await self.recreate_test_tasks(async_session, regular_build)
        component = tasks['component:chan:x86_64']
        await self.take_test_tasks()
        await self.post_result(
            async_session,
            monkeypatch,
            component.id,
            sample('result_grep_component.json'),
        )
        build_task_id = component.build_task_id

        first = await self.make_request(
            'get',
            f'/api/v1/tests/{build_task_id}/1',
        )
        assert first.status_code == 200, first.text
        assert [
            (task['kind'], task['package_tests']) for task in first.json()
        ] == [('package', None)]

        second = await self.make_request(
            'get',
            f'/api/v1/tests/{build_task_id}/{component.revision}',
        )
        assert second.status_code == 200, second.text
        by_kind = {task['kind']: task for task in second.json()}
        assert sorted(by_kind) == ['component', 'package']
        shown = by_kind['component']
        assert shown['id'] == component.id
        assert shown['revision'] == component.revision > 1
        assert shown['package_tests']['commit'] == TESTS_COMMIT
        assert shown['package_tests']['packages'] == ['chan-0.0.4-3.el8.x86_64']
        assert 'gating' not in shown['package_tests']['tmt']
        assert shown['alts_response']['tests_config']['tmt'] == {
            'summary': {'pass': 25, 'fail': 2}
        }
        assert by_kind['package']['package_tests'] is None

    async def test_restart_with_tmt_off_creates_no_component_task(
        self,
        async_session: AsyncSession,
        regular_build: models.Build,
        build_done,
        package_tests_configs: PackageTestsConfigs,
        monkeypatch,
    ):
        package_tests_configs['chan'] = sample_text('grep_alts.yaml')
        tasks = await self.recreate_test_tasks(async_session, regular_build)
        build_task_id = tasks['component:chan:x86_64'].build_task_id
        monkeypatch.setattr(settings, 'package_tests_tmt_enabled', False)
        response = await self.make_request(
            'put',
            f'/api/v1/tests/build_task/{build_task_id}/restart',
        )
        assert response.status_code == 200, response.text
        await async_session.commit()
        latest = await self.make_request(
            'get',
            f'/api/v1/tests/{build_task_id}/latest',
        )
        assert [task['kind'] for task in latest.json()] == ['package']

    async def test_tmt_expected_failures(
        self,
        async_session: AsyncSession,
        regular_build: models.Build,
        build_done,
        package_tests_configs: PackageTestsConfigs,
        monkeypatch,
    ):
        # The two real failures, in tier2-tier3, are expected
        package_tests_configs['chan'] = (
            sample_text('grep_alts.yaml')
            + """
expected_failures:
  - tests:
      - tmt_test_/Regression/bz1050916-malformed-output-when-coloring-is-on
      - tmt_test_/Regression/inconsistent-w-and-alnum-*
    reason: Fail in a container without a UTF-8 locale
  - tests: ['tmt_test_/Sanity/smoke-test']
    reason: An obsolete rule, the test passes
"""
        )
        tasks = await self.recreate_test_tasks(async_session, regular_build)
        await self.take_test_tasks()
        task = await self.post_result(
            async_session,
            monkeypatch,
            tasks['component:chan:x86_64'].id,
            sample('result_grep_component.json'),
        )
        assert task.status == TaskStatus.COMPLETED
        tests_config = task.alts_response['tests_config']
        reason = 'Fail in a container without a UTF-8 locale'
        assert sorted(
            failure['test'] for failure in tests_config['expected_failures']
        ) == [
            'tmt_test_/Regression/'
            'bz1050916-malformed-output-when-coloring-is-on',
            'tmt_test_/Regression/inconsistent-w-and-alnum-behaviour',
        ]
        assert {
            failure['reason'] for failure in tests_config['expected_failures']
        } == {reason}
        assert tests_config['unexpected_passes'] == [
            'tmt_test_/Sanity/smoke-test'
        ]
        assert tests_config['tmt'] == {'summary': {'pass': 25, 'fail': 2}}

    async def test_restart_only_failed_keeps_passed_component_task(
        self,
        async_session: AsyncSession,
        regular_build: models.Build,
        build_done,
        package_tests_configs: PackageTestsConfigs,
        monkeypatch,
    ):
        package_tests_configs['chan'] = (
            f'version: 1\ntmt:\n  plans:\n    - {TIER1}\n'
        )
        tasks = await self.recreate_test_tasks(async_session, regular_build)
        component = tasks['component:chan:x86_64']
        await self.take_test_tasks()
        # Real results: the component task passes (tier1, the only plan),
        # the package one fails
        await self.post_result(
            async_session,
            monkeypatch,
            component.id,
            only_plans(sample('result_grep_component.json'), [TIER1]),
        )
        await self.post_result(
            async_session,
            monkeypatch,
            tasks['package:chan:x86_64'].id,
            sample('result_grep_package.json'),
        )
        # main of the tests repository moves on meanwhile
        package_tests_configs.heads['chan'] = 'f' * 40
        response = await self.make_request(
            'put',
            f'/api/v1/tests/build_task/{component.build_task_id}/restart'
            '?only_failed=true',
        )
        assert response.status_code == 200, response.text
        await async_session.commit()
        latest = await self.make_request(
            'get',
            f'/api/v1/tests/{component.build_task_id}/latest',
        )
        by_kind = {task['kind']: task for task in latest.json()}
        assert by_kind['package']['status'] == TaskStatus.CREATED
        # The passed component task is carried over with its commit
        assert by_kind['component']['id'] != component.id
        assert by_kind['component']['status'] == TaskStatus.COMPLETED
        assert by_kind['component']['package_tests']['commit'] == (TESTS_COMMIT)
        payloads = await self.take_test_tasks()
        assert by_kind['component']['id'] not in payloads
        assert by_kind['package']['id'] in payloads

    async def test_cancel_dispatched_component_task(
        self,
        async_session: AsyncSession,
        regular_build: models.Build,
        build_done,
        package_tests_configs: PackageTestsConfigs,
        monkeypatch,
    ):
        package_tests_configs['chan'] = sample_text('grep_alts.yaml')
        tasks = await self.recreate_test_tasks(async_session, regular_build)
        component = tasks['component:chan:x86_64']
        # ALTS has taken it
        await self.take_test_tasks()
        cancelled = []

        async def cancel_tasks(self, tasks_ids):
            cancelled.extend(tasks_ids)
            return {'success': True}

        monkeypatch.setattr(test_crud.AltsClient, 'cancel_tasks', cancel_tasks)
        response = await self.make_request(
            'put',
            f'/api/v1/tests/build/{regular_build.id}/cancel',
        )
        assert response.status_code == 200, response.text
        # ALBS asks ALTS to cancel it as any started test task
        assert component.id in cancelled
        # and ALTS answers that it is revoked
        task = await self.post_result(
            async_session,
            monkeypatch,
            component.id,
            REVOKED_RESULT,
        )
        assert task.status == TaskStatus.CANCELLED
        assert 'tests_config' not in task.alts_response

    # The fixtures of the ALTS side (alts_format/README.md)

    async def test_payload_has_the_alts_format(
        self,
        async_session: AsyncSession,
        regular_build: models.Build,
        build_done,
        package_tests_configs: PackageTestsConfigs,
    ):
        # The config of the ALTS fixture: the plans of tests/grep and
        # trigger in the context
        package_tests_configs['chan'] = sample_text('grep_alts.yaml') + (
            '  context:\n    trigger: build\n'
        )
        tasks = await self.recreate_test_tasks(async_session, regular_build)
        payloads = await self.take_test_tasks()
        payload = payloads[tasks['component:chan:x86_64'].id]
        expected = alts_format('grep_al9_payload.json')
        # Nothing ALTS doesn't know: its model would drop it silently
        assert set(payload) <= set(expected)
        assert sorted(payload['package_tests']) == sorted(
            expected['package_tests']
        )
        assert sorted(payload['package_tests']['tmt']) == sorted(
            expected['package_tests']['tmt']
        )
        assert payload['package_tests']['tmt']['plans'] == (
            expected['package_tests']['tmt']['plans']
        )
        assert payload['package_tests']['tmt']['context'] == dict(
            expected['package_tests']['tmt']['context'],
            component='chan',
        )
        for packages in (
            payload['package_tests']['packages'],
            expected['package_tests']['packages'],
        ):
            assert all(
                re.fullmatch(r'\S+-[^-]+-[^-]+\.\w+', p) for p in packages
            )
        assert re.fullmatch(r'[0-9a-f]{40}', payload['package_tests']['commit'])

    @pytest.mark.parametrize(
        'config, plans, status, summary',
        [
            pytest.param(
                'grep_alts.yaml',
                None,
                TaskStatus.FAILED,
                {'pass': 25, 'fail': 2},
                id='failures-in-tier2-tier3',
            ),
            pytest.param(
                'expected',
                None,
                TaskStatus.COMPLETED,
                {'pass': 25, 'fail': 2},
                id='failures-expected',
            ),
            pytest.param(
                GREP_TIER1_CONFIG,
                [TIER1],
                TaskStatus.COMPLETED,
                {'pass': 6},
                id='tier1-only',
            ),
        ],
    )
    async def test_alts_expected_result(
        self,
        async_session: AsyncSession,
        regular_build: models.Build,
        build_done,
        package_tests_configs: PackageTestsConfigs,
        monkeypatch,
        config: str,
        plans: typing.Optional[typing.List[str]],
        status: TaskStatus,
        summary: dict,
    ):
        if config == 'grep_alts.yaml':
            config = sample_text(config)
        elif config == 'expected':
            config = sample_text('grep_alts.yaml') + GREP_FAILURES_EXPECTED
        package_tests_configs['chan'] = config
        tasks = await self.recreate_test_tasks(async_session, regular_build)
        await self.take_test_tasks()
        result = alts_result('grep_al9_expected_result.json')
        stage = result['result']['tmt_tests']
        assert (stage['success'], stage['tmt_exit_code']) == (True, 1)
        if plans:
            # ALTS runs only the plans of alts.yaml
            result = only_plans(result, plans)
        task = await self.post_result(
            async_session,
            monkeypatch,
            tasks['component:chan:x86_64'].id,
            result,
        )
        assert task.status == status
        tests_config = task.alts_response['tests_config']
        assert tests_config['tmt'] == {'summary': summary}
        assert sorted(
            failure['test']
            for failure in tests_config.get('expected_failures', [])
        ) == (
            GREP_FAILURES
            if status == TaskStatus.COMPLETED and not plans
            else []
        )

    @pytest.mark.parametrize(
        'rules, status',
        [
            pytest.param('', TaskStatus.FAILED, id='failure'),
            pytest.param(
                E2E_FAIL_EXPECTED, TaskStatus.COMPLETED, id='failure-expected'
            ),
        ],
    )
    async def test_alts_result_with_plan_keys(
        self,
        async_session: AsyncSession,
        regular_build: models.Build,
        build_done,
        package_tests_configs: PackageTestsConfigs,
        monkeypatch,
        rules: str,
        status: TaskStatus,
    ):
        package_tests_configs['chan'] = E2E_PLANS_CONFIG + rules
        tasks = await self.recreate_test_tasks(async_session, regular_build)
        payloads = await self.take_test_tasks()
        payload = payloads[tasks['component:chan:x86_64'].id]
        expected = alts_format('e2e_plans_payload.json')
        # ALBS sends the internal plan too, ALTS leaves it out
        assert payload['skipped_tests'] == expected['skipped_tests']
        assert payload['package_tests']['tmt']['plans'] == (
            expected['package_tests']['tmt']['plans']
        )
        result = alts_result('e2e_plans_result.json')
        # /e2e/pass ran in both plans, ALTS keys it with the plan
        assert sorted(result['result']['tmt']) == [
            'tmt_test_/e2e/fail',
            f'tmt_test_/e2e/pass@{E2E_GATING}',
            f'tmt_test_/e2e/pass@{E2E_INFO}',
            'tmt_test_/e2e/warn',
        ]
        task = await self.post_result(
            async_session,
            monkeypatch,
            tasks['component:chan:x86_64'].id,
            result,
        )
        assert task.status == status
        tests_config = task.alts_response['tests_config']
        # warn doesn't fail, skip isn't a result
        assert tests_config['tmt'] == {
            'summary': {'pass': 2, 'fail': 1, 'warn': 1}
        }
        assert tests_config['skipped'] == [{
            'test': 'tmt_test_/e2e/skipme',
            'reason': 'e2e: must not be run',
        }]

    async def test_rule_by_test_name_covers_plan_keys(
        self,
        async_session: AsyncSession,
        regular_build: models.Build,
        build_done,
        package_tests_configs: PackageTestsConfigs,
        monkeypatch,
    ):
        # Rules name the test, not the key ALTS gave it in each plan
        package_tests_configs['chan'] = E2E_PLANS_CONFIG + (
            'expected_failures:\n'
            "  - tests: ['tmt_test_/e2e/pass', 'tmt_test_/e2e/fail']\n"
            '    reason: Known\n'
        )
        tasks = await self.recreate_test_tasks(async_session, regular_build)
        await self.take_test_tasks()
        task = await self.post_result(
            async_session,
            monkeypatch,
            tasks['component:chan:x86_64'].id,
            alts_result('e2e_plans_result.json'),
        )
        assert task.status == TaskStatus.COMPLETED
        tests_config = task.alts_response['tests_config']
        assert tests_config['expected_failures'] == [
            {'test': 'tmt_test_/e2e/fail', 'reason': 'Known'}
        ]
        assert sorted(tests_config['unexpected_passes']) == [
            f'tmt_test_/e2e/pass@{E2E_GATING}',
            f'tmt_test_/e2e/pass@{E2E_INFO}',
        ]

    @pytest.mark.parametrize(
        'config, plans, status',
        [
            pytest.param(
                GREP_TIER1_CONFIG, [TIER1], TaskStatus.COMPLETED, id='passed'
            ),
            pytest.param(None, None, TaskStatus.FAILED, id='failed'),
        ],
    )
    async def test_build_api_shows_component_task(
        self,
        async_session: AsyncSession,
        regular_build: models.Build,
        build_done,
        package_tests_configs: PackageTestsConfigs,
        monkeypatch,
        config: typing.Optional[str],
        plans: typing.Optional[typing.List[str]],
        status: TaskStatus,
    ):
        # The build page tells the component test task from the others
        package_tests_configs['chan'] = config or sample_text('grep_alts.yaml')
        tasks = await self.recreate_test_tasks(async_session, regular_build)
        await self.take_test_tasks()
        component = tasks['component:chan:x86_64']
        package = tasks['package:chan:x86_64']
        result = alts_result('grep_al9_expected_result.json')
        await self.post_result(
            async_session,
            monkeypatch,
            component.id,
            only_plans(result, plans) if plans else result,
        )
        await self.post_result(
            async_session,
            monkeypatch,
            package.id,
            sample('result_grep_package.json'),
        )
        response = await self.make_request(
            'get',
            f'/api/v1/builds/{regular_build.id}/',
        )
        assert response.status_code == 200, response.text
        listed = await self.make_request(
            'get',
            '/api/v1/builds/?pageNumber=1',
        )
        assert listed.status_code == 200, listed.text
        (listed_build,) = [
            build
            for build in listed.json()['builds']
            if build['id'] == regular_build.id
        ]
        for build in (response.json(), listed_build):
            shown = {
                test_task['id']: test_task
                for build_task in build['tasks']
                for test_task in build_task['test_tasks']
            }
            for test_task in shown.values():
                assert sorted(test_task) == [
                    'id',
                    'kind',
                    'performance_stats',
                    'revision',
                    'status',
                ]
            assert shown[component.id]['kind'] == 'component'
            assert shown[component.id]['status'] == status
            # The other test tasks, with a result or without
            assert {
                test_task['kind']
                for test_task in shown.values()
                if test_task['id'] != component.id
            } == {'package'}
            assert shown[package.id]['status'] == TaskStatus.FAILED
            waiting = tasks['package:chan:i686']
            assert shown[waiting.id]['status'] == TaskStatus.STARTED

    async def test_build_api_component_task_without_result(
        self,
        async_session: AsyncSession,
        regular_build: models.Build,
        build_done,
        package_tests_configs: PackageTestsConfigs,
    ):
        package_tests_configs['chan'] = sample_text('grep_alts.yaml')
        tasks = await self.recreate_test_tasks(async_session, regular_build)
        component = tasks['component:chan:x86_64']
        response = await self.make_request(
            'get',
            f'/api/v1/builds/{regular_build.id}/',
        )
        assert response.status_code == 200, response.text
        (shown,) = [
            test_task
            for build_task in response.json()['tasks']
            for test_task in build_task['test_tasks']
            if test_task['id'] == component.id
        ]
        assert shown['kind'] == 'component'
