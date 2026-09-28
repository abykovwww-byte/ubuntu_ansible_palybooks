#!/usr/bin/env python3
"""Render the deploy template and validate it without starting containers."""
import argparse
from pathlib import Path
import subprocess

from jinja2 import Environment, StrictUndefined
import yaml

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, help='Save rendered Compose instead of invoking Docker')
    args = parser.parse_args()
    for relative in ('roles/obsidian/tasks/main.yml', 'playbooks/obsidian.yml', 'playbooks/site.yml'):
        yaml.safe_load((ROOT / relative).read_text(encoding='utf-8'))
    variables = yaml.safe_load((ROOT / 'roles/obsidian/defaults/main.yml').read_text(encoding='utf-8'))
    environment = Environment(undefined=StrictUndefined)
    for _ in range(3):
        variables = {k: environment.from_string(v).render(variables) if isinstance(v, str) else v
                     for k, v in variables.items()}
    compose = environment.from_string((ROOT / 'roles/obsidian/templates/compose.yml.j2').read_text(encoding='utf-8')).render(variables)
    yaml.safe_load(compose)
    if args.output:
        args.output.write_text(compose, encoding='utf-8')
    else:
        subprocess.run(['docker', 'compose', '--project-name', 'obsidian-validation', '-f', '-', 'config', '--quiet'],
                       input=compose.encode('utf-8'), check=True)
    print('Obsidian template validation passed')


if __name__ == '__main__':
    main()
