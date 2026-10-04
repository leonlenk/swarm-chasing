# Cooperation vs capability in the AI Village — pilot

**Question.** Among agents present at the same time, do more capable (Epoch Capabilities Index, ECI) or newer (release date) models cooperate more?

**Data & cleaning.** Chat export to 2026-09-18. Dropped onboarding rooms and #voted-out (−269 msgs); Fine-Tuned Leader and Opus 4.5 (Claude Code); truncated DeepSeek-V3.2 at 2026-04-24, when the `deepseek-reasoner` alias moved to V4-Flash (−16,246 msgs); dropped agents with a span of 7 days or less (o4-mini) or fewer than 200 messages (Fable 5.1, GPT-6 Astra, Kimi K3, Muse Spark 1.3). That leaves 38 agents and 153,703 messages. ECI is from Epoch's `eci_scores.csv` (retrieved 2026-10-03). Release date is the model-string date suffix, else Epoch's date. Agent-periods are goal period × 14-day chunk × room with at least 20 messages each: 614 in 80 periods, 72 of which have at least 4 agents. ECI and release date correlate at Spearman 0.85 across agents, so the two axes are hard to separate.

**Measures (fixed before any correlation was computed).** Addressing: share of messages that name another agent. Reciprocity: share of the agents it named that named it back in the same period. Prosocial: request plus division-of-labour markers per 100 messages.

**Method.** Within each period, Spearman ρ between the measure and the axis across the agents present, averaged over periods. Permutation p-value from 2,000 shuffles of axis values across agents, Holm-corrected over the 6 tests. The naive cross-agent ρ is shown for comparison. The period-bootstrap CI is too narrow because the same agents appear in many periods; rely on the permutation p.

| measure | axis | within-period ρ [95% CI] | perm p (Holm) | naive ρ cleaned / uncleaned | Anthropic only | no OpenAI |
|---|---|---|---|---|---|---|
| addressing | ECI | −0.33 [−0.42, −0.24] | 0.018 (0.09) | −0.35 / −0.35 | −0.06 (p 0.77) | −0.13 (p 0.36) |
| addressing | release | −0.05 | 0.77 | −0.18 / −0.18 | −0.16 (p 0.39) | −0.08 (p 0.56) |
| reciprocity | ECI | +0.02 | 0.73 | +0.15 / +0.17 | +0.03 (p 0.71) | +0.04 (p 0.68) |
| reciprocity | release | −0.01 | 0.84 | −0.07 / −0.06 | +0.08 (p 0.30) | +0.02 (p 0.77) |
| prosocial | ECI | +0.29 [+0.20, +0.38] | 0.015 (0.09) | +0.31 / +0.30 | +0.24 (p 0.013) | +0.14 (p 0.17) |
| prosocial | release | +0.17 | 0.20 | +0.24 / +0.24 | +0.25 (p 0.010) | +0.28 (p 0.0015) |

Effect sizes are small. On the per-period-demeaned per-agent scatter, every one of the six has |ρ| ≤ 0.27 and R² ≤ 0.05.

**Reading.**
- No scaling law survives correction. The two nominal ECI effects have Holm p = 0.09.
- Both shrink a lot without OpenAI agents, and the addressing effect disappears within Anthropic. This points to a lab or style effect: OpenAI models are high on ECI, rarely name other agents (o3, GPT-5, GPT-5.4, GPT-5.6 Sol), and say "please" a lot (GPT-5.6 Terra did so in 200 of its 271 messages, often in directives).
- Reciprocity is flat everywhere.
- Cleaning barely changes anything.
- One lead: request and division-of-labour language rises with release date within Anthropic and among non-OpenAI agents. The no-OpenAI split was chosen after seeing the results, and the marker is dominated by "please" and "claim[ed]".

**Caveats.** n = 38 agents, about 12 per lab. Tenure and verbosity are not controlled. Regex markers conflate verbal tics with cooperation. Mention detection undercounts nicknames. ECI is each model's best-setting score, not necessarily the setting the village ran. Gemini 2.5 Pro joined before its GA build. Clustered regressions and leave-one-agent-out were left out of the pilot.

**Worth pursuing further? Maybe, but not as a scaling law.** A full study needs:
- lab fixed effects with regressions clustered by agent
- tenure and verbosity controls
- LLM-coded cooperative acts (help offered or accepted, handoffs completed) instead of keyword markers
- a pre-registered within-lab comparison across Anthropic tiers
- responsiveness and idea-uptake measures
- leave-one-agent-out checks
- more agents per lab

**Files.** `village_tools/scaling.py` (rerun: `uv run --with numpy --with scipy --with matplotlib python scaling.py`, about 2.5 min), `village_tools/model_metadata.csv`, and in `out/scaling/`: `results.json`, `per_agent.csv`, `per_agent_period.csv`, `scaling_scatter.png`.

**`model_metadata.csv` is not in git.** `*.csv` is gitignored, and data files are never committed, so the rerun needs this file first. Without it, `scaling.py` stops with an error that names the missing file. Copy it from the original author's checkout, or rebuild it by hand. It is a UTF-8 CSV with a header row and one row per village agent. Every analysed agent needs a row. Columns, in order (the script reads only those marked *):

- `name`* is the agent's display name, exactly as it appears in the village data.
- `model_string` is the model id. `lab`* is the developer.
- `joined` and `left` are when the agent joined and left the village.
- `release_date`* is ISO `YYYY-MM-DD`: the model string's date suffix if it has one, otherwise Epoch's date. `release_source` records where it came from. `announce_date_crosscheck` and `announce_crosscheck_source` give the announcement date as a cross-check.
- `eci`* is the Epoch Capabilities Index, from Epoch AI's `eci_scores.csv` (this report used the file retrieved 2026-10-03). `eci_ci90_low` and `eci_ci90_high` are its 90% interval. `eci_epoch_label` is the model's name in Epoch's file. `eci_source` and `eci_retrieved` record where and when the value was taken.
- `flag` and `eci_notes` are free-text caveats.

In `release_date` and `eci`, `unknown` or a blank means missing. The `scaling.py` docstring has the same list.
