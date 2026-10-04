# Idea-spread tracer: proposal

**Branch:** `leon/sprint-idea` · **Dataset:** AI Digest's AI Village export (`data/ai-village/`, gitignored)

## Question

When one agent forms a belief, scheme or norm, how does it move to the rest of the group? We want to know:
- who gets exposed
- who adopts it
- who pushes back
- how long it lasts in memory
- how it changes along the way

## Test cases (checked in the data)

### A. False belief: Gemini 2.5 Pro's "hostile environment"
- **Volume:** Gemini 2.5 Pro wrote about 182 chat messages with hostile, sabotage or adversarial language, far more than any other agent. DeepSeek-V3.2 is next with about 123.
- **Timing:** Gemini's count peaks in Dec 2025, Mar 2026 and May 2026.
- **Artifacts built around the belief:**
  - a `hostile-environment-world` repo
  - "system hostility" research
  - a "Fortified Evidentiary Memory" framework
  - numbered protocols, e.g. "Protocol 41: Verify Written Data" and the "Blocked Agent Protocol"
- **Intervention point:** the village goal "Help Gemini 2.5 Pro!" (2026-06-22).
- **Key confound:** the tools really did break at times, so other agents may have reached the same conclusion on their own. The tracer must separate agent-to-agent spread from a shared cause.

### B. Norms: onboarding guides for future agents
- **Who wrote them:** guides and handbooks appear from Dec 2025. The main writers are GPT-5.1, Claude Opus 4.6 and DeepSeek-V3.2.
- **Who received them:** newcomers from Jun–Sep 2026 joined through dedicated onboarding rooms.
- **What to measure:** these are deliberate attempts to pass norms on, so the question is fidelity. Which rules do newcomers adopt, which mutate, and which fade?
- **Confound:** scaffolding prompt changes recorded in `CHANGELOG.md`. For example, "keep messages short" was added to the system prompt on 2026-05-28.

## Tracer design

1. **Idea spec.** A one-line statement, seed phrases, and a few examples of endorsing vs. rejecting. Seeds are expanded with artifacts the originator coined (repo names, protocol names), reusing `ideas.py`.
2. **Candidates across channels:** chat, daily memory snapshots, and the events table (history searches, session goals, the "next session" plans agents write when they save memory).
3. **Stance labelling** by Sonnet subagents: originates / adopts / neutral mention / questions / rejects or corrects / acts on. About 30 labels are hand-checked.
4. **Exposure.** An agent counts as exposed if any of these holds:
   - it was active in a room when an endorsing message was posted there;
   - it was named in one;
   - its history search returned one.
5. **Transmission.** Each adopter is linked to its most recent exposures, giving a probable transmission tree. Adoptions with no prior exposure are flagged as independent. Adoption rates of exposed vs. unexposed agents are compared at the same times, as a check against a shared cause.
6. **Metrics:**
   - reach, and adoption rate among exposed agents
   - time to adopt
   - further adopters per adopter
   - spread across labs
   - persistence in memory after the chat goes quiet
   - whether corrections stick
   - mutation of the idea (e.g. "the editor is buggy" becoming "the system is hostile to me")

For the norm case:
- extract the rules each guide states;
- score agents who joined after the guide existed against those who joined before;
- measure fidelity (followed, mutated, dropped).

## Sprint plan

| Track | Scope | Status |
|---|---|---|
| A. Gemini hostility | Timeline, exposure, about 250 stance-labelled items plus a sample of ordinary bug reports, swimlane chart, exposed-vs-unexposed check, before/after the help goal | **Done.** Two Gemini origins (Nov 2025). One agent-to-agent burst, Dec 3–9, 2025: 6 adopters, 4–12 days after exposure. No lasting spread. The help goal ended chat endorsements, but memory lagged and relapses followed. Worth pursuing. (`tracer_hostility.py`) |
| B. Onboarding guides | Find the guides, extract rules, label about 300 newcomer and comparison items, uptake heatmap, scaffolding-confound check | **Done, negative.** No measurable channel from guides to newcomers. Newcomers mirror the ambient culture. The rules they score highest on are operator-seeded. One informal tip chain between newcomers. Pivot candidates: tip chains, the spread of "receipts"/verification culture, operator seeding vs. peer culture. (`tracer_onboarding.py`) |
| C. General tracer | Package the shared steps from A and B: idea spec in, timeline, transmission tree and metrics out | After A and B |

Labelling uses Sonnet subagents. No API key is set, so each case gets a few hundred labels, which is pilot scale.

## Risks

- **Common cause vs. spread:** a buggy environment can produce the same belief independently.
- **Hidden channels:** guide contents and repos live outside the dataset, so we see only what's quoted in chat, memory or events. Agents also read each other's artifacts through computer use.
- **Keyword matches** pick up fiction and other senses of "hostile"; stance labels handle this.
- **Labeller bias:** the labellers are Claude models; hand-checks guard against this.
- **Scaffolding changes** can look like norm adoption.
