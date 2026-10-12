"""Offline YAML/Jinja render validation; synthetic image is never deployed."""
import json
from pathlib import Path
import yaml
from jinja2 import Environment, StrictUndefined

root = Path(__file__).resolve().parents[1]
role = root / 'roles/ngfw_metrics'
for folder in (role, root / 'roles/ngfw_native_source', root / 'roles/ngfw_native_route'):
    for path in folder.rglob('*.yml'):
        yaml.safe_load(path.read_text(encoding='utf-8'))
for name in ('site', 'ngfw-metrics', 'ngfw-native-discovery', 'ngfw-native-route', 'ngfw-extreme-endpoints'):
    yaml.safe_load((root / f'playbooks/{name}.yml').read_text(encoding='utf-8'))
values = yaml.safe_load((role / 'defaults/main.yml').read_text(encoding='utf-8'))
values['ngfw_metrics_influx_image'] = 'influxdb:1.0.0@sha256:' + '0' * 64
env = Environment(undefined=StrictUndefined)
env.filters['to_json'] = json.dumps
for external in ('', 'https://temporary.invalid/write?db=synthetic&precision=ns'):
    values['ngfw_metrics_external_write_url'] = external
    for path in (role / 'templates').glob('*.j2'):
        rendered = env.from_string(path.read_text(encoding='utf-8')).render(values)
        if path.name.endswith('.json.j2'):
            data = json.loads(rendered)
            assert len(data['sinks']) == (2 if external else 1)
            assert data['archive_days'] >= 90
        elif path.name.endswith('.yml.j2'):
            data = yaml.safe_load(rendered)
            assert data['services']['influxdb']['ports'][0].startswith('127.0.0.1:')
print('YAML parse and metrics template renders passed; no runtime compatibility claim')
