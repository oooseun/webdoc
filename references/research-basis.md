# Research Basis

Accessed and synthesized on 2026-05-26.

## Current Pattern

- Claude Artifacts are the official rich-output precedent for substantial standalone content such as documents, single-page HTML, websites, React components, dashboards, and reusable app-like artifacts.
- Claude/Codex skills are the right durable layer for repeatable behavior because they package instructions, scripts, references, and assets that can be invoked when relevant.
- Hooks can remind, enforce, or run deterministic scripts, but full automatic site generation should stay conservative because hooks cannot reliably judge sensitivity, provenance, or user intent.
- Subagents are appropriate for presentation work after the canonical document is stable because they preserve main-agent context and keep layout/verification separate from analysis.
- Static serving should bind to loopback, use explicit manifests, avoid symlinks, avoid silent port drift, and clean up only owned processes.
- User feedback gathered in the website should be persisted to local agent-readable state. A local media-triage tool is the reference pattern: browser actions POST to a localhost API, the server validates and writes durable DB/file state, and the agent can inspect that state directly.
- Code-created visuals are the reliable path for agent docs: the model should generate inspectable chart/diagram/animation specs, render them, then critique them. Current research shows LLMs can generate simple charts and structured diagrams, but correctness and subtle visualization-rule adherence still need explicit prompting, examples, and rendered validation.
- For charts, separate data transformation from visual encoding. Microsoft Data Formulator's architecture is the reference pattern: UI/natural-language intent, Vega-Lite spec, separate data transformation, then chart rendering.
- For diagrams, prefer structured intermediate representations and deterministic renderers. Research on Mermaid/software diagrams finds syntax can be strong while structure/semantics still vary by model, so rendered validation matters.
- For animations, use code-native formats such as HTML/SVG/CSS/JS, Animated Vega-Lite, or Manim. The most credible systems use a pipeline from source text to scene plan to executable animation code.

## Source Map

- Claude Artifacts help: https://support.claude.com/en/articles/9487310-what-are-artifacts-and-how-do-i-use-them
- Claude Artifacts GA blog: https://claude.com/blog/artifacts
- Claude Agent Skills blog: https://claude.com/blog/skills
- Claude Code skills docs: https://code.claude.com/docs/en/skills
- Claude Code hooks docs: https://code.claude.com/docs/en/hooks
- Claude Code subagents docs: https://code.claude.com/docs/en/sub-agents
- OpenAI Codex customization: https://developers.openai.com/codex/concepts/customization
- OpenAI Codex skills: https://developers.openai.com/codex/skills
- OpenAI Codex hooks: https://developers.openai.com/codex/hooks
- ChatGPT Canvas help: https://help.openai.com/articles/9930697
- Python `http.server`: https://docs.python.org/3/library/http.server.html
- Node `net.Server`: https://nodejs.org/api/net.html
- Vite server options: https://vite.dev/config/server-options
- Microsoft Data Formulator: https://www.microsoft.com/en-us/research/blog/data-formulator-exploring-how-ai-can-help-analysts-create-rich-data-visualizations/
- Prompt4Vis: https://link.springer.com/article/10.1007/s00778-025-00912-0
- Charting the Future: https://aclanthology.org/2025.coling-main.501/
- Evaluating LLMs for visualization generation and understanding: https://link.springer.com/article/10.1007/s44248-025-00036-4
- VIS-Shepherd: https://arxiv.org/abs/2506.13326
- Vega-Lite docs: https://vega.github.io/vega-lite/docs/
- Animated Vega-Lite: https://vis.mit.edu/pubs/animated-vega-lite/
- DiagrammerGPT: https://arxiv.org/abs/2310.12128
- MermaidSeqBench: https://arxiv.org/abs/2511.14967
- Manimator: https://arxiv.org/abs/2507.14306
