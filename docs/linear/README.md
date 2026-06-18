# Linear import bundle

Machine-readable companion to the Outline PR-triage gameplan
**"iai — PR Triage → Linear"**
([Overview & Linear Import Guide](https://outline.baffler.zone/doc/overview-amp-linear-import-guide-2QqqGdNnKt)).

It turns the triage of the 32 open PRs (#1–#32) and their ~203 unresolved
review-bot comments into a deterministic import for [Linear](https://linear.app).

## What's here

- **`issues.json`** — one object per issue (47 total) plus the project list,
  cross-cutting labels, and priority mapping an importer needs. This is the
  source of truth for the import; prefer it over re-reading the prose docs.

## Shape of `issues.json`

```jsonc
{
  "meta":     { /* counts, priority map, field guide, source links */ },
  "projects": [ { "key", "name", "doc", "prs", "issueCount" } ],   // 4
  "labels":   [ "codex", "claude", ... ],                          // 12 cross-cutting
  "issues":   [ { /* see below */ } ]                              // 47
}
```

Each issue object:

| field | meaning |
|-------|---------|
| `id` | Stable triage id (`H/I/K/C` + number), traces back to the gameplan docs |
| `title` | Issue title (no priority prefix) |
| `project` | Exact Linear project name to assign |
| `priority` | Gameplan tag `P1`/`P2`/`P3` |
| `linearPriority` | Linear API integer — `1`=Urgent, `3`=Medium, `4`=Low |
| `labels` | Subset of the 12 canonical labels |
| `type` | Optional non-cross-cutting category (`chore`/`perf`/`build`/`refactor`) or `null` |
| `prs` / `prLinks` | Source PR numbers and full GitHub URLs |
| `reviewers` | Which bot raised it: `gemini` and/or `codex` |
| `impact` / `affected` / `source` | Structured detail |
| `body` | Ready-to-paste Markdown description (impact + affected + source + PR links) |

## Totals

- **4 projects** — Capture Hooks (Claude + Codex) · Transcript / Session Import ·
  Packaging & Installation (Homebrew) · CLI Help Text & Docs
- **12 labels** — `codex` `claude` `data-loss` `crash` `idempotency` `py311`
  `homebrew` `docs` `help-text` `shell-script` `daemon-lock` `store-path`
- **47 issues** — by priority **P1=15 · P2=28 · P3=4**; by project **14 / 16 / 9 / 8**

> The gameplan's Overview prose says "P1=16 · P2=25 · P3=6" — that's a stale
> aggregate miscount. The authoritative per-issue priority tags (and the
> Overview's own per-issue tables) sum to P1=15 · P2=28 · P3=4, which is what
> `issues.json` encodes. See `meta.sourceCountDiscrepancy`.

## Import it (Linear MCP / Option A)

In a Claude session with the Linear MCP connected, fill in your team name and paste:

```text
You have the Linear MCP connected. Read docs/linear/issues.json from
gardinermichael/iai-personal-memory-engine (or I'll paste it).

In my Linear team "<YOUR TEAM NAME>":

1. Create the 4 projects named exactly as in `projects[].name`.
2. Create the 12 cross-cutting labels in `labels[]`.
3. For each object in `issues[]`, create one Linear issue in the project named
   by its `project` field. Set the title from `title`, Priority from
   `linearPriority` (1/3/4), apply the labels in `labels`, and use `body` as the
   description (it already contains impact, affected file:line, reviewer
   attribution, and linked source PRs).

Match on the `id` field so re-runs update rather than duplicate. Report how many
projects, labels, and issues you created, grouped by project.
```

Don't have Linear MCP handy? Every issue's `body` is ready to paste by hand —
the summary tables in the Outline docs are your checklist.
