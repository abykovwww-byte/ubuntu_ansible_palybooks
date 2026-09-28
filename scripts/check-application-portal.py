"""Check real Nginx routing and auth in a disposable CI container, never on the host."""
import base64
import hashlib
import importlib.util
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from jinja2 import ChainableUndefined, Environment

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('portal_renderer', ROOT / 'scripts/render-application-portal.py')
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True).strip()


def request(port, path='/', host=None, auth=False):
    headers = {'Host': host} if host else {}
    if auth:
        headers['Authorization'] = 'Basic ' + base64.b64encode(b'fixture:fixture').decode()
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', headers=headers)
    try:
        response = urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, response.read().decode(), response.headers


def main():
    with tempfile.TemporaryDirectory(prefix='application-portal-') as directory:
        stage = Path(directory)
        context = renderer.render_portal(stage / 'html')
        env = Environment(undefined=ChainableUndefined)
        env.filters['bool'] = bool
        portal = env.from_string((ROOT / 'roles/application_portal/templates/portal.conf.j2').read_text()).render(context)
        proxy = env.from_string((ROOT / 'roles/nginx/templates/reverse_proxy.conf.j2').read_text())
        common = dict(nginx_client_max_body_size='64m', nginx_proxy_read_timeout='60s')
        # Synthetic fixture values only. Existing templates own both domain and LAN auth.
        hermes = proxy.render(**common, item=dict(name='fixture', server_names=['hermes.fixture'],
            extra_listeners=['0.0.0.0:19119'], upstream_host='127.0.0.1', upstream_port=19000,
            basic_auth=dict(enabled=True, realm='Fixture')))
        tovar = proxy.render(**common, item=dict(name='public-fixture', server_names=['tovar.fixture'],
            extra_listeners=['0.0.0.0:13101'], upstream_host='127.0.0.1', upstream_port=19000))
        config = 'events {}\nhttp { include /etc/nginx/mime.types;\n' + portal + hermes + tovar
        config += '\nserver { listen 127.0.0.1:19000; location / { return 200 "upstream fixture"; } }\n}\n'
        (stage / 'nginx.conf').write_text(config, encoding='utf-8')
        hashed = base64.b64encode(hashlib.sha1(b'fixture').digest()).decode()
        (stage / 'htpasswd').write_text('fixture:{SHA}' + hashed + '\n')
        name = 'portal-check-' + uuid.uuid4().hex[:10]
        mounts = ['-v', f'{stage / "nginx.conf"}:/etc/nginx/nginx.conf:ro',
                  '-v', f'{stage / "html"}:/var/www/application-portal:ro',
                  '-v', f'{stage / "htpasswd"}:/etc/nginx/.htpasswd-fixture:ro']
        docker('run', '--rm', *mounts, 'nginx:1.24-alpine', 'nginx', '-t')
        try:
            docker('run', '-d', '--rm', '--name', name, *mounts,
                   '-p', '127.0.0.1::80', '-p', '127.0.0.1::19119', '-p', '127.0.0.1::13101',
                   'nginx:1.24-alpine')
            ports = {port: int(docker('port', name, str(port)).rsplit(':', 1)[1]) for port in [80, 19119, 13101]}
            for attempt in range(30):
                try:
                    status, html, headers = request(ports[80])
                    break
                except (urllib.error.URLError, ConnectionError):
                    if attempt == 29:
                        raise
                    time.sleep(.2)
            assert status == 200 and 'Все приложения.' in html
            assert html.count('class="app-card"') == len(context['application_portal_apps'])
            assert headers['Content-Security-Policy'] and headers['X-Content-Type-Options'] == 'nosniff'
            assert request(ports[80], '/assets/portal.css')[0] == 200
            for app in context['application_portal_apps']:
                assert request(ports[80], f'/assets/{app["id"]}.svg')[0] == 200
            assert request(ports[80], '/missing')[0] == 404
            # Domain access and the additional LAN listener must BOTH require auth.
            assert request(ports[80], host='hermes.fixture')[0] == 401
            assert request(ports[19119])[0] == 401
            assert request(ports[80], host='hermes.fixture', auth=True)[:2] == (200, 'upstream fixture')
            assert request(ports[19119], auth=True)[:2] == (200, 'upstream fixture')
            assert request(ports[80], host='tovar.fixture')[:2] == (200, 'upstream fixture')
            assert request(ports[13101])[:2] == (200, 'upstream fixture')
            print('PASS: portal, all images, security headers, 404, domain/LAN proxy routing and Basic Auth')
        finally:
            docker('rm', '-f', name)


if __name__ == '__main__':
    main()
