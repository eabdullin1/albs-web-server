"""Schema of the per-package tests config kept in git.

Every package can have a repository named like its git.almalinux.org/rpms
one in the tests organization (git.almalinux.org/tests/<package>). Its
config file (see ``settings.package_tests_config_path``) tells ALBS which
ALTS tests not to run for the package and which of them are expected to
fail, and which tmt plans of the repository ALTS runs. A failure in any
of those plans fails the test task.
"""

import dataclasses
import fnmatch
import re
import typing

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
from typing_extensions import Annotated

__all__ = [
    'PACKAGE_TESTS',
    'THIRD_PARTY_TEST_PREFIX',
    'TMT_RESERVED_CONTEXT',
    'TMT_TESTS',
    'TMT_TEST_PREFIX',
    'ExpectedFailureRule',
    'MatchedRules',
    'PackageTestsConfig',
    'SkipRule',
    'TmtConfig',
    'TmtPlan',
]


# The stage of ALTS that runs the tmt plans of the tests repository
TMT_TESTS = 'tmt_tests'
# Tests that ALTS runs for a package, named as in its results
PACKAGE_TESTS = (
    'install_package',
    'package_integrity_tests',
    'third_party_tests',
    TMT_TESTS,
    'uninstall_package',
)
# Third-party tests are also addressed one by one, as ALTS names them:
# third_party_test_<test file>
THIRD_PARTY_TEST_PREFIX = 'third_party_test_'
# And so are tmt tests: tmt_test_<test name>, the name as in results.yaml
# of tmt, e.g. tmt_test_/Sanity/basic-functionality
TMT_TEST_PREFIX = 'tmt_test_'
# tmt context dimensions that ALTS and ALBS set themselves, a config
# can't override them
TMT_RESERVED_CONTEXT = ('arch', 'component', 'distro', 'initiator')
# What tmt itself accepts as an environment variable name
_ENVIRONMENT_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')


def _has_prefix(name: str, prefix: str) -> bool:
    return name.startswith(prefix) and len(name) > len(prefix)


def _check_test_name(name: str) -> str:
    if (
        name not in PACKAGE_TESTS
        and not _has_prefix(name, THIRD_PARTY_TEST_PREFIX)
        and not _has_prefix(name, TMT_TEST_PREFIX)
    ):
        raise ValueError(
            f'unknown test {name!r}, expected one of'
            f' {", ".join(PACKAGE_TESTS)}, {THIRD_PARTY_TEST_PREFIX}<file>'
            f' or {TMT_TEST_PREFIX}<tmt test name>'
        )
    return name


PackageTestName = Annotated[str, AfterValidator(_check_test_name)]


def _matches_any(value: str, patterns: typing.Optional[typing.List[str]]):
    # A filter that isn't set matches everything
    if patterns is None:
        return True
    return any(
        fnmatch.fnmatchcase(value.lower(), pattern.lower())
        for pattern in patterns
    )


def _single_value_to_list(value):
    if isinstance(value, str):
        return [value]
    return value


class Rule(BaseModel):
    """Test tasks a rule applies to and why.

    ``packages``, ``platforms`` and ``arches`` are lists of shell-style
    patterns, matched case-insensitively against the binary package name,
    the ALBS platform name (e.g. AlmaLinux-9) and the build architecture.
    An omitted filter matches everything, so a rule without ``packages``
    covers all binary packages built from the source package.
    """

    # Unknown keys are typos far more often than features from the future,
    # so they invalidate the config instead of being silently ignored
    model_config = ConfigDict(extra='forbid')

    packages: typing.Optional[typing.List[str]] = Field(None, min_length=1)
    platforms: typing.Optional[typing.List[str]] = Field(None, min_length=1)
    arches: typing.Optional[typing.List[str]] = Field(None, min_length=1)
    reason: str

    @field_validator(
        'tests',
        'packages',
        'platforms',
        'arches',
        mode='before',
        check_fields=False,
    )
    @classmethod
    def single_value_to_list(cls, value):
        return _single_value_to_list(value)

    @field_validator('reason')
    @classmethod
    def reason_is_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError('reason should explain the rule')
        return value

    def matches(self, package_name: str, platform_name: str, arch: str):
        return (
            _matches_any(package_name, self.packages)
            and _matches_any(platform_name, self.platforms)
            and _matches_any(arch, self.arches)
        )


class SkipRule(Rule):
    """Tests that aren't run, so their results never fail the test task.

    Without ``tests`` the packages aren't tested at all.
    """

    tests: typing.Optional[typing.List[PackageTestName]] = Field(
        None,
        min_length=1,
    )


class ExpectedFailureRule(Rule):
    """Tests that run, but their failures don't fail the test task."""

    tests: typing.List[PackageTestName] = Field(min_length=1)


@dataclasses.dataclass(frozen=True)
class MatchedRules:
    """Rules of a config that apply to one test task."""

    skip: typing.List[SkipRule] = dataclasses.field(default_factory=list)
    expected_failures: typing.List[ExpectedFailureRule] = dataclasses.field(
        default_factory=list
    )

    @property
    def skipped_package(self) -> typing.Optional[SkipRule]:
        """The rule that switches all tests of the package off, if any."""
        return next((rule for rule in self.skip if rule.tests is None), None)

    @property
    def skipped_tests(self) -> typing.List[str]:
        return list(
            dict.fromkeys(
                test for rule in self.skip for test in rule.tests or ()
            )
        )


def _to_string_values(value):
    """Let numbers in YAML stand for strings, but not booleans.

    ``TIMEOUT: 30`` means "30", while ``yes`` would become "True", which is
    hardly what the author meant.
    """
    if not isinstance(value, dict):
        return value
    converted = {}
    for key, item in value.items():
        if isinstance(item, bool):
            raise ValueError(f'quote the value of {key!r}: {item!r}')
        if isinstance(item, (int, float)):
            item = str(item)
        converted[key] = item
    return converted


class TmtPlan(BaseModel):
    """A tmt plan to run, on the platforms and arches given.

    A plain string in ``plans`` is a plan for all platforms and arches.
    ``platforms`` and ``arches`` are shell-style patterns, matched as in
    the rules.
    """

    model_config = ConfigDict(extra='forbid')

    name: str
    platforms: typing.Optional[typing.List[str]] = Field(None, min_length=1)
    arches: typing.Optional[typing.List[str]] = Field(None, min_length=1)

    @field_validator('platforms', 'arches', mode='before')
    @classmethod
    def single_value_to_list(cls, value):
        return _single_value_to_list(value)

    @field_validator('name')
    @classmethod
    def name_is_plan(cls, value: str) -> str:
        if not value.startswith('/') or value.rstrip('/') != value:
            raise ValueError(
                f'{value!r} is not a tmt plan name, they look like'
                ' /plans/tier1/public'
            )
        if any(char.isspace() for char in value):
            raise ValueError(f'tmt plan name {value!r} has spaces')
        return value

    def matches(self, platform_name: str, arch: str) -> bool:
        return _matches_any(platform_name, self.platforms) and _matches_any(
            arch, self.arches
        )


class TmtConfig(BaseModel):
    """tmt plans of the tests repository that ALTS runs.

    Every plan is required: a failure in any of them fails the test task,
    unless the config expects it. ``filter``, ``context`` and
    ``environment`` are passed to tmt as they are; ``context`` is added to
    the one ALTS sets.
    """

    model_config = ConfigDict(extra='forbid')

    plans: typing.List[TmtPlan] = []
    filter: typing.Optional[str] = None
    context: typing.Dict[str, str] = {}
    environment: typing.Dict[str, str] = {}

    @field_validator('plans', mode='before')
    @classmethod
    def plan_names_to_plans(cls, value):
        value = _single_value_to_list(value)
        if not isinstance(value, list):
            return value
        return [
            {'name': plan} if isinstance(plan, str) else plan for plan in value
        ]

    @model_validator(mode='before')
    @classmethod
    def no_gating(cls, value):
        # gating of the first drafts of the format: there are no plans
        # that don't gate any more, so it would only mislead
        if isinstance(value, dict) and 'gating' in value:
            raise ValueError(
                'gating is not supported: every plan in plans is required,'
                ' list only the plans that must pass'
            )
        return value

    @field_validator('filter')
    @classmethod
    def filter_is_not_blank(cls, value: typing.Optional[str]):
        if value is not None and not value.strip():
            raise ValueError('filter is empty, remove it instead')
        return value

    @field_validator('context', 'environment', mode='before')
    @classmethod
    def numbers_to_strings(cls, value):
        return _to_string_values(value)

    @field_validator('context')
    @classmethod
    def context_isnt_reserved(cls, value: typing.Dict[str, str]):
        for key in value:
            if key in TMT_RESERVED_CONTEXT:
                raise ValueError(
                    f'context {key!r} is set by ALTS and ALBS,'
                    f' {", ".join(TMT_RESERVED_CONTEXT)} can\'t be changed'
                )
            if not key or '=' in key or any(c.isspace() for c in key):
                raise ValueError(f'{key!r} is not a tmt context dimension')
        return value

    @field_validator('environment')
    @classmethod
    def environment_names_are_valid(cls, value: typing.Dict[str, str]):
        for key in value:
            if not _ENVIRONMENT_NAME.fullmatch(key):
                raise ValueError(f'{key!r} is not an environment variable')
        return value

    def get_plans(self, platform_name: str, arch: str) -> typing.List[str]:
        """Names of the plans to run on the platform and arch, in order."""
        return list(
            dict.fromkeys(
                plan.name
                for plan in self.plans
                if plan.matches(platform_name, arch)
            )
        )


class PackageTestsConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')

    version: typing.Literal[1] = 1
    tmt: typing.Optional[TmtConfig] = None
    skip: typing.List[SkipRule] = []
    expected_failures: typing.List[ExpectedFailureRule] = []

    def get_rules(
        self,
        package_name: str,
        platform_name: str,
        arch: str,
    ) -> MatchedRules:
        def matching(rules: typing.List[Rule]) -> typing.List[Rule]:
            return [
                rule
                for rule in rules
                if rule.matches(package_name, platform_name, arch)
            ]

        return MatchedRules(
            skip=matching(self.skip),
            expected_failures=matching(self.expected_failures),
        )
