"""Portable, escaped reports. External content is never injected into HTML."""

from html import escape
import json


def html_report(case: dict) -> str:
    rows = "".join(
        "<tr>" + "".join(f"<td>{escape(str(n[k]))}</td>" for k in ("kind", "value", "attribution", "evidence_ids")) + "</tr>"
        for n in case["surface"]
    )
    evidence = "".join(
        f'<article id="e-{escape(e["id"])}"><h3>{escape(e["source"])}</h3>'
        f'<p>Evidence {escape(e["id"])}; synthetic: {bool(e["synthetic"])}</p>'
        f'<pre>{escape(e["body"])}</pre></article>' for e in case["evidence"]
    )
    return """<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Security Lab — отчёт</title><style>
body{font:16px system-ui;margin:2rem auto;padding:0 1rem;max-width:1100px;color:#17212b}
table{border-collapse:collapse;width:100%}td,th{padding:.7rem;border:1px solid #ccd5dd;text-align:left}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#eef2f5;padding:1rem}
.note{padding:1rem;background:#fff1c7}article{margin-top:2rem}
</style><h1>""" + escape(case["seed"]) + "</h1><p>Дело " + escape(case["id"]) + "</p>" + \
        '<p class="note">Прототип управляющего контура. Fixture-данные синтетические. Live discovery и pentest не выполнялись.</p>' + \
        "<table><tr><th>Тип</th><th>Объект</th><th>Атрибуция</th><th>Evidence IDs</th></tr>" + rows + \
        "</table><h2>Материалы</h2>" + evidence + "<h2>Ограничения</h2><pre>" + \
        escape(json.dumps(case["limitations"], ensure_ascii=False, indent=2)) + "</pre></html>"
