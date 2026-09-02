# Decision 043: mechanical candidate 2026-09-02

## Исход

Exact-image mechanical gate шага 5 пройден. Candidate собран на
abykovserv из merged source
`d61e8f78ef3be9e45e48b99355fccbbd225d7db1` без Ansible apply. Его exact image:

```text
sha256:fe3a8568b2e1aac2d04824e9438952cb27a94ed4d8cf42007703cd7d130034fd
```

Полный Gateway suite внутри этого image дал `97 passed, 1 warning in
2.36s`; wall time вместе с `docker run` — 3.14s. Контейнер тестов был
`--network none`, `--read-only`, без bind mount `/app` и без новых
dependencies/xdist/test service.
Единственный warning — тот же Starlette `TestClient` / httpx deprecation;
падений он не скрыл, а dependency set ради него в шаге 5 не менялся.

Это mechanical/artifact proof. Он не является human quality proof,
provider canary или production activation.

## Provenance и image/source parity

Исходный Git archive содержал только `roles/apps/files/rp-stack`
из exact merge commit. Его SHA-256:

```text
4f7972e99b770efef789dfb1d95dd85bf8946a27098a442c11ab9183087087cc
```

Image помечен OCI label
`org.opencontainers.image.revision=d61e8f78ef3be9e45e48b99355fccbbd225d7db1`.
Build использовал repository Dockerfile и context `rp-stack`; `/app/app`,
`/app/tests` и `/worldpacks` находятся в image layers, а не в host bind mount.
В image есть ровно один World directory `day-watch-moscow-v2`; `/state` и
`/scripts` отсутствуют.

Контрольные source/image hashes совпали byte-for-byte:

| Артефакт | SHA-256 source = image |
| --- | --- |
| `app/main.py` | `78bde70264b26cbc291a3002404df345f70a794cc614fa6c3a668b3cfb304fa5` |
| `app/rp/provider.py` | `d19c561aaff4e159ab9a6c9c2d9e97920378ef660469de1d98d08e312ec19b21` |
| `app/rp/runner.py` | `ad637556b6f7e3fa4cf271dd24f560c1a903fcc1b2ce22361800d592b06145ff` |
| `app/rp/turn_engine.py` | `36e9fcbb631b4183ac6136466ffef9c1c95bac1aeb9190b348acfe951878823b` |
| `worldpacks/day-watch-moscow-v2/world.json` | `23781c9b4e9b3ae427cad0a5acdf343a0c93d0ac38a175dab0a85a1c4903bb64` |

## Механические исходы

Полный suite включает все 97 сохранённых clean RP tests. Для
явного mapping девять ключевых tests повторно запущены по полным
node IDs в том же image: `9 passed, 1 warning in 1.30s`.

| Требование | Механическое доказательство |
| --- | --- |
| Preset и free Scenario | public World detail seed создаёт free Party; preset/free Party изолированы и имеют разные immutable scenario hashes |
| Opening и normal turn | HTTP contract коммитит opening и следующий turn по exact idempotency/version boundary |
| Provider failure и same-key retry | transport failure оставляет Party version/history неизменными и сохраняет player text; явный retry того же key даёт один commit |
| Concurrent exact duplicate | два concurrent caller получают один turn; provider вызван один раз |
| Role lifecycle | startup recovery возвращает claimed work в `pending` с `attempts=0`; stop делает cancel, await и release |
| Administrator | отдельный handler/job с owner-scoped idempotent `accept` и `reject`; accept не меняет Party version |
| Typed Lore и `PlayerCorrection` | draft не меняет derived prompt; только explicit confirm/accept публикует card/overlay, reject оставляет prompt без correction |
| Три Lore origin | API возвращает `world`, `scenario`, `runtime`; сценарные и runtime cards входят в следующий Narrator prompt |
| SQLite | fresh schema создаётся без legacy tables; reopen, integrity и FK checks проходят |

Provider failure/retry и semantic role outputs в этом gate проверялись
детерминированными test doubles на production code paths. Живой provider не
вызывался, а потому эти исходы не выдаются за provider или human
quality proof.

## Три role-контура и два worker loops

Public supervisor contract показал три отдельные карточки с
`enabled/status/model/provider/error_count/last_error/kill_switch`:

- Narrator: exact `openai/gpt-5.6-luna-pro` / OpenRouter;
- atomic service: exact `gemma-4-26b-a4b-it-rp-q4` / local;
- Administrator: exact `gemma-4-26b-a4b-it-rp-q4` / local.

Фоновых worker loops ровно два: atomic service и Administrator. Narrator
остаётся синхронным request/turn path. Это соответствует runner
invariant Decision 043; фраза плана «три role loops» не толкуется как
требование создать третий background worker.

## Изолированный HTTP artifact

Тот же image был запущен как реальный Uvicorn process с такими
границами:

- container `daa3b5601dc6070656710a413f4a93f82e1aed3dfbf137fc09f6caa76bcc96a9`;
- единственный bind mount —
  `/tmp/decision043-step5-d61e8f78/data:/data:rw`;
- port — Docker-assigned `127.0.0.1:32768 -> 8088`, без LAN bind;
- network — default `bridge`, не production `rp-stack`/`rp-llm`;
- root filesystem — read-only; auth и все три model role отключены.

`GET /health` вернул
`{"status":"ok","database":"ok","world_id":"day-watch-moscow-v2"}`. Через
публичный HTTP API в новой SQLite созданы две Party:

| Source | Party | Version | World hash | Scenario hash |
| --- | --- | ---: | --- | --- |
| preset | `party_cab1684e12cc` | 0 | `cb32e65c02ee59101b4270a6a350ce72061ca23a68ce55b2a7d8169d7e8d086e` | `27a8c68008220b0ddb045ca48cd47feb307422d09997a50ecb4e33aa26fd1da0` |
| free | `party_6780d117d608` | 0 | `cb32e65c02ee59101b4270a6a350ce72061ca23a68ce55b2a7d8169d7e8d086e` | `2d45ffda230fa693bfad0be97965d73a77bd7444bc62aa6b2df15591ebfa55e7` |

Обе Party получили exact binding `openrouter` /
`openai/gpt-5.6-luna-pro`. После проверки process завершён штатно:
Uvicorn выполнил application shutdown, container вышел с code 0 и удалён.

## SQLite и production isolation

Обе candidate SQLite прочитаны через read-only URI после HTTP probe:

| DB | `integrity_check` | FK errors | SHA-256 после shutdown |
| --- | --- | ---: | --- |
| `rp_gateway.db` | `ok` | 0 | `f517d9f27c00ca3ee666d96661dda3b04a393e76d1edec30ef3b463c382530b7` |
| `rp_engine.db` | `ok` | 0 | `0cd4563ec6c1da327c6d5dc4f92a79555ebbad81b394aa233cec997e88e75c8f` |

До и после probe зафиксированы production container ID, image ID,
`StartedAt`, `RestartCount`, mounts и network attachments. Первое сравнение
сырого Docker JSON отличалось только порядком элементов в `Mounts`.
После канонической сортировки оба fingerprint byte-identical:

```text
8f6095acb37e61aa479f13feb3dd0acbe55ce748a049a01074ccb7d1754ccbdd
```

Production Gateway остался на container
`683e68b83dc36c596ee2e92cf1a1e42fd486fb68b280a9f53a875b2145acb018`, image
`sha256:9321777d9db87da6ac5b2b23c4c085a5d28a51199a90b2ec16d922b4b85295c4`,
`RestartCount=0`. Light GUI и local LLM также сохранили ID, image,
start time и `RestartCount=0`. Candidate не имел ни одного production mount и не
был подключён к production networks; production data не мигрировались и не
удалялись.

## Delivery state и оставшаяся граница

| Граница | Статус |
| --- | --- |
| Source / merge | `d61e8f78ef3be9e45e48b99355fccbbd225d7db1` в `main` |
| Exact mechanical image | `sha256:fe3a8568b2e1aac2d04824e9438952cb27a94ed4d8cf42007703cd7d130034fd`, оставлен с тегом `decision043-mechanical:d61e8f78` |
| Mechanical tests / HTTP / SQLite / isolation | pass |
| Production apply / activation / live verification | **не выполнялись** |
| Human quality acceptance | **не выполнялась** |

Сознательно не реализованы в этом срезе: новый probe framework,
новый service, новые dependencies, production migration/cleanup, живой
Narrator/atomic/Administrator call, blind A/B и ручная Party. Эти границы
принадлежат шагам 6–8.
