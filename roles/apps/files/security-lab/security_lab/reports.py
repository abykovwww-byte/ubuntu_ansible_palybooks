"""Portable evidence graph with local navigation; no external scripts or resources."""

from html import escape
import json
from datetime import datetime, timedelta, timezone

from .osint_sources import source_catalog


def html_report(case: dict) -> str:
    def text(value: object) -> str:
        return escape(str(value))

    def when(timestamp: object) -> str:
        return datetime.fromtimestamp(float(timestamp), timezone(timedelta(hours=3))).strftime('%d.%m.%Y %H:%M:%S МСК')

    def refs(ids: str | list[str]) -> str:
        values = json.loads(ids) if isinstance(ids, str) else ids
        return " ".join(f'<a href="#e-{text(value)}">{text(value)}</a>' for value in values)

    nodes = {n["id"]: n for n in case["surface"]}
    observations = case.get("observations", [])
    # Small portable overview; full tables below retain every node and relation.
    # Round-robin sampling avoids a large CT response hiding networks or Git.
    groups = ["Домены", "IP / сервисы", "Сети / ASN", "Git", "URL", "Другие"]
    group_by_kind = {"domain": 0, "hostname": 0, "ip": 1, "service": 1,
                     "network": 2, "asn": 2, "repository": 3, "url": 4}
    buckets = [[] for _ in groups]
    for node in sorted(case["surface"], key=lambda n: (n["value"] != case["seed"], n["kind"], n["value"])):
        buckets[group_by_kind.get(node["kind"], 5)].append(node)
    selected = []
    for level in range(6):
        for bucket in buckets:
            if level < len(bucket) and len(selected) < 24:
                selected.append(bucket[level])
    positions = {}
    group_counts = [0] * len(groups)
    for node in selected:
        group = group_by_kind.get(node["kind"], 5)
        positions[node["id"]] = (20 + group * 240, 70 + group_counts[group] * 90)
        group_counts[group] += 1
    graph_height = max(180, 100 + max(group_counts, default=0) * 90)
    graph_edges = []
    for link in case.get("links", []):
        if link["source_id"] not in positions or link["target_id"] not in positions:
            continue
        x1, y1 = positions[link["source_id"]]
        x2, y2 = positions[link["target_id"]]
        ids = json.loads(link["evidence_ids"])
        destination = f'e-{ids[0]}' if ids else f'n-{link["target_id"]}'
        label = f'{nodes[link["source_id"]]["value"]} → {link["relation"]} → {nodes[link["target_id"]]["value"]}'
        if x1 == x2:
            # Curves stay outside boxes, including a self-link on the seed.
            start_y, end_y = (y1 + 14, y2 + 42) if y1 == y2 else (y1 + 28, y2 + 28)
            path = f'M {x1 + 210} {start_y} C {x1 + 250} {start_y} {x2 + 250} {end_y} {x2 + 210} {end_y}'
        else:
            start_x, end_x = (x1 + 210, x2) if x1 < x2 else (x1, x2 + 210)
            path = f'M {start_x} {y1 + 28} L {end_x} {y2 + 28}'
        graph_edges.append(f'<a href="#{text(destination)}" aria-label="{text(label)}"><title>{text(label)}</title>'
                           f'<path class="map-edge" d="{path}" '
                           'marker-end="url(#map-arrow)"/></a>')
    graph_nodes = []
    for node in selected:
        x, y = positions[node["id"]]
        short = node["value"] if len(node["value"]) <= 27 else node["value"][:24] + "…"
        graph_nodes.append(f'<a href="#n-{text(node["id"])}" aria-label="{text(node["value"])}">'
                           f'<title>{text(node["kind"])}: {text(node["value"])} — перейти к наблюдениям и источникам</title>'
                           f'<rect class="map-node" x="{x}" y="{y}" width="210" height="56" rx="7"/>'
                           f'<text x="{x + 10}" y="{y + 21}" class="map-kind">{text(node["kind"])}</text>'
                           f'<text x="{x + 10}" y="{y + 42}" class="map-value">{text(short)}</text></a>')
    graph_headings = ''.join(f'<text x="{20 + group * 240}" y="35" class="map-heading">{text(label)}</text>'
                             for group, label in enumerate(groups))
    graph = (f'<svg class="surface-map" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1440 {graph_height}" '
             'role="img" aria-labelledby="map-title map-description"><title id="map-title">Связи поверхности</title>'
             '<desc id="map-description">Сводная карта наблюдений. Узлы открывают таблицу объектов, линии открывают доказательства связи.</desc>'
             '<defs><marker id="map-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">'
             '<path d="M 0 0 L 8 4 L 0 8" fill="#588eb4"/></marker></defs>' + graph_headings +
             ''.join(graph_edges) + ''.join(graph_nodes) + '</svg>')
    graph_note = (f'Обзор: {len(selected)} из {len(nodes)} объектов; {len(graph_edges)} из {len(case.get("links", []))} связей. '
                  'Показано не более 24 объектов и 6 в каждой колонке. Полные таблицы ниже сохраняют все объекты и связи. '
                  'Клик по узлу открывает наблюдения; клик по линии — evidence связи. Карта не расширяет scope.')
    node_rows = []
    for node in case["surface"]:
        claims = [o for o in observations if o["node_id"] == node["id"]]
        history = "<br>".join(
            f'{text(o["attribution"])} / {text(o["freshness"])}; {when(o["collected_at"])}; '
            f'{refs([o["receipt_id"]])}; pointer {text(o["pointer"])}' for o in claims
        ) or text(node["attribution"])
        node_rows.append(f'<tr id="n-{text(node["id"])}"><td>{text(node["kind"])}</td>'
                         f'<td>{text(node["value"])}</td><td>{history}</td><td>{refs(node["evidence_ids"])}</td></tr>')

    link_rows = []
    for link in case.get("links", []):
        claims = [o for o in case.get("link_observations", []) if o["link_id"] == link["id"]]
        basis = "<br>".join(
            f'{text(o["basis"])}; {text(o["freshness"])}; {when(o["collected_at"])}; '
            f'{refs([o["receipt_id"]])}; pointer {text(o["pointer"])}' for o in claims
        )
        def node_link(node_id: str) -> str:
            return f'<a href="#n-{text(node_id)}">{text(nodes[node_id]["value"])}</a>'
        link_rows.append(f'<tr><td>{node_link(link["source_id"])}</td><td>{text(link["relation"])}</td>'
                         f'<td>{node_link(link["target_id"])}</td><td>{basis}</td><td>{refs(link["evidence_ids"])}</td></tr>')

    receipts = case.get("source_receipts", [])
    coverage_rows = "".join(
        f'<tr><td>{text(r["source"])}</td><td>{text(r["category"])}</td><td>{text(r["status"])}</td>'
        f'<td>{text(r["reason"])}</td><td>{bool(r["truncated"])}</td><td>{bool(r["synthetic"])}</td>'
        f'<td>{refs([r["id"]])}</td></tr>' for r in receipts
    )
    receipt_index = {r["id"]: r for r in receipts}
    evidence_articles = []
    for evidence in case["evidence"]:
        r = receipt_index.get(evidence["id"])
        metadata = ""
        if r:
            metadata = "<dl>" + "".join(
                f'<dt>{text(key)}</dt><dd>{when(r[key]) if key == "collected_at" else text(r[key])}</dd>' for key in (
                    "run_id", "request_url", "subject", "parser", "parser_version", "collected_at", "status",
                    "http_status", "reason", "truncated", "body_hash", "schema_version"
                )
            ) + "</dl>"
        evidence_articles.append(
            f'<article id="e-{text(evidence["id"])}"><h3>{text(evidence["source"])}</h3>'
            f'<p>Evidence {text(evidence["id"])}; synthetic: {bool(evidence["synthetic"])}</p>{metadata}'
            f'<details open><summary>Сохранённый ответ источника</summary><pre>{text(evidence["body"])}</pre></details>'
            '<a href="#surface">Вернуться к карте</a></article>'
        )
    public_receipts = [r for r in receipts if not bool(r["synthetic"])]
    http_responses = sum(r["http_status"] is not None for r in public_receipts)
    gaps = sum(r["status"] in {"error", "blocked", "rate_limited", "not_attempted"} for r in public_receipts)
    not_attempted = sum(r["status"] == "not_attempted" for r in public_receipts)
    note = (f'Сохранено HTTP-ответов внешних источников: {http_responses}. Записей с пробелами сбора: {gaps}; '
            f'из них not_attempted: {not_attempted}. Статус без HTTP-кода не подтверждает получение ответа. '
            'Наблюдения не подтверждают уязвимости или право проверять связанные ресурсы.') if public_receipts else (
        'SYNTHETIC: fixture-данные синтетические. Подтверждённых HTTP-ответов внешних источников пока нет.'
    )
    catalog = source_catalog()
    if not any(source["source"] == "seed_http" for source in catalog):
        catalog.append({"source": "seed_http", "category": "public_seed_navigation", "implemented": False,
                        "reason": "Public seed-site GET/robots/sitemap adapter pending; source lookup is not seed HTTP coverage"})
    catalog_rows = []
    for source in catalog:
        matching = [r for r in receipts if r["source"] == source["source"]]
        status = "not_implemented" if not source["implemented"] else ("recorded" if matching else "not_attempted")
        catalog_rows.append(f'<tr><td>{text(source["source"])}</td><td>{text(source["category"])}</td>'
                            f'<td>{text(status)}</td><td>{text(source.get("limitations", source.get("reason", "")))}</td>'
                            f'<td>{refs([r["id"] for r in matching])}</td></tr>')
    job_rows = []
    for job in case.get('jobs', []):
        evidence_ref = refs([job['evidence_id']]) if job.get('evidence_id') else '—'
        job_rows.append(f'<tr><td>{text(job["id"])}</td><td>{text(job["profile"])}</td>'
                        f'<td>{text(job["status"])}</td><td>{text(job["generation"])}</td>'
                        f'<td>{text(job.get("acknowledged_sequence", 0))}/{text(job.get("lease_sequence", 0))}</td>'
                        f'<td>{evidence_ref}</td></tr>')
    return ("""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Security Lab — карта поверхности</title><style>
body{font:16px system-ui;margin:2rem auto;padding:0 1rem;max-width:1400px;color:#17212b}
table{border-collapse:collapse;width:100%;margin:1rem 0}td,th{padding:.6rem;border:1px solid #ccd5dd;text-align:left;overflow-wrap:anywhere}
pre,dd{white-space:pre-wrap;overflow-wrap:anywhere}pre{background:#eef2f5;padding:1rem}
.note{padding:1rem;background:#fff1c7}article{margin-top:2rem}dt{font-weight:bold}dd{margin-bottom:.5rem}
:target{outline:3px solid #588eb4;scroll-margin-top:1rem}a{color:#176194}nav a{margin-right:1rem}
.surface-map{display:block;width:100%;height:auto;background:#f5f8fa;border:1px solid #ccd5dd}
.map-edge{stroke:#588eb4;stroke-width:2;fill:none;opacity:.65}.map-edge:hover{stroke:#17212b;stroke-width:5;opacity:1}
.map-node{fill:white;stroke:#588eb4;stroke-width:1.5}.surface-map a:hover .map-node{fill:#e5eff6}
.map-kind{font:12px system-ui;fill:#526472}.map-value{font:13px system-ui;fill:#17212b}.map-heading{font:bold 17px system-ui;fill:#17212b}
</style></head><body><h1>""" + text(case["seed"]) + "</h1><p>Дело " + text(case["id"]) + \
        "; состояние " + text(case["state"]) + '</p><p class="note">' + text(note) + '</p>' + \
        '<nav><a href="#surface">Объекты</a><a href="#links">Связи</a><a href="#coverage">Покрытие</a><a href="#materials">Материалы</a></nav>' + \
        '<p>Исторические URL и сертификаты могут быть устаревшими. Shared-provider и общий IP/ASN не доказывают владение. '
        'Каждое наблюдение сохраняет дату, атрибуцию и источник; противоречия не перезаписываются. Обнаружение не расширяет scope.</p>' + \
        '<h2>Карта связей</h2><p>' + text(graph_note) + '</p>' + graph + \
        '<h2 id="surface">Объекты карты</h2><table><tr><th>Тип</th><th>Объект</th><th>Наблюдения</th><th>Источники</th></tr>' + \
        "".join(node_rows) + '</table><h2 id="links">Связи</h2><table><tr><th>От</th><th>Связь</th><th>К</th><th>Основания</th><th>Источники</th></tr>' + \
        "".join(link_rows) + '</table><h2 id="coverage">Покрытие и пробелы</h2>' + \
        '<p>empty — успешный пустой ответ; error, blocked, rate_limited, not_attempted — пробелы сбора; partial/truncated — неполное покрытие.</p>' + \
        '<table><tr><th>Источник</th><th>Категория</th><th>Статус</th><th>Причина</th><th>Усечено</th><th>Синтетика</th><th>Ответ</th></tr>' + \
        coverage_rows + '</table><h2>Каталог источников и отсутствующие интеграции</h2>' + \
        '<p>Наличие адаптера не доказывает сбор в этом деле. recorded означает наличие receipts, включая ошибки; '
        'not_attempted означает отсутствие receipts; not_implemented означает незавершённую интеграцию.</p>' + \
        '<table><tr><th>Источник</th><th>Категория</th><th>Доступность/сбор</th><th>Ограничения или причина</th><th>Receipts</th></tr>' + \
        ''.join(catalog_rows) + '</table><h2>Запуски сбора</h2><pre>' + text(json.dumps(case.get("discovery_runs", []), ensure_ascii=False, indent=2)) + \
        '</pre><h2>Задания проверок</h2><p>Generation: ' + text(case.get('generation', 0)) + \
        '; консервативно списано секунд: ' + text(case.get('spent', 0)) + \
        '. Синтетический результат не доказывает проверку внешней цели.</p>' + \
        '<table><tr><th>Job</th><th>Профиль</th><th>Статус</th><th>Generation</th><th>ACK/lease sequence</th><th>Evidence</th></tr>' + \
        ''.join(job_rows) + '</table><h2 id="materials">Материалы</h2>' + "".join(evidence_articles) + '<h2>Ограничения исполнения</h2><pre>' + \
        text(json.dumps(case["limitations"], ensure_ascii=False, indent=2)) + '</pre></body></html>')
