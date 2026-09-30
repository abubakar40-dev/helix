# Helix — Build Progress Log

Append-only dated log. Newest entries at the bottom of each day.

---

## 2026-09-29

### Step 1 — Read `HELIX_BUILD_SPEC.md` + `helix_requirements.txt`

**Done.** Both read in full.

Key context from `helix_requirements.txt` not covered by the build spec:

- §17 Gesture control (webcam finger-tip / fist / double-tap) is an **active** project
  in parallel — must stay lightweight when idle.
- §22 Security: permission levels, confirmation system, audit logs, prompt-injection
  defense, kill switch, safe mode. High-risk ops require confirmation. This feeds
  directly into the `pc` profile's `SOUL.md` (step 4).
- §28 Goal: one high-level objective ("research X → PDF + PPT on Desktop → verify →
  report") end-to-end without micromanagement, re-verifying with the user before any
  destructive command.
- §13 Browser: local headless Chromium (Hermes default). **No anti-detection /
  Camofox** — bot-evasion explicitly out of scope.
- §29 Four-question standard: improve HELIX / can it be local / can an existing tool
  do it / is it minimal.
- Closing notes: link-paste downloader via yt-dlp (already deferred in spec §5), and
  minimize data sent to Gemini (they're being surveilled). "Send as low data as
  possible" → prefer local/Ollama paths, don't ship large payloads to cloud.

### Environment verified (nothing assumed)

- `hermes` v0.21.5+3698.g6f7a799 (2026.9.24), git install, Python 3.14.7
- `hermes-gateway` systemd user service: **active**
- Profiles that exist: `helix` (display name of `default`) and `research`.
  Both currently on model `openrouter/free`.
- `~/helix` git repo exists, empty scaffolding dirs (`agents/ bridge/ config/ skills/ ui/`).

**Next:** step 2 — resolve the Kanban named-profile dispatcher bug (§2 of spec).

### Re-read of `helix_requirements.txt` (rewritten by the human)

Requirements file was reorganized (1115 → 833 lines). Re-read in full. Changes that
matter for the build order:

- **§19 now explicitly rules out CAPTCHA bypass and "out of sandbox" execution.**
  This resolves the one genuinely ambiguous item from the old draft — it's out of
  scope, so nothing to ask about. The real safety requirement that *does* remain is
  confirmation-before-destructive-action + verification (§20), which is what the
  `pc` profile's `SOUL.md` needs.
- §2/§5.2: "local-first" is split in two — PC control/files stay local, but the
  *reasoning model* is online-first (free cloud) with local only as fallback.
  Rationale is the hardware: CPU-only, <7GB RAM. The old blanket "local-first"
  reading was wrong for the model layer.
- §25 phases match the build spec's §6 order. Phase 1 = Kanban bug, Phase 2 = `pc`
  profile, Phase 6 = UI.
- §3.2 / §17: reinforce reuse-before-build and the shortlist table. §17 marks
  `DietrichGebert/ponytail` as "Not yet installed" — worth a look later, but it is
  an OpenCode-side convenience, not a blocker for any step in §6.

No contradictions found between the rewritten requirements and `HELIX_BUILD_SPEC.md`.

### Step 2 — Kanban named-profile bug: **RESOLVED (fixed upstream by `hermes update`)**

`hermes update` initially hung, but it had actually completed the git pull and was
blocked on an interactive prompt. `--plan` showed a half-finished run; re-running with
`--yes` finished cleanly. It drained and restarted `hermes-gateway` (standard update
path, no data loss).

| | before | after |
|---|---|---|
| Hermes version | `v0.21.5+3698.g6f7a799` | `v0.21.5+4533.g39faafb` |
| upstream commit | `6f7a7991` | `39faafb6` |

**The `ModuleNotFoundError: No module named 'hermes_cli'` bug is GONE.** A kanban
task assigned to the named `research` profile spawned a worker process and ran for
40s before failing — i.e. dispatch itself works now. Upstream fixed it. Issue link
for future reference: NousResearch/hermes-agent#36981.

**But a different, previously-hidden bug surfaced** and needed fixing before
delegation worked:

```
⚠️  No API key found for provider 'openrouter'. Run 'hermes model' to choose a provider...
```
Reproduced 2/2 (task `t_bd6b0d88`, runs 8 and 9, then `gave_up`).

Root cause — **the build spec's premise was wrong**. Spec §2 says "OpenRouter API key
is already configured in `~/.hermes/.env`". It is not. The key exists in exactly one
place: `~/.bashrc`, exported into interactive shells. Verified:

- `~/.hermes/.env` → no `OPENROUTER_API_KEY`
- `~/.hermes/profiles/research/.env` → no `OPENROUTER_API_KEY`
- systemd user env → no `OPENROUTER_API_KEY`
- `~/.bashrc` → has it

Why that broke only Kanban and not the CLI: `agent/secret_scope.py:get_secret()`
resolves credentials profile-scoped on purpose. When a secret scope is bound and
multiplexing is active, a miss returns empty **rather than falling back to
`os.environ`** — so the `research` worker correctly refused to borrow the default
profile's key from the inherited shell env. Working as designed; the profile simply
had no key of its own.

**Fix:** appended `OPENROUTER_API_KEY` to `~/.hermes/.env` and
`~/.hermes/profiles/research/.env` (perms `600`, secret not echoed to output).
Key relocated, not newly created or hardcoded into source.

**Verification** (task `t_a6ed3fec`, assigned to `research`, dispatched via Kanban):

```
status: done
  #10  completed  @research  52s
        → Smoke test v2 — replied with exactly KANBAN_OK as instructed.
```

Clean on the first run — no crash, no retry. **CEO → research delegation over Kanban
is working.**

⚠️ **Latent issue, not fixed yet:** the systemd `hermes-gateway` does not have
`OPENROUTER_API_KEY` in its environment either. Anything routed through the gateway
(Telegram/WhatsApp turns) will fail the same way until that env is wired up. Separate
from §6 — flagging so it isn't rediscovered later.

Non-blocking warnings seen during update, noted for later: platform `teams` and
`google_chat` reference toolsets Hermes reports as unknown. Cosmetic, and neither
platform is in use.

### Step 3 — CEO → research delegation: **WORKING end-to-end via Kanban**

Two things were needed.

**(a) The CEO couldn't see Kanban at all.** Asked the CEO to list its
kanban/delegation tools, it returned a list with no kanban entry. Cause:
`platform_toolsets.cli` for the default profile was
`[browser, clarify, code_execution, computer_use, connections, cronjob, delegation,
file, image_gen, memory, session_search, skills, terminal, todo, tts, vision, web]`
— `delegation` (the `delegate_task` tool) was on, `kanban` was not. The CEO could
only ever have used the fallback path. Fixed with:

```
hermes tools enable kanban --platform cli      # ✓ Enabled: kanban
```

Re-read the config to confirm rather than trust the success line — `kanban` is now
in `platform_toolsets.cli`.

**(b) Real test task, CEO-driven.** Asked the CEO agent to create the task itself
(not me calling `kanban create` by hand), with a genuine research question:

```
hermes -z "Use the kanban tools to create ONE task assigned to the 'research'
profile with workspace 'scratch'. Title: 'Helix delegation end-to-end test'.
Body: 'Find out which Linux distribution ships the most user-friendly default
desktop for a single developer on a laptop...' Do NOT run the dispatch yourself."
→ Task created: `t_812f4800`. Not running dispatch — left to you.
```

Then `hermes kanban dispatch` → spawned to `research` → done in 70s on the first
run, with a real artifact attached:

```
status: done
  #11  completed  @research  70s
      attached 'recommendation.txt' (744 bytes)
```

**Verification (§20 — did not trust the summary, checked the file):**
`~/.hermes/kanban/attachments/t_812f4800/recommendation.txt` exists, 744 bytes,
readable, real sourced content, and the URLs are **plain text** not markdown links —
matching the `research` profile's `SOUL.md` rule. The sub-agent's personality is
surviving the Kanban round-trip, not just producing text.

So the delegation chain is now real: CEO decides → Kanban board → named profile
worker → verifiable artifact on disk. No `delegate_task` fallback needed, since the
upstream bug is fixed.

Memory during the test: 3865 MB available of 7041 MB — fine for one worker at a
time, which is all this needs (§2's RAM constraint is respected for now).

### Step 4 — `pc` profile: **created, working, but found a real security gap**

**Model availability re-verified against the live OpenRouter catalog (460 models) —
the spec's priority list has already drifted:**

| Spec §11 | Status now |
|---|---|
| `stealth/space-bunny-alpha` (P1) | **available** |
| `nvidia/nemotron-3-ultra:free` (P2) | **GONE** — renamed to `nvidia/nemotron-3-ultra-550b-a55b:free` |
| `openrouter/free` (P3) | available |

Used the renamed nemotron as fallback #1. This is exactly the "free models rotate and
disappear" risk the spec warned about, confirmed in practice.

**Created:** `hermes profile create pc --clone --description "..."`. `--clone` copies
`.env`, so the OpenRouter key fix from step 2 carried over automatically — verified the
key is present in the new profile's `.env` before doing anything else.

**Tools trimmed** to `clarify, computer_use, file, memory, skills, terminal, todo, vision`.
Disabled: `browser`, `code_execution`, `connections`, `cronjob`, `delegation`,
`image_gen`, `kanban`, `session_search`, `tts`, `web`. Re-read the config to confirm
rather than trusting the success line. Kept `clarify` on purpose — it's the tool the
agent would use to ask for confirmation.

**Model + fallback chain** (`hermes fallback add` is an interactive picker with no
flags, so used the config key directly, then confirmed via `hermes fallback list`):

```
Primary:   stealth/space-bunny-alpha  (via auto)
  1. nvidia/nemotron-3-ultra-550b-a55b:free  (via openrouter)
  2. openrouter/free                          (via openrouter)
```

**`SOUL.md`** written to `~/.hermes/profiles/pc/SOUL.md`, following the existing
`research/SOUL.md` shape. Requires explicit confirmation before deletion / `rm -rf` /
`sudo` / system config / credentials / bulk edits / force-push / killing services.
Explicit allowlist of safe ops. "Verify — never assume" section. Report format
mirrors the research agent's. Notes at `~/helix/agents/pc.md`.

**Smoke test 1 (read-only, as the spec requires):** asked for hostname/RAM/disk/CPUs.
All four correct — and I re-ran all four myself rather than trusting the report:
hostname `Wick`, RAM `6.9Gi`, disk `65G`, cpus `4`. Exact match.

**Smoke test 2 (real path, via Kanban):** task `t_877de103` → `pc` → completed in 23s
on `stealth/space-bunny-alpha`, claims re-verified independently. All four again exact.

### ⚠ Security finding — `pc` has no real destructive-action gate

I tested this instead of assuming the SOUL.md worked. It did not, for two separate
reasons, and the first one was **my own test harness**:

**1. `hermes -z` forces yolo.** `hermes_cli/oneshot.py:279` sets
`HERMES_YOLO_MODE=1` with the comment *"Non-interactive by definition — an approval
prompt would hang forever."* So my first delete test was auto-approved by the harness
regardless of any config. Not a production issue — but it means **one-shot testing
cannot evaluate safety behaviour at all.**

Secondary, still real: even knowing this, the agent rationalised rather than simply
refusing — *"No human was reachable to answer, so I relied on your original message,
which named the exact path and the word 'permanently' — that is the authorization I
acted on."* Prose in `SOUL.md` is not enforcement.

**2. The production path (Kanban) auto-approves too, and this one is real.**
`hermes_cli/kanban_db_dispatch.py` sets `HERMES_SESSION_SOURCE=kanban` but sets no yolo
flag, no single-query marker, and no gateway session. That context falls through
`tools/approval.py` to line 1015 — `return _approved()` — auto-approve when no human
is present.

Concretely: **when HELIX delegates a destructive PC task to `pc` over Kanban, Hermes
approves it automatically, and the only remaining protection is SOUL.md asking nicely.**
Confirmed a second time with a safe probe (`rm -rf /nonexistent-helix-safety-probe-xyz .`)
— the block that stopped it came from GNU `rm`'s own guard, not from Hermes.

**What already exists and is genuinely unbypassable** (so the fix is config, not code):
- `_floor_block` — the hardline deny floor, always evaluated first.
- `approvals.deny` rules (`_user_deny_block`) — documented as never bypassable, not
  even by `--yolo` or `approvals.mode: off`. Evaluated *before* the container fast
  path, so it holds regardless of backend.
- `approvals.mode` / `cron_mode` / `single_query_mode` / `unattended_mode` all exist;
  defaults are `smart` / `deny` / `deny` / `deny`.

**Not configured yet** — which deny rules to set is the human's call, not mine, so
asking rather than guessing. This is spec §19's "confirmation system / kill switch /
safe mode" and it is the gap the old Jarvis died of.

### Step 4b — `pc` safety gate configured and **verified end-to-end**

Human's decision: deny-list destructive ops, scoped to *catastrophic + privileged
only*; route ordinary single-file deletions through the OS trash instead of permanent
`rm`, so normal file management keeps working while true irreversibility is blocked.

**1. Trash-first is real, not assumed.** `gio` (glib 2.88.0) is present at
`/usr/bin/gio`. Round-tripped a probe file: it lands in
`~/.local/share/Trash/files/` with a matching `info/<name>.trashinfo` recording the
original path and deletion time. Genuinely recoverable.

**2. Hard deny-list** on the `pc` profile via `approvals.deny` (case-insensitive fnmatch
globs; blocks even under `--yolo` / `approvals.mode: off`):

```
*sudo *  *sudo  *rm -rf*  *rm -fr*  *rm --recursive*  *dd if=*  *dd of=/dev/*  *mkfs*
*git push --force*  *git push -f *  *git reset --hard*  *git clean -f*
*rm /etc/*  *rm /usr/*  *rm /bin/*  *rm /sbin/*
*rm ~/.ssh/*  *rm ~/.aws/*  *.ssh/id_rsa*  *.ssh/id_ed25519*  *.aws/credentials*
*credentials.json*
```

**3. Every pattern tested against the real runtime guard** via
`hermes -p pc approvals test` — not assumed. 13/13 deny (exit 3); `dd if=…` and
`mkfs…` are caught by the even-stronger hardline floor, the rest by the deny rules.

**4. Confirmed it didn't brick the agent** — these must still work, and do:
`rm <single file>`, `gio trash`, `ls`, `cat /etc/hostname`, `git status`, `grep`,
`mkdir`. All `allow` (exit 0).

**5. End-to-end through the actual Kanban path** (the one where I found the
auto-approve). Task `t_d43ced04`, assigned to `pc`, asked to run
`sudo rm -rf /tmp/helix-should-not-exist` and *not* to work around it:

```
Result: BLOCKED: this command matches the user-defined deny rule '*sudo *'
(approvals.deny in config.yaml). It cannot be executed via the agent — not even
with --yolo, /yolo, or approvals.mode=off. Do NOT retry or rephrase this command.
#13  completed  @pc  16s
```

Confirmed `/tmp/helix-should-not-exist` does not exist. **The gap is closed.**

**6. Trash-first verified end-to-end too.** Task `t_2a3c080d` asked `pc` to delete a
real test file. I checked all three claims myself rather than trusting the summary:

| claim | verified |
|---|---|
| source path gone | yes, `No such file or directory` |
| copy in trash, content intact | yes, 17 bytes, `helix trash test` |
| restore record with original path + time | yes, `Path=` and `DeletionDate=2026-09-29T09:31:04` |

`SOUL.md` updated: trash-first section, the hard-block notice ("do not try to work
around it with a different tool, script, or reworded command"), and trash added to the
safe-without-asking list.

### Step 5 — bridge + two-desk Agent Town: **BUILT AND VERIFIED LIVE**

Scope as given: bridge server + Agent Town with exactly two desks (research, pc),
prove the data path is real. Not mocked anywhere.

**Bridge** — `~/helix/bridge/server.py`, ~170 lines, **stdlib only** (verified by AST
scan: `non-stdlib: none`). No pip, no venv, nothing to keep in sync.

Design decisions, and why:

- **Reads `~/.hermes/kanban.db` directly via SQLite `mode=ro`.** No Hermes internals,
  no dashboard API, no auth, no polling of a subprocess. The board *is* the source of
  truth. Read-only is enforced, not just claimed — tested:
  `PASS: write refused -> attempt to write a readonly database`, and the target row
  was confirmed untouched afterwards.
- **SSE, not WebSocket.** The spec suggested "e.g. via WebSocket", but the UI only
  ever *receives* pushed state and every browser ships `EventSource`. A WebSocket here
  buys a dependency plus a handshake protocol for the identical result. `/events`
  pushes a snapshot when it changes; `/state` returns one on demand.
- Serves the UI itself from the same port — one process instead of two plus CORS.

**Status rules** (the only non-trivial logic, so `--selftest` covers it against a
throwaway DB with no Hermes involved — `selftest OK`):
`running` task → `working`; else any `blocked` task → `blocked`; else `idle`.

**Localhost-only — proven, not assumed.** `ss` shows `127.0.0.1:8787`, but that was not
trusted. Real test: loopback `200`, LAN address `192.168.100.97:8787` → `000`
(connection refused). Correctly unreachable off-box.

**Agent Town** — `~/helix/ui/agent-town/index.html`, one page, no build step, no
framework, no assets. Two desks only, as scoped. `helix` is in the payload but has no
desk yet. Includes the live event stream because it was already in the snapshot and it
is the most direct evidence the data path is real.

#### Verification: the UI tracked real Kanban activity, correlated to the DB

Installed an in-page poller (400ms) that recorded the *rendered* `data-status` and
task text, then dispatched two genuine tasks — one per desk. Correlated against the
authoritative `task_events` timestamps afterwards:

| real Kanban event | DB time (UTC) | UI recorded | lag |
|---|---|---|---|
| `t_54e56b99` claimed → research **working** | 04:52:27 | 04:52:28 | +1s |
| `t_54e56b99` completed → research **blocked** | 04:53:52 | 04:53:53 | +1s |
| `t_19d30ba8` claimed → pc **working** | 04:54:12 | 04:54:13 | +1s |
| `t_19d30ba8` completed → pc **idle** | 04:54:30 | 04:54:31 | +1s |

Full UI-recorded timeline, end to end, no manual reload:

```
04:52:19  research=blocked | pc=idle                 (baseline)
04:52:28  research=working t_54e56b99 | pc=idle
04:53:53  research=blocked | pc=idle
04:54:13  research=blocked | pc=working t_19d30ba8
04:54:31  research=blocked | pc=idle
```

`pc` finished in ~18s, faster than any external poll could catch — the in-page poller
caught it, which is exactly why the state had to be read out of the DOM rather than
`/state`. Both desks, both directions, all four transitions within 1s of the DB.

Screenshot taken mid-run confirms the rendered state, not just the DOM attribute:
research **WORKING** (green, left bar lit) showing `t_54e56b99`, pc **IDLE**, event
stream showing live `created`/`claimed`/`spawned`/`heartbeat` rows.

Task results also checked rather than trusted (§20):
- `t_54e56b99` (research) — real answer, 2 plain-text source URLs, per its SOUL.md rule.
- `t_19d30ba8` (pc) — claimed kernel `7.0.0-34-generic`, hostname `Wick`; both
  re-checked with `uname -r` / `hostname`, exact match.

Notes carried forward:
- A profile that just finished shows `blocked` if stale stuck tasks remain — true for
  `research`, from the pre-fix smoke tests. The desk names the stuck task so it is not
  ambiguous. Honest rather than smoothed over; refine only if it proves noisy.
- The bridge is **not** a systemd service, so it dies with the shell. Documented in
  `bridge/README.md` as a deliberate deferral, not an oversight — the gateway already
  survives logout via linger and the bridge can be wrapped the same way when wanted.

## 2026-09-30

### Housekeeping — archived stale debug tasks off the kanban board

`research` was permanently reading **blocked** in Agent Town because four crashed
smoke-test tasks were still sitting on the board from the pre-fix debugging.

**The list I was given had three; there were four.** Archiving only the three named
(`t_b7ca0dad`, `t_5296608b`, `t_f9172fab`) left `t_bd6b0d88` blocked, and the desk
still read `blocked` — verified in the rendered DOM, not assumed. That fourth task is
exactly the category described: the crashed named-profile smoke test from step 2. So
the third one was archived too, which is what actually reached the goal.

**Archive is not destructive — checked before relying on it.** `hermes kanban archive`
only sets `status='archived'`. Confirmed the row, title, body, events and workspace
reference all survive: `hermes kanban show t_b7ca0dad` still returns everything. There
is no `unarchive` subcommand, but this is a one-field status change, fully reversible.
The genuinely destructive flag is `--rm` (permanently deletes archived tasks) — not
used. Noted because "no unarchive" looked one-way at first glance and that was worth
knowing before running it four times.

**Verification.** `/state` and the rendered DOM both show all three profiles `idle`.
But the tab was opened *after* the change, so that is only a before/after — not proof
the desk is live rather than parked. So: dispatched a real task to `research` and
watched the in-page poller (300ms).

```
19:07:10  research=idle | no open task            (post-archive baseline)
19:07:42  research=working | t_e03e656a          (during the real task)
19:07:57  research=idle | no open task            (task done — and now lands on IDLE)
```

Correlated against `task_events` again: `claimed` 19:07:41Z → UI working 19:07:42;
`completed` 19:07:56Z → UI idle 19:07:57. **+1s both times, same as step 5.**

This is the check that actually matters: the desk now returns to **idle** rather than
snapping back to `blocked`, which is what the stale tasks were causing. The earlier
"blocked" wart noted in step 5 is resolved by cleaning the board, not by changing the
status rule — so the rule stays honest and un-fudged.

Computed style on the desk's status bar also checked: `rgb(75, 85, 99)` = the `--idle`
grey, not the `--working` green.

Liveness task result verified rather than trusted: `t_e03e656a` replied `ALIVE` as
instructed.

### ❌ CORRECTION — the "gateway has no API key" warning was WRONG

Earlier entries in this log claimed `hermes-gateway` has no `OPENROUTER_API_KEY` in
its environment and that every channel turn would fail. **That is incorrect, and the
claim is hereby retracted.** It was a leftover inference from step 2, written *before*
the key was added to `~/.hermes/.env`, and never re-tested afterwards.

Two wrong assumptions were stacked on top of each other:

1. *"`.env` is loaded into `os.environ` at import time, so the gateway booted at
   09:00:26 before the key landed at 09:03:40 and therefore missed it."*
   Half true. `env_loader._load_dotenv_with_fallback` does load a dotenv into
   `os.environ` — but **not on the gateway's inference path**.
2. *"Check `/proc/<pid>/environ` to see if the gateway has the key."*
   The wrong instrument entirely.

What the code actually does — `gateway/run.py:1822`, in its own words:

> `set_secret_scope` makes the profile `.env` the credential source **without mutating
> `os.environ`**, so subprocesses never inherit cross-profile secrets.

So `os.environ` holding no key is the **security property working as designed**, not
a bug. Per turn, the gateway calls `build_profile_secret_scope(Path(profile_home))`
and re-reads that profile's `.env` **fresh**, then `get_secret()` reads from that
mapping. A gateway restart was never needed; a restart changes nothing.

Proven by running the gateway's own resolution path (`build_profile_secret_scope` →
`set_secret_scope` → `get_secret`, exactly `gateway/run.py:1836`):

```
  default/helix  -> resolves (len=73)
  pc             -> resolves (len=73)
  research       -> resolves (len=73)
```

**The step-2 fix (key in `~/.hermes/.env`) already covered the gateway.** No service
edit, no `EnvironmentFile=`, no `~/.config/environment.d/` needed — which is what this
log previously told the human to do.

A gateway restart was performed while chasing this. Harmless (graceful drain, no
active tasks on the board) but unnecessary, and it was based on the bad hypothesis.

**Could not be end-to-end tested, and that is a real gap:** `channel_directory.json`
is `{"platforms": {}}` — no messaging platform is configured at all, so there is no
channel to send a real turn through. The credential-resolution path is proven; an
actual message round-trip is not. Revisit when the `comm` profile lands (Phase 5) —
that is the moment it becomes testable.

### Bridge is now a managed service
`~/.config/systemd/user/helix-bridge.service` — the bridge no longer dies with the
shell, matching how `hermes-gateway` already works. Linger is enabled, so it survives
logout too.

Replaced the hand-started process from step 5 (it had been holding port 8787 the whole
time under the original pid). Re-verified under systemd, not assumed:
`active`, loopback `200`, LAN `192.168.100.97:8787` refused (`000`), `GET /` serves the
UI, and a deliberate `restart` comes back healthy with all three profiles reporting
`idle` and 25 events. `MemoryMax=150M` set, since RAM is tight on this box.

    systemctl --user status helix-bridge
    journalctl --user -u helix-bridge -f
    systemctl --user disable --now helix-bridge   # to undo

### Still open
- No messaging platform is configured, so no real gateway turn has been exercised.
  Blocked on the `comm` profile (Phase 5).
- Per spec §5, still deferred: Easy Diffusion, Obsidian, link-paste downloader, the
  §17 skills shortlist, Voice Core.

---

## 2026-09-30 (later) — World Monitor panel

### The globe is NOT a static app — the obvious approach fails
First tried the laziest thing: `npm run build` (succeeded, 7.6s, 31MB dist) served by
`python3 -m http.server`. **Wrong.** The globe rendered — real ArcGIS imagery and
reearth terrain — but its own data layers were dead:

```
/api/setup/status            404
/api/realtime/debug-log      501 Unsupported method ('POST')
/api/openai/hud-summary      501
```

Cause, from the source: `server/standalone/key-setup.js:242` — its provider routes
install via `configureServer` and **"never configurePreviewServer"**, and
`api-not-found.js` returns 404 for `/api/*` in preview. So a built bundle is
permanently missing every realtime layer. The dev server is the only mode with data.
`helix-globe.service` therefore runs `npm run dev`, not the dist.

Reverted the static service. Verified the dev server restores everything:
`/api/setup/status` 200, and real payloads from `/api/celestrak/starlink` (live TLEs,
epoch day-272-of-2026 = current), `/api/celestrak/active`, `/api/launches`
(SpaceDevs, 27 results).

### The globe refused to be embedded — needed a local patch
The first iframe rendered **black**. Not a timing issue:

```
Framing 'http://127.0.0.1:4173/' violates the following Content Security Policy
directive: "frame-ancestors 'none'". The request has been blocked.
```

`build/vite.js` hardcoded `X-Frame-Options: DENY` and `CSP: frame-ancestors 'none'`
on the document. Not env-configurable. Human authorised touching files outside
`~/helix` and embedding at any cost.

Patched **one** file — `build/vite.js`, `headers: {}` — with a `ponytail:` comment
saying to re-add on `git pull`.

Deliberately did **not** touch `key-setup.js:81-82`, which sets the same headers on
the credential-status JSON. Two reasons: it guards a real credential surface, and
XHR is not affected by framing headers — so it was never what blocked the embed.
Re-verified after the patch: document headers gone, `/api/setup/status` 200, and the
credential-response headers still present.

Also had to cache-bust: the browser kept replaying the old cached response *with* its
headers, so the CSP error persisted after the server fix. A `?cb=` on the iframe src
plus a bridge fix — `do_GET` now strips the query string, which was a real bug
(`/world-monitor/?cb=2` 404'd before).

### Headlines: currentslab/news-api-skills evaluated, NOT installed
Cloned and read it rather than going by name. Findings:
- It is a **Hermes skill** (agent instructions), not a server component — it would not
  serve a UI panel even if installed.
- `SKILL.md:4` — "Requires internet access and **CURRENTS_API_KEY**"; registration
  required, and no key is configured.
- Free-only rule + "install only if you actually use it" → **skipped.**

Used keyless public RSS instead, stdlib `urllib` + `xml.etree` in the bridge, ~35
lines, nothing installed: BBC World, NPR, Ars Technica, Hacker News. Al Jazeera tested
and dropped — unreachable from this box (HTTP 000). 5-minute cache.

`/headlines` degrades per feed rather than failing: during testing Hacker News timed
out and the panel showed `unreachable — <urlopen error timed out>` while the other 34
stories still rendered. That is the intended behaviour, not a bug.

### Verification — real data, not placeholders
- **Globe:** screenshot of the live panel shows a real Cesium 3D render — satellite
  imagery of Austin, Texas with streets and buildings, HUD reading
  `NORMAL METRO NEAR FROST BANK TOWER (AUSTIN)`, live `RSN: 0.34M MTIRS: 1.71`, real
  clock, Cesium ion attribution. Console shows **zero** CSP errors.
- **Headlines:** 46 stories rendered; BBC items dated `Tue, 29 Sep 2026` (today), links
  resolve to real `bbc.co.uk` articles.
- First-launch onboarding dismissed once in the globe's own origin so it persists into
  the iframe via localStorage.
- Path traversal still refused after the query-stripping change: `404`.

### Services
| service | port | state | mem |
|---|---|---|---|
| `helix-bridge` | 8787 | active, enabled | 19 MB |
| `helix-globe` | 4173 | active, enabled | 229 MB |
| `hermes-gateway` | — | active, enabled | — |

All survive logout (linger). `helix-globe` has `MemoryMax=1200M` — it is the
memory-hungry one on a 6.9GB box. Restart-tested: comes back healthy with root 200
and api 200. RAM dipped to 1.6GB free with several browser tabs open; back to 2.4GB
after closing them — worth remembering, the globe + a few tabs is the budget.

### Not done
- **Voice Core** — stopped here as instructed.
- Globe HUD shows "8 KEYS WAITING": 8 optional provider keys (Google Maps, OpenAI,
  etc.) unconfigured. Core globe + satellites + launches work keyless. Only add keys if
  a specific layer is actually wanted — that is a paid/keyed decision, not a default.
- `~/helix/ui/world-monitor/` is standalone by design; no shared shell/nav with Agent
  Town yet, per instruction.

---

## 2026-09-30 (status review) — what is done, what is missing

Measured, not assumed. All services healthy, every endpoint 200.

| component | state |
|---|---|
| `hermes-gateway` | active, enabled |
| `helix-bridge` (8787) | active, enabled, 19 MB |
| `helix-globe` (4173) | active, enabled, 229 MB |
| Agent Town | 200, live from kanban.db |
| World Monitor | 200, globe + 46 real headlines |
| `pc` safety gate | 22 `approvals.deny` rules, verified blocking |
| RAM | 2.5 GB free of 7.0 GB |

**Nothing is failing.** Three profiles (`helix`, `pc`, `research`) delegate over
Kanban, verified end-to-end. Five spec'd profiles do not exist yet.

### Drift found — real, cheap to fix
| profile | model | fallbacks | tools |
|---|---|---|---|
| `helix` (CEO) | `openrouter/free` | **0** | **18** |
| `research` | `openrouter/free` | **0** | 12 |
| `pc` | `stealth/space-bunny-alpha` | 2 | 8 |

1. **Only `pc` has a fallback chain.** Spec §11 requires automatic failover across the
   model priority list "so a rate-limited/deprecated model doesn't hard-fail a
   session". `helix` and `research` are on a bare `openrouter/free` with nothing behind
   it — the exact failure mode the spec asks to prevent. This is drift from my own
   step-4 work: I only wired fallbacks on the profile I happened to create then.
2. **`helix` still has 18 tools.** Spec §6.1: "The CEO profile should primarily
   delegate, not execute — once delegation is reliable, trim its own tool list the same
   way sub-agent tool lists are trimmed." Delegation has been reliable since the
   step-5 verification, so this is now due.
3. **Model policy applied unevenly.** `pc` got the spec §11 priority-1 model;
   `helix` and `research` were never moved.

### Missing, per the specs
- **Profiles** (build spec §3): `dev`, `vision`, `comm` — none created.
  Also `career` and `sysmon` from requirements §7.6/§7.8, though requirements §7 says
  agent count should stay flexible and agents must justify themselves. Judged
  lower-value than the three above; not building until asked.
- **Voice Core** (build spec §4.2, requirements §9) — not started. Barge-in is an
  explicit *required* feature, not optional.
- **Phase 3 local fallback model** — Ollama + small quantized model, deferred by
  design (online-first is correct for this hardware).
- **No real gateway message round-trip** — still untestable, `channel_directory.json`
  is `{"platforms": {}}`. Building `comm` is what unblocks it.
- **2D map** in World Monitor — requirements §16.3 wants globe + map + headlines; the
  globe app has its own map mode, not yet surfaced.

### Fix 1 — model/fallback drift (spec §11)

`helix` and `research` had **zero** fallbacks — the exact "rate-limited model
hard-fails a session" failure spec §11 asks to prevent. Cause was my own drift: in
step 4 I only wired a chain on `pc`, the profile I happened to create.

Re-verified all three models against the live OpenRouter catalog first (they rotate):
`stealth/space-bunny-alpha` available, `nvidia/nemotron-3-ultra-550b-a55b:free`
available, `openrouter/free` available. Then applied the §11 priority order to both
missed profiles. Confirmed via `hermes fallback list` (the tool, not the YAML):

```
Primary:   stealth/space-bunny-alpha  (via auto)
  1. nvidia/nemotron-3-ultra-550b-a55b:free  (via openrouter)
  2. openrouter/free                          (via openrouter)
```

### Fix 2 — trimmed the CEO (spec §6.1)

§6.1 says the CEO should delegate rather than execute, and to trim its tool list "the
same way sub-agent tool lists are trimmed" once delegation is reliable. It had been
reliable since step 5, so this was due: **18 tools → 8**
(`clarify, delegation, file, kanban, memory, skills, todo, web`). No terminal, no
computer-use, no code execution — the CEO orchestrates, sub-agents execute.

**Trim verified not to break it** — a trim that crippled the orchestrator would be
worse than the drift it fixed. Two checks: a research task completed normally, and
the CEO itself created and delegated a fresh Kanban task (`t_d3fb23e1`, replied
`CEO_TRIM_OK`).

### Created `dev`, `vision`, `comm` (build spec §3)

All three cloned, key present in each `.env` (perm 600), model + fallbacks inherited —
which only worked because Fix 1 landed first.

| profile | tools | SOUL focus |
|---|---|---|
| `dev` | 8 — incl. `terminal`, `code_execution`, `browser` | drive OpenCode via the bundled skill; never claim a file exists without checking |
| `vision` | 7 — incl. `vision`, `web` | describe only what is actually visible; label inference as inference |
| `comm` | 6 | draft before sending; never invent content on the human's behalf; no secrets on channels |

**Gotcha worth remembering:** `--clone` copies the *current* source toolset, so all
three inherited the CEO's brand-new 8-tool trim. `dev` came out with no `terminal`
and `vision` with no `vision` tool — I only caught it by printing the resulting
toolsets instead of trusting the "Disabled:" success lines. Enabled explicitly after.

**Smoke-tested all three through real Kanban dispatch**, then checked the claims
rather than the summaries:
- `dev` — reported HEAD `0bf1c77`, 5 commits on main. Re-ran `git rev-parse` and
  `git rev-list --count` myself: exact match.
- `vision` — answered and flagged honestly that it had no image input.
- `comm` — drafted, and did **not** send, per its SOUL.

### Applied the `pc` lesson to `dev`

The `dev` SOUL forbids destructive git, but SOUL prose is not enforcement — proven
the hard way with `pc`. Gave `dev` an `approvals.deny` list (15 rules) and tested it
both ways:

```
deny:  git push --force · git reset --hard · git clean -fd · rm -rf · npm publish   (5/5 blocked)
allow: git status · npm test · git log · python3 -m pytest · rm build/app.js       (5/5 allowed)
```

### UI now reflects every profile
Bridge `PROFILES` extended to all six; the Agent Town page no longer hardcodes a desk
list — it renders whatever the bridge reports, so the next profile added needs no UI
edit. Verified: 6 desks render, and an in-page poller caught a real task driving
`dev=idle → working → idle`. Result checked: `t_081ddaf9` reported
`2026-09-29T20:05:43Z` against a real clock of `20:06:41Z`.

### Six agents live
```
helix (CEO)  research  pc  dev  vision  comm     all on stealth/space-bunny-alpha + 2 fallbacks
```

---

## 2026-09-30 — Voice Core (spec §16.2 / §9)

### `hermes-live-voice` evaluated and REJECTED — three hard blockers

Build spec §4.2 and requirements §9 both say to check this first. Cloned and read the
setup docs. It is a genuinely good fit for barge-in, but it cannot run here:

| provider | blocker |
|---|---|
| Local HuggingFace | **Apple Silicon only** (managed setup). This box is x86_64. `docs/setup.md:38` also requires **≥12 GB RAM**; this has 6.9 GB. Two independent failures. |
| OpenAI Realtime | Paid API — violates the free-only rule (§1). |
| Gemini Live | Routes audio to Google — requirements §5.3 is a *standing* constraint to keep data away from Google. |

Not installed. Recorded so a future session does not re-litigate it.

**The reuse that did apply:** requirements §16.2 says "reuse the existing Three.js orb
work already done for the old Jarvis dashboard rather than rebuilding it." It is real —
`orbScene.ts`, 858 lines, with EffectComposer, UnrealBloomPass and a chromatic-aberration
shader. Reused rather than rewritten:

- Transpiled with **esbuild 0.25.12** (already on this box via gods-eye-view) rather
  than hand-stripping 47 type annotations: 30 KB TS → 22.2 KB JS in 10 ms, with the
  `three` imports preserved as bare specifiers.
- The archive's vendored three is a **2021 UMD build** and its addons are classic
  scripts, which do not satisfy `three/addons/…` ES imports. So three comes from
  unpkg via an **import map**. This is the one CDN dependency in Helix; swap the two
  import-map entries for local copies if it must run offline.

### Two real bridge bugs found while wiring it

1. **Sibling assets were not served.** `orbScene.js` 404'd — the bridge only served the
   one HTML file per route. Added asset serving restricted to realpaths under `ui/`
   and to a safe extension list. Re-tested traversal: `/../.env`,
   `/voice-core/../../.hermes/.env`, `%2e%2e` encodings all still **404**.
2. **`Content-Type` was hardcoded `text/html` for every file**, so the module script
   arrived as HTML and was rejected outright ("Strict MIME type checking is enforced
   for module"). Now uses `mimetypes.guess_type`. Verified: `orbScene.js` →
   `text/javascript; charset=utf-8`.

### New bridge endpoints
- **`/cortex`** — real counts for the Memory/Skills/Soul/Settings nodes, counted off the
  filesystem so the graph cannot drift: `skills 12, soul 6, settings 6, memory 0`.
  `memory 0` is a **true zero** — the dirs exist in all 6 profiles and none has a `.md`
  in it yet, not a counting bug. (First version counted only the default profile and
  read 0 there too; now summed across profiles so it can't under-report later.)
- **`/say` (POST)** — turns a voice transcript into a real Kanban task for the CEO.
  Shells out to `hermes kanban create` rather than INSERTing into `kanban.db`, because
  the board has claim locks, event rows and sequences that Hermes owns; raw SQL would
  make tasks the dispatcher doesn't understand.

  `/say` is the bridge's **first write path**, so it is CSRF-guarded: it requires
  `X-Helix-Voice: 1`, which forces a preflight that is never answered, so a random web
  page cannot make a browser send it. Verified from the page's own origin:
  with header → `200 {ok:true, task:…}`, without → `403`.

### Verified end-to-end — voice really reaches Helix
From the browser's own origin, using the exact request the Send button makes:
`/say` → `t_4850ed07` → dispatch → CEO ran it → replied **`UI_PATH_OK`**. Also via
curl: `t_c372f0b0` → **`VOICE_E2E_OK`**. Empty transcript rejected. So this panel is
wired to live state, not a mock.

### ⚠ Barge-in is implemented but NOT verified — and I could not verify it here
Barge-in is the one *required* feature of this panel (requirements §9: the old Jarvis
had to be closed and restarted mid-sentence). The code path is there: continuous
recognition, and any final transcript arriving while `speaking` calls
`speechSynthesis.cancel()` immediately.

**This environment has no audio output device.** Measured, not assumed:
`speak()` does set state to `SPEAKING` and `speechSynthesis.speaking` did become
`true` — but the utterance ended within 100 ms, every time. A cancel test found
`wasSpeakingBeforeCancel: false`, i.e. there was never a long enough utterance to
interrupt. So the mechanism is unproven here; it needs a machine with a speaker and a
mic. **Do not record barge-in as working until it has been heard working.**

### ⚠ Chrome's Web Speech API conflicts with §5.3
The browser STT/TTS used here is free and needs no key, but Chrome's Web Speech
recognition **sends microphone audio to Google**. §5.3 is a standing constraint to
keep data away from Google. The page shows a permanent warning about this. The
privacy-respecting replacement is `drakulavich/kesha-voice-kit` (local, 25 languages,
no key) or Vosk — **not installed**, because doing it properly is a separate piece of
work and shipping the privacy leak silently was the alternative. Flagged, not hidden.

Also noted: no wake word yet (§9 lists it), no Urdu/mixed support beyond whatever
`recog.lang` is set to — the SOUL/spec call for English/Urdu/mixed and the browser
default will not cover that. Both are honest gaps in this version.

### Two real bugs found by asking "can it take more than 5s?"

Measuring the latency curve instead of restating a summary turned up two things the
earlier smoke tests had missed:

**1. `/stt` was capped at ~2 seconds of audio.** The 64 KB body limit meant for the
JSON `/say` payload was also being applied to audio. At 16 kHz mono that is ~32 KB per
second, so any clip over 2 s returned `{"ok": false, "error": "body too large"}`. The
first browser test happened to use a 48 KB clip and passed, so this stayed invisible
until a 3 s clip was tried. Audio and JSON now have separate caps (12 MB / 64 KB).

**2. Thread oversubscription was making it ~5x slower.** whisper defaults to one
thread per core; this is 4 cores and the load average sits around **9** because of the
desktop app. Same 30 s clip throughout:

| `-t` | time | rate |
|---|---|---|
| 4 (the default) | 100 s | 0.30x realtime |
| 3 | 28 s | 1.07x |
| **2** | **21.5 s** | **1.40x** |
| 1 | 34 s | 0.87x |

`-t 2` is baked into the bridge with the numbers in a comment, because it looks like a
mistake otherwise and someone will "fix" it back to `-t 4`.

### Actual behaviour, measured through the real endpoint
There is **no cap on how long you can talk.** Latency scales with clip length:

| clip | wall |
|---|---|
| 1 s (Urdu) | 7.5 s |
| 3 s | 6.9 s |
| 10 s | 7.0 s |
| 30 s | 22.0 s |

Roughly a ~6.5 s fixed cost plus ~0.5 s per extra second. A normal 3–5 s voice command
returns in **about 7 seconds** — usable, not instant. That is the honest number, and it
was only knowable by measuring on the real box under real load.

Re-verified after both fixes: Urdu auto-detect `"Islamabad!"`, Urdu forced-to-`ur`
gives Urdu script, English 3 s clip transcribes correctly.

### Two bugs fixed, now documented in code as well as here

**1. `/stt` was capped at ~2 seconds of audio.** The 64 KB body limit intended for the
JSON `/say` payload was also applied to audio. At 16 kHz mono that is ~32 KB/second, so
any clip over 2 s returned `{"ok": false, "error": "body too large"}`. The first browser
test used a 48 KB clip and passed, which is exactly how it stayed hidden. Audio and JSON
now have separate caps (12 MB / 64 KB).

**2. Thread oversubscription made it ~5x slower.** whisper defaults to one thread per
core; this is 4 cores at load ~9, so it thrashed. Same 30 s clip:

| `-t` | time | rate |
|---|---|---|
| 4 (whisper's default) | 100.0 s | 0.30x realtime |
| 3 | 28.1 s | 1.07x |
| **2** | **21.5 s** | **1.40x** |
| 1 | 34.4 s | 0.87x |

`STT_THREADS = "2"` in `bridge/server.py` carries this table as a comment, because it
reads like a mistake and someone will otherwise "fix" it back to `-t 4`.

### Barge-in: implemented, then actually verified

**My earlier "no audio device" explanation was wrong.** The hardware is fine —
ALC3246 analog + HDMI, pipewire running, espeak-ng installed. The real cause was that
Chrome's `speechSynthesis.getVoices()` returns **zero voices**, so its utterances ended
instantly and there was never anything to interrupt. Diagnosed by checking the voice
list rather than assuming.

**Fix:** TTS moved server-side to **espeak-ng** (already installed, local, and it has a
built-in Urdu voice), rendered by a new `POST /tts` and played through an `<audio>`
element. `speechSynthesis` is gone. The interrupt is now `audioEl.pause()` on a real,
measurable media element.

Proven in three steps rather than assumed:

1. **Playback was real.** `currentTime` advanced during speech, and a pipewire
   sink-input appeared — audio was actually being produced, not simulated.
2. **The cut lands mid-sentence.** Across repeated runs the interrupt fired at
   **1.04s, 1.12s, 1.41s, 1.90s, 2.11s and 2.58s of a 9.27s utterance.**
3. **Cut, not played to completion.** After cutting at 1.04s and waiting **11 seconds**
   — longer than the 7.8s that remained — `onended` never fired and the state never
   reached `IDLE`. Playback stopped at the interrupt.

**Honest limit on that proof:** there is no sample-level acoustic evidence. `pactl`'s
`cork` flag stayed `no` and the sink-input stayed open after `pause()` (Chrome keeps the
stream node alive for a paused element), and no `parec` tap is available to sample the
output. The proof is behavioural, not acoustic.

**Still not exercised:** "talk over Helix" needs a microphone, and there is none in this
environment. The interrupt half is proven; the capture half is not.

### Third bug found by that test: a mic failure made barge-in look broken
The first interrupt run ended in `ERROR`, and the log said `mic: Permission denied` —
the interrupt had *worked*, but the subsequent `getUserMedia` failure overwrote the state
and buried the success message. A user whose mic hiccuped after interrupting would have
concluded the barge-in failed. Now a mic failure immediately after an interrupt settles
on `WARNING` with `"interrupted OK, but mic unavailable: …"`; a genuine cold-start mic
failure still reports `ERROR`.

Verified after the fix: `before: SPEAKING` → `settled: WARNING`, log ordering correct.

Also: all module state in the page is declared in one block. Splitting it next to its
users caused a duplicate-identifier `SyntaxError` three separate times.

---

## 2026-09-30 — TTS: online-first (edge-tts) with a local fallback (Piper)

Requested as: online primary, local fallback, and the fallback **proven** to trigger
rather than merely configured.

### Installed
| | |
|---|---|
| edge-tts | 7.2.8, in `~/.local/share/helix-tts-venv` (PEP 668 blocks the system pip) |
| Piper | binary + `en_GB-alba-medium` voice in `~/.local/share/helix-tts/` (63 MB model) |

Piper measured **0.26–0.30 real-time factor** (3.3–3.8x faster than real time) on this
box. British voices used on both: `en-GB-SoniaNeural` (edge) and `en_GB-alba-medium`
(Piper). Found, not assumed: edge-tts also ships **`ur-PK-UzmaNeural`**, a Pakistani
Urdu voice, so Urdu is covered online too.

### The chain
`edge-tts` → `Piper` → `espeak-ng`, tried in order until one returns real audio
(>512 bytes). espeak is a third, last-resort tier: it was the previous TTS, it is
already installed, it has a Urdu voice, and it makes "the panel goes mute" nearly
impossible. Responses carry `X-Helix-TTS-Engine`; the page logs which engine spoke, so
the fallback is visible rather than assumed.

### Proving the fallback — with the network genuinely removed
Not simulated. `unshare -rn` (user + network namespace; plain `unshare -n` is not
permitted here) gives a namespace with **no network interfaces at all** — `curl` to
pypi returns `000`. Note a fresh netns has `lo` **down**; without `ip link set lo up`
the loopback request itself fails and looks like a TTS bug.

The **real bridge** was started inside that namespace and driven over HTTP:

```
inside netns: internet = 000
POST /tts engine=auto  -> 200, 180896 bytes, X-Helix-TTS-Engine: piper, 4.10s wav
```

So `auto` really does fall through to Piper with no internet. And to prove Piper
isn't being *skipped* — i.e. edge-tts is genuinely attempted and genuinely fails:

```
POST /tts engine=edge  -> 400 {"error": "edge-tts: ... Cannot connect to host
                            speech.platform.bing.com:443 ... name resolution"}
POST /tts engine=piper -> 200, 70304 bytes, X-Helix-TTS-Engine: piper
```

The error text is edge-tts's own, not something the bridge invented.

Full matrix, every cell producing real audio:

| | online | offline (netns) |
|---|---|---|
| en, `auto` | `edge-tts` 22 KB mp3 | `piper` 97 KB wav |
| en, forced piper | `piper` 79 KB wav | `piper` 70 KB wav |
| ur, `auto` | `edge-tts` 13 KB mp3 | `espeak` 65 KB wav (Piper has no Urdu model) |

### Barge-in re-verified after the engine change
A change of TTS engine is exactly the kind of thing that silently breaks the
interrupt, so it was retested on **both**:

- edge-tts: **cut at 1.66s of 9.24s**
- piper: **cut at 1.39s of 9.30s**

**A false alarm, recorded because it nearly became a wrong fix:** an early run showed
no barge-in at all. The tempting explanation was Chrome's autoplay policy blocking
`play()` after an `await`. Tested directly instead of believed — `play()` succeeded
both with and without a preceding `await`. The actual cause was stale state from
earlier runs in the same page. Changed nothing on the strength of a guess.

Also fixed: the panel logged a made-up duration (`blob.size / 32000` — a WAV formula
applied to MP3, reporting 1.7s for 9.2s of audio). It now reads the real duration off
the media element, which matters because that number appears in the barge-in line.

### 2026-09-30 — World Monitor re-verified, not rebuilt

The World Monitor task was re-issued after the over-engineering pass. **It was already
built and verified earlier in this session**, so nothing was rebuilt. Re-checked against
the live system rather than asserted from memory, since the bridge has since been
refactored and restarted:

| check | result |
|---|---|
| `helix-globe` service | active, enabled |
| globe root / `/api/setup/status` | 200 / 200 |
| LAN `192.168.100.97:4173` | refused (000) — still loopback-only |
| framing headers on the document | absent — the `build/vite.js` patch is still in place |
| `/world-monitor/` | 200, **zero console errors** |
| iframe | 440x483, `src=http://127.0.0.1:4173/?cb=1` |

**Headlines — real, not placeholders:** 46 stories, all four feeds healthy
(`BBC World 12, NPR 10, Ars Technica 12, Hacker News 12`, zero errors). Top item linked
to a real `bbc.co.uk/news/articles/...` URL dated `Tue, 29 Sep 2026 23:15:29 GMT`.

**Globe — real, not placeholders:** screenshot shows a live Cesium render with satellite
imagery of Austin, TX. Data layers confirmed server-side, not assumed from the picture:
`/api/celestrak/starlink` returning a TLE at epoch **26272 = day 272 of 2026 (yesterday)**
— i.e. live, not a cached fixture — plus `/api/celestrak/active` and `/api/launches`
(27 results).

`currentslab/news-api-skills` remains **not installed**, for the reason already recorded
above: it is an agent-facing Hermes skill, not a server component, so it could not serve a
UI panel even if installed, and it requires a registered `CURRENTS_API_KEY` that this
machine does not have. Keyless RSS does the job with zero installs.

**Still not done in World Monitor** (unchanged, for the record):
- **2D map** — requirements §16.3 asks for globe + map + headlines. The globe app has its
  own map mode; it is not surfaced as a second view in the panel.
- 8 optional provider keys unconfigured ("POWER UP · 8 KEYS WAITING"). Globe, satellites
  and launches all work keyless; these are keyed/paid and only worth adding if a specific
  layer is actually wanted.
