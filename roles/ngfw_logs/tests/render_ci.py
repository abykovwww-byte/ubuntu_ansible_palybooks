"""Render normal templates with loopback-only synthetic CI inputs."""
import json
from pathlib import Path
import sys

from test_config import ROOT, render_values

directory = Path(sys.argv[1])
directory.mkdir(parents=True, exist_ok=True)
values, config, _ = render_values(ngfw_logs_listen_address='127.0.0.1',
                                ngfw_logs_audit_sources=['127.0.0.1'],
                                ngfw_logs_ngfw_sources=['127.0.0.1'],
                                ngfw_logs_rotate_mib=1, ngfw_logs_rotate_count=2)
(directory / 'collector.json').write_text(json.dumps(config), encoding='utf-8')
# The real production Compose template is parsed by Docker without starting it.
import jinja2
env = jinja2.Environment(undefined=jinja2.StrictUndefined)
(directory / 'compose.yml').write_text(env.from_string(
    (ROOT / 'templates/compose.yml.j2').read_text()).render(values), encoding='utf-8')
