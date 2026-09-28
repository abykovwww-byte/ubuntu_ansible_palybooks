# Application portal

The server's default HTTP page is a small, self-contained application catalogue at
`http://192.168.1.88/`. Nginx serves static HTML, CSS, JavaScript and local SVG
illustrations; there is no application process, database, external font, CDN or
Docker socket. Cards navigate in the same tab to the existing application.
Search and category filters are optional progressive enhancements; all links
are present in the initial HTML and also work without JavaScript.

The dedicated `application_portal` role owns `/var/www/application-portal` and
`/etc/nginx/sites-available/application-portal.conf`. The catalogue is
`inventories/local/group_vars/portal.yml`; change names, descriptions, artwork
IDs, URLs and operator notes there. Status notes are explicitly dated manual
observations, not health monitoring. Remove/update them after restoring an app.
No credentials belong in this catalogue or its URLs.

## Entry points

| Application | Entry point | Boundary |
| --- | --- | --- |
| Task Reminder | `http://192.168.1.88:3100/` | Existing app login |
| tovar.ai | `http://192.168.1.88:3101/` | Existing application |
| USE Framework | `http://192.168.1.88:8765/` | Existing application |
| Showroom | `http://192.168.1.88:8011/` | Existing application |
| PT NGFW | `https://192.168.1.88:8443/` | Existing lab management login and certificate |
| Obsidian | `https://192.168.1.88:3201/` | Existing native HTTPS and app login |
| Beszel | `http://192.168.1.88:8090/` | Existing configured endpoint |
| RP Stack | `http://192.168.1.88:8010/` | Existing endpoint, paused at discovery |
| OpenSearch | `http://osearch.abykov.site/` | Existing DNS and Basic Auth, paused at discovery |

Task Reminder and tovar.ai keep their existing port-80 domain virtual
hosts. Optional `extra_listeners` on those SAME virtual hosts add LAN-only
addresses using the existing backend port numbers. Docker still binds its
backends to loopback. The portal links to Obsidian's existing LAN HTTPS listener (delivered
separately in PR #180) and does not proxy it or change its access policy. No firewall,
Tailscale, other app authentication or VM policy is changed.

Hermes is retired: the `retired_hermes` role removes only its named container,
`/srv/apps/hermes/compose.yml`, both nginx site definitions and their enabled
links. It preserves `/srv/app-data/hermes`, logs and credential files. The
service has no deployment definition or portal card, so later applies cannot
recreate it, even if an obsolete `hermes_enabled` override remains.

## Delivery and rollback

Use the standard branch -> PR -> green CI -> merge -> pull-based Ansible flow.
Prefer the focused apply below: unlike a full `site.yml` apply, it only converges
Hermes retirement, nginx and the portal; it does not restart other applications.

On the server, after the PR is merged (interactive sudo):

```bash
cd /opt/ubuntu_ansible_palybooks
sudo ./scripts/apply-local.sh playbooks/application-portal.yml
```

The script pulls `main` and passes server-local overrides to Ansible. The focused
playbook expects the existing nginx platform and inventory; it is intended for
this already provisioned server. Both roles run `nginx -t` before handlers
reload nginx. Changes to static assets alone require no reload.

Verify `GET /` and all assets return 200, the two LAN entries work, Hermes is absent from `docker ps -a`, ports 8642
and 9119 have no listeners, and remaining named virtual hosts still route
correctly. Check browser search, categories, keyboard access, narrow layout and
card navigation on the deployed page. The CI `application-portal` job validates
the real generated nginx configuration in an isolated container, including
positive/negative domain and additional-listener Basic Auth cases.

For rollback, set `application_portal_enabled: false` and remove the two
`extra_listeners` entries in inventory, then repeat the focused apply via the
same PR flow. The role removes only its enabled-site link; app data is untouched.
Disabling the portal does not undo Hermes retirement.

Offline preview (Python with PyYAML and Jinja2):

```bash
python scripts/render-application-portal.py /path/to/preview
```

The renderer deliberately expands only public catalogue values; it does not
execute secret lookups or read server override files. Open the generated
`index.html` directly. This portal does not change RP Stack architecture or its
operator workflow, so the RP Wiki is unaffected.
