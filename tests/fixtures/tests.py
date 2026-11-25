import base64
import typing

import pytest

from alws.pulp_models import RpmPackage
from alws.utils import package_tests
from alws.utils.parsing import parse_rpm_nevra


@pytest.fixture
def mock_get_pulp_packages(monkeypatch):
    def func(*args, **kwargs):
        result = {}
        artifacts, *_ = args
        for artifact in artifacts:
            nevra = parse_rpm_nevra(artifact.name)
            result[artifact.href] = RpmPackage(
                name=nevra.name,
                epoch=nevra.epoch,
                version=nevra.version,
                release=nevra.release,
                arch=nevra.arch,
            )
        return result

    monkeypatch.setattr('alws.crud.test.get_pulp_packages', func)


def install_failed_result() -> dict:
    """ALTS result of a package that couldn't be installed."""
    return {
        'logs': [],
        'skipped_tests': [],
        'system_info': {'success': True},
        'install_package': {'success': False},
        'stop_environment': {'success': True},
        'initial_provision': {'success': True},
        'start_environment': {'success': True},
        'initialize_terraform': {'success': True},
    }


def passed_result() -> dict:
    """ALTS result of a package that has passed all tests."""
    return {
        **install_failed_result(),
        'install_package': {'success': True},
        'uninstall_package': {'success': True},
        'third_party_tests_wrapper': {'success': True},
    }


def gitea_contents_entry(package_name: str, content: str) -> dict:
    """What the Gitea contents API answers for a package tests config."""
    return {
        'type': 'file',
        'encoding': 'base64',
        'content': base64.b64encode(content.encode()).decode(),
        'html_url': (
            f'https://git.almalinux.org/tests/{package_name}'
            '/src/branch/main/alts.yaml'
        ),
        'last_commit_sha': 'c0ffee',
    }


# Head of the tests branch of every package with a config in the fake Gitea
TESTS_COMMIT = '0123456789abcdef0123456789abcdef01234567'


class PackageTestsConfigs(dict):
    """Package tests configs a fake Gitea serves, by package name.

    A string is the content of the config, an exception is raised instead
    of answering, anything else is answered as is. The config is the same
    at every commit unless ``at_commit[(package, commit)]`` says otherwise.
    The head of the tests branch of a package with a config is
    ``heads.get(package, TESTS_COMMIT)``.
    """

    def __init__(self):
        super().__init__()
        # Packages Gitea has been asked about the config of, as
        # <package>@<commit> for a commit
        self.lookups = []
        self.at_commit = {}
        self.heads = {}

    def __setitem__(self, package_name: str, value: typing.Any):
        super().__setitem__(package_name, value)
        # Like a config pushed to git, but seen right away
        for key in list(package_tests._lookups_cache):
            if key == package_name or key.startswith(f'{package_name}@'):
                package_tests._lookups_cache.pop(key)


@pytest.fixture(autouse=True)
def package_tests_configs(
    monkeypatch,
) -> typing.Iterator[PackageTestsConfigs]:
    """Serve package tests configs from the yielded dict instead of Gitea.

    It's used by every test, so that none of them looks the configs up for
    real.
    """
    configs = PackageTestsConfigs()

    async def fetch_config_file(package_name: str, ref=None):
        configs.lookups.append(f'{package_name}@{ref}' if ref else package_name)
        if package_name not in configs:
            return None
        value = configs.at_commit.get(
            (package_name, ref), configs[package_name]
        )
        if isinstance(value, Exception):
            raise value
        if isinstance(value, str):
            return gitea_contents_entry(package_name, value)
        return value

    async def fetch_branch_commit(package_name: str):
        if package_name not in configs:
            return None
        head = configs.heads.get(package_name, TESTS_COMMIT)
        if isinstance(head, Exception):
            raise head
        return head

    monkeypatch.setattr(
        package_tests,
        '_fetch_config_file',
        fetch_config_file,
    )
    monkeypatch.setattr(
        package_tests,
        '_fetch_branch_commit',
        fetch_branch_commit,
    )
    package_tests._lookups_cache.clear()
    yield configs
    package_tests._lookups_cache.clear()
