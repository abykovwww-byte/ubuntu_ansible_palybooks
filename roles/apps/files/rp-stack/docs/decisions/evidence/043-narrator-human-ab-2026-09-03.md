# Decision 043 narrator human A/B — 2026-09-03

## Verdict

Human A/B accepts the clean narrator candidate with the universal narrator
contract at source revision
`89bcc7f4409155c28601fa03a10265af468bbaaa`.

The final blind round compared the previous corrected clean trajectory
`e2b05fd8d114527ad3e1333115eed9c898f957c8` with the universal-contract
trajectory. It did not compare against legacy. The same exact narrator model,
`openai/gpt-5.6-luna-pro`, generated both trajectories through OpenRouter.
Sides were independently shuffled across twelve anchors, with the universal
candidate assigned to A six times and B six times.

After the mapping was revealed, the result was:

| Outcome | Pairs |
| --- | ---: |
| universal-contract candidate | 8 |
| previous corrected clean | 1 |
| tie | 3 |

The accepted universal candidate won pairs 01, 02, 04, 06, 07, 08, 09 and 10.
The previous correction won pair 11. Pairs 03, 05 and 12 were ties.

The owner explicitly selected the universal candidate as the new canonical
source and authorized continuing the Decision 043 slice. This closes the
narrator-selection part of Plan 029 step 6.1. It does not close the manual
20-turn, contrasting-start or 65+ turn causal-chain gates in steps 6.2–6.3.

## Acceptance chain

The human comparison was resolved in three bounded rounds:

1. Clean candidate versus preserved legacy anchors: clean won 11, legacy won
   0, and 1 pair tied. The remaining clean defect was unwanted return to a
   superseded task during an active conflict.
2. Previous clean versus the first correction: 5 wins each and 2 ties. The
   correction fixed the stale-task rail but introduced or retained unexplained
   character knowledge, passive dialogue during threats, player-facing choice
   prompts and repetitive prose forms. It was not accepted.
3. Previous correction versus the universal-contract candidate: universal won
   8, previous won 1, and 3 tied. The owner observed materially better action,
   counterattacks and use of world mechanics instead of NPC instructions.

The final comparison therefore selects between two clean implementations after
legacy had already lost. It does not keep the legacy path alive as an A/B
control.

## Accepted functional behavior

The Gateway narrator contract is world-agnostic. It does not name a specific
world, character, player class or ability. It tells the narrator that:

- a superseded task remains background until a causal return;
- character sheets and action wording are service context rather than automatic
  in-world knowledge;
- characters know only what was heard, observed or previously established;
- player identity, role, class and abilities are not public titles without an
  in-world cause, while narration addresses the player in the second person;
- capable characters respond to immediate threats by changing the situation;
- NPC speech accompanies action instead of becoming a menu or an instruction to
  clarify, cancel or redirect the player's move;
- repetitive contrast formulas and end-of-response recaps should be avoided.

The one scenario-owned change contains only the concrete facts needed to make
the opening case understandable. It does not carry narrator behavior policy.

No deterministic prose filter, substring gate, automatic rewrite, second model
call, fallback, repair path, dependency or action-system abstraction was added.

## Exact runtime proof

The final 17-turn trajectory ran in a fresh isolated container and fresh SQLite
databases on abykovserv. It did not publish a host port, join the production
network, run Ansible or mutate production state.

| Evidence | Observed result |
| --- | --- |
| source revision | `89bcc7f4409155c28601fa03a10265af468bbaaa` |
| archive SHA-256 | `c60c03d152f9059d32e9e78dab6b45258252c3e5177eada0a94e08e4d25d8e0d` |
| exact image ID | `sha256:2741267ac35c0bec5ca6f11317ece8d93b86a2ac6b505be06e66a0c8acd675bd` |
| candidate Party | `party_e07350d13b16` |
| committed turns | 17, versions 1 through 17 |
| narrator calls | 17 completed, 17 HTTP 200 |
| route | `openrouter` → `openai/gpt-5.6-luna-pro` |
| attempt/fallback | `attempt = null`; no fallback, repair or NVIDIA route |
| token usage | 1,358,890 prompt; 55,978 completion; 1,414,868 total |
| reported provider cost | `$0.20232257` |
| Gateway SQLite | `integrity_check = ok`; 0 foreign-key violations |
| RP SQLite | `integrity_check = ok`; 0 foreign-key violations |

The production Gateway remained container
`683e68b83dc36c596ee2e92cf1a1e42fd486fb68b280a9f53a875b2145acb018`,
image
`sha256:9321777d9db87da6ac5b2b23c4c085a5d28a51199a90b2ec16d922b4b85295c4`
and start time `2026-09-02T12:38:09.600314415Z` before and after the probe.
The probe container, temporary data and copied secret environment were removed
after evidence capture. The exact candidate image remains only as a reproducible
acceptance artifact; it is not an activated production image.

Local verification of the final source produced `97 passed in 6.39s`, with all
repository, DevKit, JavaScript and installed-skill drift gates passing. The
production allowlist remained `4,985 / 5,000` physical LOC with debt 0.

## Known residual quality limits

The acceptance is comparative, not a claim of perfect prose. The owner recorded
three non-blocking residual observations in the final round:

- pair 10 still ended with an explanatory recap instead of presenting the next
  event directly;
- pair 11 degraded later in the large battle, returning to dialogue and giving
  a transformed character an insufficiently motivated rescue action;
- pair 12 began more strongly but contained an unclear addressee and an awkward
  transformed-character reference.

The owner nevertheless accepted the universal candidate because action and
counteraction improved materially overall. No further narrator iteration or
provider run belongs to this acceptance round. Long-scene stability remains
observable evidence for the still-open Plan 029 steps 6.2–6.3.

## Delivery boundary

- source candidate: accepted at `89bcc7f4409155c28601fa03a10265af468bbaaa`;
- local tests and isolated exact-image proof: complete;
- human narrator A/B selection: complete;
- push/PR/merge: not yet evidenced by this document;
- manual 20-turn, contrasting-start and 65+ turn causal proof: not complete;
- Ansible apply, production activation and production live proof: not performed.
