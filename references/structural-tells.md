# Structural AI-writing tells and the second-pass audit

The main `references/avoid-ai-writing.md` ruleset is mostly word- and phrase-level. It catches "delve" and em dashes. It misses the tells that live in sentence *structure* — the rhythm, not the vocabulary. Those are the ones a human reader spots on sight while an automated wordlist waves them through.

This file covers that gap. It has three parts: the enforced audit (the actual fix), the pattern catalogue (what to look for), and the native linter that encodes the high-precision subset.

## Why a wordlist is not enough

A wordlist says "avoid negative parallelism." It does not count. So a document can stack twenty instances of "X, not Y" and pass, because each individual one is on no banned list. The fix is not a longer wordlist. It is a verification step that counts and caps. That is the single thing the popular anti-AI-writing tools lack, and it is why they under-catch.

## The second-pass audit (run before you serve)

After the draft reads clean, run these counted checks over your own prose. Each is a number you can actually evaluate, not a vibe.

- **Negative-parallelism count.** Count "X, not Y" / "it does not X; it Y" / "not just A, but B" per paragraph. One is fine. Two or more in a paragraph means rewrite to the positive claim.
- **Stacked negation.** Flag any run of two or more negations climbing to an abstraction ("Not a tool. Not a feature. A revolution."). Collapse to one plain sentence.
- **Tricolon cap.** At most one rule-of-three per passage. Count the parallel triplets; vary or cut the rest.
- **Signpost scan.** Search for openers that announce instead of state: "Below is", "Here is how", "Here's the thing", "What follows is". Delete the announcement.
- **Aphorism scan.** Search for "the X of Y" noun-chains ("the power of", "the art of", "the future of"). Replace with the concrete claim.
- **Punchline check.** Find short standalone sentences placed after a long one for drama. Cut them and let the result stand.
- **Staccato and burstiness check.** Two failure modes. If every sentence is the same length, break the rhythm. The more common one after an over-eager humanizer pass: a run of clipped short sentences ("Amylose is long. It is straight.") reads as manufactured drama. Connect them. Do not chop prose to look varied; that is its own tell.

The principle for thresholds: flag clustering and cross-passage repetition, not isolated use. A single contrast is good writing. Twenty of them is a tell.

## Pattern catalogue (before to after)

Ordered by how often they slip through. Items marked NET-NEW are weakly covered or absent in the baseline wordlist.

| Pattern | Before | After |
|---|---|---|
| Stacked negation (NET-NEW) | This isn't just a tool. It's not a feature. It's a revolution. | This tool changes how the team ships. |
| Single negative parallelism | Success isn't about luck; it's about preparation. | Preparation drives success. |
| Tailing / clipped negation (NET-NEW) | The list updates automatically, no refresh needed, no guessing. | The list updates automatically when the selection changes. |
| Rule-of-three triplet | It's fast, scalable, and secure. | It handles 10k req/s and fails over without dropping connections. |
| Ascending tricolon / climax | It's not a checklist, not a process, but a philosophy. | Treat it as a habit, not a one-time checklist. |
| Reveal signpost (NET-NEW) | Here's the thing: most teams skip testing. | Most teams skip testing. |
| Organizational signpost (NET-NEW) | Below is a breakdown of how the pipeline is organized. | The pipeline has three stages: |
| Definitional opener (NET-NEW) | Advocacy isn't a content problem. It's a distribution problem. | Advocacy fails because no one shares the content. |
| Generic temporal opener (NET-NEW) | In today's fast-paced landscape, staying ahead matters more than ever. | (delete; open on the actual point) |
| "The X of Y" aphorism (NET-NEW) | This is about the power of community and the art of connection. | The community helps members find jobs. |
| Manufactured punchline (NET-NEW) | ...and the team shipped twice as fast. That's the real revolution. | (cut the punchline; the result stands on its own) |
| Balancing hedge / false antithesis | While AI offers efficiency, it also poses challenges. | (commit to a claim, or give the specific tradeoff with a number) |
| Low burstiness (NET-NEW, quantitative) | (every sentence 12 to 16 words, same shape) | (mix a 3-word sentence with a 30-word one) |

## The native linter

`scripts/lint_prose.py` encodes the high-precision, deterministic subset of the above as a gate. It is pure Python (no `brew install`, runs anywhere Python 3 does), markdown-aware (strips fenced code and inline code first), and reads its rules from `lint/rules.json`.

Severity maps to action:
- **error (blocks the build):** stacked negation (counts sentence-initial "Not …/No …" negations, two or more per paragraph), signpost openers (pure announcers like "Below is", "Here's the thing", "Let's walk through" — not "The following" or "Now that we", which read as ordinary prose), and em/en dashes plus unspaced double-hyphens (numeric ranges like 10–20 or 14–28× are exempt). High precision, so blocking is safe.
- **warning / suggestion (surfaced, non-blocking):** negative parallelism (a single "X, not Y", "rather than", "instead of"; leading adverbs like "However, not …" are exempt), staccato (a run of two or more very short sentences), tricolon (an "a, b, and c"/"or" triad; suggestion), "the X of Y" aphorism, AI vocabulary (matched on stems, so inflections like "leveraging" and "utilization" count), generic temporal openers ("in today's", "in an era/age/world of", "more than ever", "fast-paced"), and the "While X, it also/still Y" balancing hedge. These have real false-positive rates, so they advise rather than block.

`create_site.py` runs it automatically before building and refuses to emit a site on an error. Run it standalone with `python3 scripts/lint_prose.py file.md` (add `--warn-only` to report without failing, `--json` for machine output). Edit `lint/rules.json` to tune patterns or severities; no code change needed.

Some checks stay human judgment: a single clipped sentence used for effect, and manufactured-punchline placement, are hard to gate without false positives, so the audit catches what the linter cannot. The linter is the floor, the audit is the ceiling.

## Sources

- Wikipedia, "Signs of AI writing" (WikiProject AI Cleanup), CC BY-SA. Baseline; documents negative parallelism, rule of three, and several formatting tells, lightly.
- matteoroversi/anti-ai-rhetoric (MIT). The counted second-pass audit approach above is adapted from its verification passes; it is the one surveyed tool that enforces rather than lists.
- Colin Gorrie, "Why ChatGPT writes like that" (deadlanguagesociety.com). Linguistic framing: the tell is explicit negation, where a human tends toward the implicit form.
- Pangram, "Comprehensive Guide to Spotting AI Writing Patterns"; bloomberry.ai, "AI First Lines" (signpost-opener taxonomy); refine.so on negative parallelism; huntingthemuse.net on staccato closers.
- Kim et al. (2024), "Threads of Subtlety: Detecting Machine-Generated Texts Through Discourse Motifs" (arXiv:2402.10586): structural variability resists paraphrase better than surface features, which is the case for auditing structure over vocabulary.
