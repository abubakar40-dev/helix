# Helix — Build Specification for OpenCode

You (OpenCode) are building **Helix**, a personal multi-agent AI system for a single
developer on his own laptop. Read this whole file before running anything. Then work
autonomously: plan, run commands, read the actual error output when something fails,
fix it, retry, and log progress. Only stop and ask the human when a decision is
genuinely ambiguous or a command is destructive/irreversible.

## 0. Ground rules (do not violate these)

- **Free tools/models only.** No paid API keys, no paid subscriptions. If something
  free isn't available, look for a free/open-source GitHub alternative before
  suggesting anything paid.
- **Minimal code.** Prefer configuring/wiring existing tools (Hermes Agent's built-in
  tools and skills, OpenRouter free models, existing open-source repos) over writing
  new code. Use the `ponytail` OpenCode plugin philosophy: lazy, not negligent — don't
  pad code, but don't skip error handling either.
- **Never fabricate output.** If a command fails, show the real error and the real fix.
  Never claim a file was created/fixed without actually verifying it (this project
  exists specifically because the old Jarvis lied about file state).
- **Ask before destructive actions.** Deleting files/directories, overwriting configs,
  `rm -rf`, force-pushing, killing running services — confirm with the human first.
- **Log as you go.** Append a dated entry to `~/helix/docs/PROGRESS.md` after each
  meaningful step: what you did, what worked, what didn't, what's next. This file is
  how the human (and future OpenCode sessions) catches up without re-reading a full
  chat transcript.

## 1. Environment (already true, verified — don't redo this)

- Laptop: Ubuntu 26.04.1 LTS, **no GPU**, 6.9GB RAM (tight — usually ~1GB free),
  135GB disk (~70GB free). Design for low memory: avoid running multiple heavy
  local models at once; prefer cloud (free-tier) inference over local inference.
- `opencode` and `ollama` are installed.
- **Hermes Agent** (github.com/NousResearch/hermes-agent) is installed at `~/.hermes`
  and running as a systemd user service (`hermes-gateway`, survives logout via
  linger). This is Helix's "brain" / agent runtime — do not reinstall it or build a
  competing agent runtime. Build UI and glue code around it.
- Project root: `~/helix` (git initialized). Structure:
  ```
  ~/helix/
  ├── agents/        # notes per sub-agent (maps to Hermes profiles, see §3)
  ├── skills/         # only CUSTOM skills not already in Hermes's 54 bundled skills
  ├── ui/
  │   ├── agent-town/    # pixel-art office UI
  │   ├── voice-core/    # voice orb + Memory/Skills/Soul/Settings graph
  │   └── world-monitor/ # globe + map + headlines (wraps "God's Eye View")
  ├── bridge/        # small server: exposes live Hermes/kanban state to the UI
  ├── config/        # config templates, .env.example (NEVER real secrets)
  └── docs/           # this file, PROGRESS.md, requirements doc, research notes
  ```
- `~/helix/docs/helix_requirements.txt` (or similarly named) holds the original
  free-form requirements dump from the human — read it for full context on features
  not yet covered below (multi-agent CEO framing, barge-in interrupt, link-paste
  downloader, security/verification principles, etc.).

## 2. Hermes Agent — model & provider

- Auth: OpenRouter API key is already configured in `~/.hermes/.env`. Do not ask for
  or hardcode any other key inline in code — new keys go in `.env` files, never in
  source.
- Model policy: **online-first, no local-fallback model configured yet** (the human
  has no GPU; a local model is a later nice-to-have, not a blocker).
- Preferred free models, in priority order (re-verify availability — free/stealth
  models on OpenRouter rotate and disappear):
  1. `stealth/space-bunny-alpha` — best available for coding/agentic work right now,
     but it's an anonymous stealth-preview model and can vanish or go paid without
     notice. Treat as primary, not as guaranteed.
  2. `nvidia/nemotron-3-ultra:free` — solid reasoning fallback.
  3. `openrouter/free` — OpenRouter's own auto-router; safest fallback since it
     always resolves to *something* free with tool-calling support.
  4. Small fast free models (e.g. `*-flash:free`, `*-lightning:free` variants) —
     fine for lightweight/routing sub-agents, not for coding.
- Set per-profile via `hermes config set model.default "<model>"` (CEO/default
  profile) or `hermes -p <profile> config set model.default "<model>"` (sub-agent).
- Check `hermes fallback --help` and wire an automatic-failover chain across the
  models above so a rate-limited/deprecated model doesn't hard-fail a session.
  Confirm the exact flags before using them — don't guess the syntax.
- **Known upstream bug**: as of Hermes v0.21.5, the Kanban dispatcher crashes when
  spawning workers for **named profiles** (only the `default` profile's kanban
  workers run correctly) — error is `ModuleNotFoundError: No module named
  'hermes_cli'`. This matches NousResearch/hermes-agent issue #36981. Before
  building sub-agent task delegation on Kanban:
  1. Run `hermes update` and retest a named-profile kanban task
     (`hermes kanban create "test" --assignee <profile>`, then
     `hermes kanban dispatch`).
  2. If still broken, fall back to the `delegate_task` tool (in-process, no
     subprocess spawn, so it's unaffected) for CEO → sub-agent handoff, and revisit
     Kanban once upstream fixes it. Note the workaround and the bug link in
     PROGRESS.md so it's easy to revisit.

## 3. Sub-agents (Hermes profiles)

Each sub-agent is a full Hermes **profile** (own config, `.env`, `SOUL.md`, memory,
tool selection) — not a lightweight preset. Already created:

- `default` — display-renamed to "helix". This is the CEO. It should mainly
  delegate/orchestrate, not do heavy execution itself. Once delegation works
  reliably, trim its own tool list similarly to the sub-agents below.
- `research` — cloned from default. Tools disabled: Terminal, Code Execution, Image
  Generation, TTS, Computer Use. Web search provider set to Tavily. `SOUL.md`
  defines: cite sources with plain-text URLs (not markdown hyperlinks — the human
  wants copy-pasteable links), flag conflicting/thin evidence, prefer primary
  sources, ask one clarifying question if the request is ambiguous, and a fixed
  report format (answer → findings w/ links → confidence → what couldn't be
  verified).

Still to create (same pattern: `hermes profile create <name> --clone --description
"..."`, trim tools to just what the role needs, write a role-specific `SOUL.md`,
verify with a smoke-test chat before wiring it into delegation/kanban):

- `pc` — desktop control (terminal, computer-use, file ops). This is the most
  sensitive profile — its `SOUL.md` must require confirmation before destructive
  commands (matches the human's explicit requirement).
- `dev` — coding tasks, should primarily drive **OpenCode itself** (Hermes already
  bundles an `opencode` skill — use that instead of reimplementing code-execution
  logic).
- `vision` — image/vision analysis, and eventually the Easy Diffusion / MeiGen
  image-video generation wiring (see §5).
- `comm` — messaging channels (WhatsApp via `hermes channels add whatsapp`, plus
  the `42-evey/hermes-plugins` bridge pack for Discord/voice/WhatsApp/inter-agent
  bridging once core agents are stable).

Character names (Alice/Bob/Carol/Dave-style) are a **UI-only** concern for Agent
Town — map them to these profile ids in the UI layer, don't rename the profiles
themselves (profile ids must stay lowercase/alphanumeric).

## 4. UI — three panels, inspired by (not copied from) a reference app

The human showed screenshots of a third-party app called "Stonic AI" as a style/UX
reference. **Do not copy its name, logo, or branding.** Build an original
implementation of the same three panel concepts, genuinely wired to live Hermes
data (not mocked):

1. **Agent Town** — pixel-art top-down office. Each sub-agent profile has a desk and
   a live status (idle/working/blocked) sourced from Hermes/Kanban state via the
   `bridge/` server. Lightweight 2D engine (Phaser.js or PixiJS via CDN — check what
   `frontend-design` conventions this environment expects before hand-rolling CSS).
2. **Voice Core** — a circular "thinking" node graph (Memory / Skills / Soul /
   Settings branches into a central orb) with a Start/Stop control. The human
   explicitly wants **barge-in**: the ability to interrupt Helix mid-speech, which
   the old Jarvis lacked. Check `bielcarpi/hermes-live-voice` (interruptible voice)
   before building this from scratch.
3. **World Monitor** — 3D globe + 2D map + live headlines. The human already has
   `bilawalsidhu/gods-eye-view` installed locally (open-source, live
   aircraft/ship/satellite/earthquake/public-cam globe) — embed/wire it in rather
   than building a new globe.

`bridge/` is the one piece of custom glue code this project actually needs: a small
local server exposing Hermes/Kanban state (profile status, current tasks, chat
events) to these three UI panels, e.g. via WebSocket. Keep it minimal. Do not expose
it on `0.0.0.0` or any non-localhost interface — no auth is assumed on Hermes's own
dashboard plugin routes.

## 5. Deferred / not yet started (don't build until asked)

- Easy Diffusion local image gen (CPU-only, so treat as an offline/low-res fallback,
  not primary — primary image/video gen should go through
  `jau123/MeiGen-AI-Design-MCP` on a free cloud model, since there's no GPU).
- Obsidian integration.
- Link-paste-to-download UI feature (yt-dlp-based).
- Additional free skills the human has shortlisted: `elkimek/honcho-self-hosted`
  (memory layer), `screenpipe/screenpipe` (on-device screen/audio memory),
  `drakulavich/kesha-voice-kit` (local STT/TTS), `robbyczgw-cla/hermes-web-search-plus`,
  `ZeroPointRepo/youtube-skills`, `currentslab/news-api-skills`.

Pick these up only when the human asks, or when a task genuinely needs one — don't
front-load installs "just in case" (matches the free-only / minimal-code principle).

## 6. What to do right now

1. Read `~/helix/docs/helix_requirements.txt` in full for anything not summarized
   above.
2. Resolve the Kanban named-profile bug (§2) — try `hermes update` first, verify,
   record the outcome in PROGRESS.md.
3. Get CEO → `research` delegation working end-to-end (via Kanban if fixed, else
   `delegate_task`), with a real test task, before creating more sub-agent profiles.
4. Create the `pc` profile next (highest-value, matches the original "Jarvis sucks
   at file ops" complaint) — clone, trim tools to terminal/computer-use/file ops,
   write a `SOUL.md` that requires confirmation before destructive actions, smoke-test
   it with a simple, safe, read-only task first.
5. Only after CEO + 2 working sub-agents: start on the `bridge/` server and Agent
   Town UI, so there's something real to visualize.

Update `~/helix/docs/PROGRESS.md` after each of the above with what happened.
