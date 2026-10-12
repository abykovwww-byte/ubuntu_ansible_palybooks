"""Extract supplied Grafana definitions without claiming a live metric exists."""
import hashlib
import json
from pathlib import Path


def panels(document):
    for panel in document.get('panels', []):
        yield panel
        yield from panels(panel)
    for row in document.get('rows', []):
        yield from panels(row)


def extract(path, source_url=None, native_interval=None):
    path = Path(path)
    raw = path.read_bytes()
    document = json.loads(raw)
    document = document.get('dashboard', document)
    result = []
    for panel in panels(document):
        for index, target in enumerate(panel.get('targets', [])):
            result.append({
                'name': panel.get('title', 'UNKNOWN'),
                'dashboard': document.get('title'), 'panel_id': panel.get('id'),
                'target_ref': target.get('refId', str(index)),
                'source': {'file': path.name, 'sha256': hashlib.sha256(raw).hexdigest(),
                           'url': source_url, 'datasource': target.get('datasource', panel.get('datasource'))},
                'evidence_status': 'DECLARED_DASHBOARD_NOT_LIVE_VERIFIED',
                'unit': panel.get('fieldConfig', {}).get('defaults', {}).get('unit'),
                'legacy_axes': panel.get('yaxes'),
                'kind': 'UNKNOWN', 'interval_seconds': native_interval,
                'formula': {'target': target, 'transformations': panel.get('transformations', []),
                            'field_config': panel.get('fieldConfig'),
                            'legacy_columns': panel.get('columns')},
                'limitations': ['Counter/gauge and live availability require actual source inspection.',
                                'Grafana variables, panel overrides and transformations are preserved, not evaluated.',
                                'OS CPU does not establish useful dataplane utilization.'],
            })
    return {'schema_version': 1, 'metrics': result,
            'dashboard_variables': document.get('templating', {}).get('list', []),
            'source_sha256': hashlib.sha256(raw).hexdigest()}
