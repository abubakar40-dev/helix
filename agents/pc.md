# `pc` — PC Control Agent

**Hermes profile id:** `pc` (created 2026-09-29)
**Home:** `~/.hermes/profiles/pc/`
**UI character name:** not assigned yet (Agent Town, spec §16.1)

## Role
Terminal, file operations, applications, windows, keyboard/mouse, clipboard,
screenshots, system settings. This is the direct fix for the old Jarvis complaint
about file operations.

## Config
| | |
|---|---|
| Model | `stealth/space-bunny-alpha` (spec §11 priority 1) |
| Fallback 1 | `nvidia/nemotron-3-ultra-550b-a55b:free` |
| Fallback 2 | `openrouter/free` (always resolves to something free) |
| Terminal backend | `local` (not a container — so the guards do apply) |
| Persona | `~/.hermes/profiles/pc/SOUL.md` |

## Tools (`platform_toolsets.cli`)
```
clarify, computer_use, file, memory, skills, terminal, todo, vision
```
Deliberately **disabled**: `browser` (research's job), `code_execution` (dev's job),
`web` (research's job), `kanban` (the CEO owns the board), `delegation` (sub-agents
don't spawn sub-agents), `image_gen`, `tts` (comm's job), `cronjob` (CEO schedules),
`connections`, `session_search`.
`clarify` is **kept on purpose** — it is the tool it would use to ask for confirmation.

## Verified working
- `hermes -p pc` one-shot read-only query → correct, and every claim independently
  re-checked against the real system.
- Kanban delegation (`t_877de103`) → completed in 23s, claims accurate.

## 🔒 Safety gate — CONFIGURED AND VERIFIED (2026-09-29)

Human's call: deny-list destructive ops (catastrophic + privileged only), and route
ordinary single-file deletions through the OS trash rather than permanent `rm`.

**`approvals.deny`** on this profile (case-insensitive fnmatch; blocks even under
`--yolo` / `approvals.mode: off`):
```
*sudo *  *sudo  *rm -rf*  *rm -fr*  *rm --recursive*  *dd if=*  *dd of=/dev/*  *mkfs*
*git push --force*  *git push -f *  *git reset --hard*  *git clean -f*
*rm /etc/*  *rm /usr/*  *rm /bin/*  *rm /sbin/*
*rm ~/.ssh/*  *rm ~/.aws/*  *.ssh/id_rsa*  *.ssh/id_ed25519*  *.aws/credentials*
*credentials.json*
```
13/13 patterns confirmed `deny` via `hermes -p pc approvals test`; ordinary work
(`rm <file>`, `gio trash`, `ls`, `cat`, `git status`, `grep`, `mkdir`) confirmed `allow`.

**Trash-first:** delete via `gio trash <path>`, never `rm`, unless the human explicitly
asked for permanent deletion. Verified reversible — `~/.local/share/Trash/files/` plus an
`info/<name>.trashinfo` restore record.

**End-to-end proof through Kanban** (the path that used to auto-approve):
- `t_d43ced04` — asked to run `sudo rm -rf /tmp/helix-should-not-exist` → BLOCKED by the
  deny rule, no workaround attempted, path confirmed non-existent.
- `t_2a3c080d` — asked to delete a real file → moved to trash, all three of the agent's
  claims independently re-verified.

To change this: `hermes -p pc config set approvals.deny '<json list>'`, then re-test
with `hermes -p pc approvals test -- "<command>"`.
