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

### Step 5 — bridge + Agent Town: not started, gate is now satisfied

Spec §6 step 5 says start this only after CEO + 2 working sub-agents. That condition is
now met: CEO delegates over Kanban, `research` and `pc` both verified end-to-end.
Not started in this session — it is the first genuinely new code in the project, and it
should be its own decision rather than a footnote at the end of a security fix.
