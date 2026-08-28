# Intake Questions

Ask no more than 3 questions at a time. If the user wants speed, make reasonable assumptions and record them in the world pack manifest.

## Mandatory New World Intake

Do not create a new non-test world pack until these three questions are answered
in the current thread, discoverable local context, or explicit user-provided
brief:

1. Название мира / кампании: как должен называться мир?
2. Суть и источник мира: о чем мир, и он оригинальный, основан на реальном мире/истории, или на существующем лоре/IP?
3. Персонаж игрока: кто игрок на старте, каков его статус/уровень силы и начальные ограничения?

Ask them together, in Russian when the user is using Russian:

```text
Перед созданием мира уточню три вещи:
1. Как называется мир/кампания?
2. В чем суть мира: оригинальный, реальный/исторический или по существующему лору/IP?
3. Кто персонаж игрока на старте: роль, статус/сила, ограничения?
```

Only skip this intake when the user explicitly says to create a test/smoke world
or to proceed on assumptions. In that case, record the assumptions in
`manifest.json` and label the pack as draft/test.

If the user references a "neighboring thread" or prior conversation but its
details are not available locally, ask for the mandatory intake again instead
of inventing it.

## Minimum Viable Intake

Use these when the user gives only a short premise:

1. Player role: who is the player at the start?
2. Canon mode: canon-faithful, canon-divergent, or original inspired-by?
3. Tone and boundaries: power fantasy, intrigue, survival, comedy, grimdark; any hard limits?

## Optional Questions

Use only when they matter:

- Language for play: Russian, English, bilingual.
- Starting scale: village, city, kingdom, empire, multiverse.
- Power level: weak survivor, competent adventurer, faction leader, monster, ruler.
- Relationship style: solo protagonist, party, harem, political court, guild/team.
- Mechanics taste: light checks, crunchy checks, mostly narrative.
- Lore density: compact, medium, encyclopedic.
- Spoiler policy for fandom worlds: avoid spoilers, use all canon, user-provided canon only.
- NSFW/romance policy: absent, fade-to-black, explicit only if allowed by the broader system and user.
- Automation: draft only, install locally, deploy to server.
- Creation path: quick Light GUI prompt-world, reviewable Git worldpack, or both.
- Play surface: Light GUI first, SillyTavern compatibility, or legacy SillyTavern-only.

## Existing IP Handling

When the user names an anime, game, book, or franchise:

- Ask whether they want canon-faithful or inspired-by if not obvious.
- Prefer original campaign situations inside the setting rather than reproducing scenes verbatim.
- Keep entries concise. Do not paste long copyrighted passages.
- If exact canon accuracy matters and facts may be uncertain, browse or ask the user to provide canon notes.
- Preserve player agency: do not force the player to become the original protagonist unless requested.

## Assumption Block

If proceeding without answers, include a manifest section like:

```json
{
  "assumptions": [
    "Canon-divergent fan campaign.",
    "Russian prose and UI labels.",
    "Player starts as a newly awakened minor power with limited local knowledge."
  ]
}
```

## Light GUI Shortcut

If the user wants to start playing quickly rather than create a reusable pack,
offer the new Light GUI path:

```text
Можно быстро создать мир прямо в Light GUI через "Новая партия -> Мир -> Задать prompt".
Это сохранит runtime prompt-world на сервере, но не создаст reviewable Git worldpack
и не установит SillyTavern lorebook.
```

Use the full mandatory intake only when the user wants a reusable/deployable
world pack, lorebook, character notes, or server-side Git/IaC artifact.
