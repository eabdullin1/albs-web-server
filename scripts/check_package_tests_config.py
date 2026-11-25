"""Check package tests configs (alts.yaml) the way ALBS reads them.

    python3 scripts/check_package_tests_config.py [alts.yaml ...]

Validates every file with the same model ALBS uses and prints what's wrong
in a readable form. Exits with 1 when any file is invalid, so that it can
guard pushes and pull requests to git.almalinux.org/tests/<package>. Needs
only pydantic and PyYAML, not a configured ALBS.
"""

import argparse
import os
import sys
import typing

import yaml
from pydantic import ValidationError

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alws.schemas.package_tests_schema import PackageTestsConfig  # noqa: E402


def parse_args(args: typing.Optional[typing.List[str]] = None):
    parser = argparse.ArgumentParser(
        description='Check package tests configs (alts.yaml) as ALBS does',
    )
    parser.add_argument(
        'files',
        nargs='*',
        default=['alts.yaml'],
        help='configs to check, alts.yaml of the current directory by default',
    )
    return parser.parse_args(args)


def _location(loc: typing.Tuple[typing.Union[str, int], ...]) -> str:
    """tmt.plans.0.name -> tmt.plans[0].name"""
    location = ''
    for part in loc:
        if isinstance(part, int):
            location += f'[{part}]'
        else:
            location += f'.{part}' if location else str(part)
    return location or '(top level)'


def format_errors(error: ValidationError) -> typing.List[str]:
    lines = []
    for item in error.errors(include_url=False):
        # Model validators prefix their messages with "Value error, "
        message = item['msg'].removeprefix('Value error, ')
        lines.append(f'{_location(item["loc"])}: {message}')
    return lines


def describe(config: PackageTestsConfig) -> str:
    parts = [
        f'{len(config.skip)} skip rule(s)',
        f'{len(config.expected_failures)} expected failure rule(s)',
    ]
    if config.tmt:
        parts.append(f'{len(config.tmt.plans)} tmt plan(s)')
    return ', '.join(parts)


def check_file(path: str) -> typing.Tuple[bool, typing.List[str]]:
    """Whether the config is valid, and what to say about it."""
    try:
        with open(path, encoding='utf-8') as file:
            content = yaml.safe_load(file)
    except OSError as exc:
        return False, [f'cannot read: {exc}']
    except yaml.YAMLError as exc:
        return False, [f'not valid YAML: {exc}']
    try:
        # ALBS reads an empty file as an empty config
        config = PackageTestsConfig.model_validate(content or {})
    except ValidationError as exc:
        return False, format_errors(exc)
    return True, [describe(config)]


def main(args: typing.Optional[typing.List[str]] = None) -> int:
    options = parse_args(args)
    all_valid = True
    for path in options.files:
        valid, lines = check_file(path)
        all_valid = all_valid and valid
        print(f'{path}: {"OK" if valid else "INVALID"}')
        for line in lines:
            print(f'  {line}')
    return 0 if all_valid else 1


if __name__ == '__main__':
    sys.exit(main())
