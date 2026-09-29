"""Render an offline portal preview using only non-secret catalogue variables."""
import argparse
import shutil
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).resolve().parents[1]


def render_portal(output: Path):
    context = {}
    for relative in ('roles/application_portal/defaults/main.yml',
                     'inventories/local/group_vars/server.yml'):
        context.update(yaml.safe_load((ROOT / relative).read_text(encoding='utf-8')))
    env = Environment(undefined=StrictUndefined, autoescape=False)

    def resolve(value):
        if isinstance(value, str):
            for _ in range(5):
                if '{{' not in value:
                    return value
                value = env.from_string(value).render(context)
            raise ValueError('Unresolved catalogue value')
        if isinstance(value, dict):
            return {key: resolve(item) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        return value

    # Do not expand unrelated server variables (many are runtime secret lookups).
    public = {key: resolve(value) for key, value in context.items()
              if key.startswith('application_portal_')}
    env.loader = FileSystemLoader(ROOT / 'roles/application_portal/templates')
    output.mkdir(parents=True, exist_ok=True)
    (output / 'index.html').write_text(env.get_template('index.html.j2').render(public), encoding='utf-8')
    shutil.copytree(ROOT / 'roles/application_portal/files/assets', output / 'assets', dirs_exist_ok=True)
    return public


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    catalogue = render_portal(args.output)
    print(f"Rendered {len(catalogue['application_portal_apps'])} applications to {args.output}")
