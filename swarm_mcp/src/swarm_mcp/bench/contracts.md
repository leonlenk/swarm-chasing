# Bench contracts: what the scope tools return

The synthetic benchmark (`python -m swarm_mcp.bench`) scores three investigation
tools that are being built on another branch. This file fixes the **output shapes**
the scorer reads. A tool may return extra keys; the scorer ignores them.

## Ids and actors

- **Event ids** use `<source>:<kind>:<local_id>` (origin/main `events.py`):
  `village:chat:<chat_messages.id>`, `village:event:<events.id>`,
  `village:agent:<agents.id>`, `village:goal:<village_goals.id>`.
  The scorer also accepts `village:msg:<uuid>` (the SwarmScope adapter's spelling)
  as an alias of `village:chat:<uuid>`.
- **Actors** should be agent ids (`village:agent:<uuid>`). The scorer also accepts a
  bare agent uuid or an exact display name. Display names are matched exactly, so a
  Cyrillic look-alike name never resolves to the Latin original.
- **Timestamps** are ISO 8601 or dataset format (`2031-03-11 14:02:07.123456`), naive UTC.

## `scope_trace_diffusion(term)`

How one term spread: who used it first and, for every later user, whether they were
exposed to it before their first use.

```json
{
  "term": "glimmerframe",
  "first": "village:chat:<uuid>",
  "adopters": [
    {"actor": "village:agent:<uuid>", "first_event_id": "village:chat:<uuid>",
     "label": "likely_copier", "basis_event_id": "village:chat:<uuid>"},
    {"actor": "village:agent:<uuid>", "first_event_id": "village:chat:<uuid>",
     "label": "possibly_independent", "basis_event_id": null}
  ]
}
```

- `first`: the earliest message containing the term (case-insensitive, whole word).
- `adopters`: one entry per later user, excluding the first user, keyed by their first use.
- `label`: `likely_copier` when some earlier use of the term could have reached the
  adopter before their first use, otherwise `possibly_independent`. An earlier use can
  reach the adopter if it was posted in a room the adopter took part in, or if it names
  the adopter.
- `basis_event_id`: the earliest earlier use that could have reached them, or `null`
  when `possibly_independent`.

## `scope_coordinators()`

Agents whose messages others act on: shortly after the agent posts, other agents
reply naming it and start work whose session goal names it. The result is ranked best
first. Either a bare list or `{"coordinators": [...]}` is accepted.

```json
{
  "coordinators": [
    {"actor": "village:agent:<uuid>", "score": 8.0,
     "example_event_ids": ["village:chat:<directive>", "village:chat:<reply>", "village:event:<session goal>"]}
  ]
}
```

## `scope_integrity_report()`

Data problems an analyst should know about before trusting attributions.

```json
{
  "name_collisions": [
    {"agents": ["village:agent:<a>", "village:agent:<b>"], "names": ["Corvin", "Cоrvin"],
     "kind": "homoglyph", "evidence_event_ids": ["village:chat:<self-reference>"]}
  ],
  "gaps": [
    {"actor": "village:agent:<uuid>", "start": "2031-03-10 19:44:01.000000",
     "end": "2031-03-16 09:12:40.000000", "days": 5.6,
     "evidence_event_ids": ["<last event before>", "<first event after>"]}
  ],
  "attribution_issues": [
    {"event_id": "village:chat:<uuid>", "issue": "missing_event"},
    {"event_id": "village:chat:<uuid>", "issue": "speaker_mismatch",
     "talk_event_id": "village:event:<uuid>",
     "chat_speaker": "village:agent:<uuid>", "event_speaker": "village:agent:<uuid>"}
  ]
}
```

- `name_collisions`: agents whose display names differ but look the same, for example
  through Unicode confusables or invisible characters. Any group of two or more agents is
  scored as all its pairs.
- `gaps`: periods with no activity (chat or events) between an agent's first and last
  activity. Silence before an agent joins or after it leaves is not a gap.
- `attribution_issues`: agent chat rows with no `AGENT_TALK` event (`missing_event`), and
  rows whose `AGENT_TALK.speakerId` differs from `agent_speaker_id` (`speaker_mismatch`).
  `event_id` is the chat message; the `AGENT_TALK` event id is also accepted there.
  Human messages (`USER_TALK`) are not issues.

## Outputs file read by `score`

```json
{
  "diffusion": {"<term>": <scope_trace_diffusion(term)>, "...": "..."},
  "coordinators": <scope_coordinators()>,
  "integrity": <scope_integrity_report()>
}
```

A missing task scores 0 recall for that task and is listed in `summary.missing`.

## Scoring (`score.py`)

| task | items compared | a match needs |
|---|---|---|
| diffusion | `first` per term, and `(term, actor, label)` per adopter | the same id, or the same actor **with the same label** (a wrong label counts as one FP and one FN) |
| coordinators | the top-k actors, where k = the number of true coordinators | the same actor |
| integrity.name_collisions | unordered agent pairs | the same pair |
| integrity.gaps | `(actor, [start, end])` | the same actor and interval IoU ≥ 0.5 |
| integrity.attribution_issues | `(chat event id, issue)` | the same message **and** the same issue |

Each task reports precision, recall and F1. Diffusion also reports how often `first`,
each adopter's `first_event_id`, and `basis_event_id` are correct; `basis_event_id` is
correct if it is any acceptable exposure listed in the truth. Coordinators also report
the rank of each true coordinator, MRR, and the share of `example_event_ids` that are
planted coordination events. The integrity score is micro-averaged over its three
lists. `summary.macro_f1` is the mean F1 of diffusion, coordinators and integrity.
