# Before and after: editing the AI tells out of a draft

webdoc runs a prose linter every time it builds. The linter looks for the structural tells that a plain word list misses. It blocks the build on the few it is most sure about and leaves the rest as advice for a person to weigh. This page takes one slopped product announcement, shows what the linter reports, then shows the rewrite that builds clean.

## The draft, before

A product announcement, written the way a language model tends to write one (`draft-before.md`):

```text
# Introducing Pulse: The Future Of Analytics

In today's fast-paced world, data matters more than ever. That's why we are
thrilled to announce Pulse — a robust, seamless analytics platform that will
transform how your team works with data.

Here's the thing: Pulse isn't just a dashboard, it's a complete reimagining of
analytics. We leveraged cutting-edge technology to build a truly comprehensive
solution that empowers teams to delve into their data and unlock insights.

Pulse offers powerful dashboards, real-time updates, and seamless integrations.
No setup. No friction. No limits.

While Pulse is powerful, it also stays simple. At the end of the day, we believe
in the power of data to change everything.
```

## What the linter reports

Running `python3 scripts/lint_prose.py draft-before.md` prints:

```text
  WARNING    L   1  aphorism: Aphorism formula 'The Future Of'
  WARNING    L   3  ai-vocab: AI-vocabulary 'robust'
  ERROR      L   3  em-dash: Em/en dash or double-hyphen ('—')
  WARNING    L   3  generic-temporal-opener: 'In today's'
  WARNING    L   5  ai-vocab: AI-vocabulary 'leveraged'
  WARNING    L   5  negative-parallelism: near 'isn't just a dashboard, it's'
  ERROR      L   5  signpost-opener: 'Here's the thing'
  WARNING    L   7  ai-vocab: AI-vocabulary 'seamless'
  WARNING    L   7  staccato: 3 short sentences in a row
  ERROR      L   7  stacked-negation: 3 sentence-initial negations in one paragraph
  SUGGESTION L   7  tricolon: 'dashboards, real-time updates, and ...'
  WARNING    L   9  aphorism: Aphorism formula 'the power of'
  WARNING    L   9  balancing-hedge: 'While Pulse is powerful, it also ...'
prose lint: 3 error(s), 10 advisory  (draft-before.md)
```

Three findings are errors, so the build stops. The ten advisories do not stop it; they are there for a person to judge and override.

## The same announcement, after

Stored as `draft-after.md`, the rewrite keeps every fact and drops the tells:

```text
# Pulse: a faster way to read your analytics

Pulse is an analytics dashboard for teams that have outgrown exported
spreadsheets. It loads a year of event data in about two seconds and refreshes
as new events land, so the figure you cite in a meeting matches the figure on
the screen.

The dashboard ships with a live event stream, a funnel builder, and a retention
grid. They share one query engine, so a filter set in any view carries to the
rest. Connecting a data source takes a single API key and about a minute, with
no schema to define first.
```

This version reports one suggestion and no errors, so it builds. It says what Pulse is and what it costs, with numbers in place of adjectives.

## What changed, tell by tell

| Tell | What the linter flagged | The fix |
|---|---|---|
| Em dash | `Pulse — a robust, seamless platform` | split into two sentences |
| Signpost opener | `Here's the thing: Pulse isn't just...` | cut it; state the claim |
| Stacked negation | `No setup. No friction. No limits.` | `one API key and about a minute` |
| Negative parallelism | `Pulse isn't just a dashboard, it's...` | say plainly what Pulse is |
| AI vocabulary | `robust`, `seamless`, `leveraged`, `delve` | concrete verbs and nouns |
| Rule of three | `dashboards, updates, and integrations` | one specific claim |
| Generic opener | `In today's fast-paced world` | open on what the product does |
| Aphorism | `the power of data` | the concrete thing it does |
| Balancing hedge | `While Pulse is powerful, it also...` | lead with the point |

The errors are the high-precision tells: an em dash, a signpost opener, a stack of one-word negations. The linter blocks only on those. Everything else is a warning or a suggestion, because rhythm and word choice are judgment calls a writer should get to make.
