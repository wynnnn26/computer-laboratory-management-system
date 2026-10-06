# Architecture — How the System Works

> Companion to [README.md](README.md), which documents features and user-facing
> behavior. This document explains the **runtime architecture**: how the two
> applications start, authenticate, exchange messages, enforce website policy,
> persist data, and get built and tested. All file references are relative to
> the repository root.

---

## 1. Big picture

The whole system is **two applications built from one codebase**. `main.py`
decides which one it is; nothing else needs to change.

```
┌────────────────────── Server PC  (server.exe / --server) ─────────────────────┐
│                                                                               │
│   main.py ──► database.init_db() ──► LabServer.start() ──► LoginWindow        │
│                    │                      │                      │           │
│             lab_system.db            TLS TCP :8443            role routing    │
│            (SQLite, WAL mode)        UDP  :8444                 │            │
│                    ▲                      ▲          ┌─────────┴─────────┐    │
│                    │      registry,       │          │ admin/staff/      │    │
│   users · computers · sessions · heartbeats│          │ maintenance       │ student│
│   websites · audit · settings · commands   │          ▼                   ▼    │
│                                     AdminDashboard   StudentDashboard        │
└───────────────────────────────┬───────────────────────────────────────────────┘
                                │
        4-byte length + JSON frames over TLS  (auth, heartbeats, commands,
        screenshots, website policy, activity logs)  +  UDP broadcast discovery
                                │
┌───────────────────────────────┴──────────────── Lab PC  (client.exe / --client)┐
│   run_client() ──► single-instance mutex · Startup entry · 2 watchdog tasks    │
│        │  guard (--guard): watches every 2 s, relaunches a killed kiosk        │
│        │                                                                       │
│   ClientApp (fullscreen kiosk: login → locked / paused / unlocked / session)   │
│        │                                                                       │
│   ClientNetwork (worker thread: connect · discover · heartbeat · commands)     │
│        │                                                                       │
│   lab_config.json (server address + last web policy)                           │
│   lab_client.db   (offline login roster + queued activity logs)               │
└───────────────────────────────────────────────────────────────────────────────┘
```

**Design rules the whole system obeys:**

- **Server-authoritative state.** Locks, pauses, policy versions and session
  data live on the server; clients mirror them and re-sync on every reconnect.
- **LAN only.** No cloud, no REST API — raw TCP/TLS sockets with a
  length-prefixed JSON protocol, plus UDP broadcast for zero-config discovery.
- **Offline-tolerant.** A lab PC keeps working (login + logging) without the
  server and reconciles when the link returns.
- **Audited.** Every login, command, policy change and security-sensitive
  action lands in `admin_activity_log`.

| | |
|---|---|
| Language / UI | Python 3.13, CustomTkinter (dark theme) |
| Database | SQLite (WAL) — `lab_system.db` (server), `lab_client.db` (client) |
| Transport | TLS ≥ 1.2 TCP (default **:8443**), UDP discovery (**:8444** = TCP+1) |
| Client version | `CLIENT_VERSION = "2.0"` |
| Roles | `admin`, `staff`, `maintenance`, `student` |

---

## 2. Repository map

| Group | Files | Role |
|---|---|---|
| Entry / launcher | `main.py` | Mode detection (`_detected_mode` :35), server launcher (`run_server_mode` :316), `LoginWindow` (:126) |
| Server core | `server.py` (2.4k lines) | `LabServer` (:210): accept loop, auth, heartbeats, commands, observe/remote, screen-share fan-out, website policy, audit |
| Client core | `client.py` (4.3k lines) | `ClientApp` kiosk (:1146), `ClientNetwork` (:429), command handlers, share-screen overlay, watchdog/startup + guard process, CAD policy hardening, offline mode |
| Uninstaller | `uninstall.py`, `startup_ids.py` | Standalone `dist\uninstall.exe` (`--uac-admin`): watchdog tasks → Run entry → CAD policy values → stop `client.exe` → export log CSV → delete client files → self-delete; `startup_ids.py` is the single source of the auto-start names AND the CAD policy values both sides share |
| Wire protocol | `protocol.py` | `MessageType` enum (:19), `Message` framing (:126), TLS helpers (:251), self-signed cert generation (:269), message builders (:322-627) |
| Server database | `database.py` | Schema (:58-241), migrations (:244), PBKDF2 hashing (:40), seeding + settings (:408-516) |
| Client database | `local_store.py` | Offline log queue + auth roster cache (:64-384) |
| Website Access | `web_access.py`, `dns_filter.py` | Connection→domain detection & guarded close (:330); pure verdict engine + DNS repair (:84, :282) |
| Admin UI | `admin_dashboard.py` (3.9k lines) | `AdminDashboard` console (:176) — sidebar + 13 pages, remote controls, Share Screen toggle |
| Student UI | `student_dashboard.py` | Announcements / messages / borrowing (:38) |
| Data bridge | `client_api.py` | Network-first fetches for the student UI with SQLite fallback (:17) |
| UI framework | `components.py`, `theme.py`, `utils.py`, `crud_frame.py` | Shared widgets, design tokens, styling helpers, reusable CRUD tables/pages |
| Tests | `_test_integration.py`, `_test_client_gui.py`, `_probe_layout.py`, `_full_sweep.py`, `_contrast_all.py` (all via `gate.bat`) | 606 + 290 + 50 checks, self-contained |
| Build | `build_exe.bat`, `requirements.txt` | PyInstaller onefile builds for both apps |
| Assets | `assets/`, `pc_icons/`, `app_icon.ico` | Logo, 8 PC status icons, window icon |
| Design docs | `design-system/lab-management-system/MASTER.md` | Token/component spec behind `theme.py` |

### 2.1 File-by-file — what each Python file does and how it works

Runtime modules (shipped in the builds):

**`main.py`** — entry point and mode launcher. `_detected_mode` decides
from `sys.frozen` + the exe name (`server.exe` / `client.exe`) or the
`--server` / `--client` flags; run from source it shows a choice window.
Server mode: `init_db()` → start `LabServer` on a background thread →
open `LoginWindow` (:126); the login role routes to `AdminDashboard`
(admin/staff/maintenance) or `StudentDashboard` (student), and closing
that window tears the server down. Client mode simply hands off to
`client.run_client()`.

**`server.py`** — the LAN core (`LabServer`, §6). One thread accepts TLS
connections (`_accept_loop`), each connection gets a `_client_thread`
reading framed messages into a per-PC `ClientEntry` registry; a UDP
thread answers discovery (`_responder`), a sweep thread marks PCs
OFFLINE on missed heartbeats. Inbound traffic hits small `_on_*`
handlers (register, heartbeat, auth, session, command-response,
web-policy ack); outbound commands all funnel through `send_command`,
which writes the `client_commands` audit row first, sends the `CMD_*`
message and updates the row from the ack. Screen share runs its
capture→fan-out loop in `_share_worker`; dashboard updates leave
through the `on_event` queue.

**`client.py`** — the lab-PC kiosk (§7). `ClientNetwork` owns the
connection on its own thread (connect → UDP discovery fallback →
register → receive → reconnect with backoff); everything received is
queued and executed on the Tk thread, so widgets are only ever touched
there. `ClientApp` is the fullscreen login/pause/lock UI driven by the
guarded `_KioskState` machine. Startup registers the HKCU Run entry and
the two watchdog tasks (honouring `auto_start: false` after an
uninstall), spawns the `--guard` relaunch watcher, and applies/removes
the HKCU Ctrl+Alt+Del policy values with every screen transition;
shutdown/disconnect/logout paths always stop
observation, remote input and the share overlay. Offline behaviour
(auth roster, queued logs) is delegated to `local_store`.

**`admin_dashboard.py`** — the Server console (§10). A
`CTkToplevel` with a sidebar and 13 pages, most of them a configured
`CRUDFrame`. Two queues drive live behaviour: the server's `on_event`
queue (status changes) and a `results` queue from worker threads
(command outcomes → toasts). Page controls call `LabServer` methods and
the server re-checks the role; the toolbar on *Client PCs* carries
search, *Send to All* and the `📺 Share Screen` toggle
(`_toggle_share`).

**`student_dashboard.py`** — the student window: Announcements,
Messages and Equipment Borrowing tabs. Every fetch goes through
`client_api`, so the same code works over the LAN (Client Mode) and on
the server PC (direct SQLite).

**`protocol.py`** — the whole wire format (§4), used by both ends and
by nothing else: the `MessageType` enum, `Message` JSON with 4-byte
length framing (10 MB cap), `TLSSocketWrapper` (thread-safe framed I/O
over TLS with timeout save/restore around sends), TLS context +
self-signed certificate helpers, one `build_*` function per message,
the remote-input event sanitizer, and the send-file deny list.

**`database.py`** — `lab_system.db`, server side only. `init_db()`
creates the schema, runs `_migrate_legacy_schema` so old lab databases
upgrade in place, and seeds the default admin plus `system_settings`.
Passwords are PBKDF2-HMAC-SHA256 (100 000 iterations, 16-byte salt).
`get_connection()` returns a `Row`-factored connection.

**`local_store.py`** — `lab_client.db`, the kiosk's durable memory:
`local_logs` (client-generated UUID `event_id`, PENDING → SYNCED, never
deleted before the server acks), `auth_cache` (the last roster pushed
over TLS, salted hashes only — this is what makes offline login work)
and `meta`. `log_event` swallows its own failures: a log write can
never take the kiosk down.

**`client_api.py`** — the data bridge for UI code. With a live link
registered (`set_link`, done by `ClientNetwork`), every call becomes a
typed `link.request(kind, payload)` over TLS; with no link (server PC,
early startup) it runs the original direct-SQLite fallback, so client
pages never care which side they are on.

**`components.py`** — shared, pure-presentation widgets (cards, padded
frames, tab hosts, dividers, icons, status tiles, toast plumbing). No
network, no database: pages compose these instead of restyling them.

**`crud_frame.py`** — one generic "table + form" component over a single
SQLite table. Every management tab (students, computers, inventory,
borrowing, maintenance, announcements, sessions) is the same
`CRUDFrame`/`AccountsFrame` class configured with a field list; it owns
search/filter, stat tiles, add/edit/delete dialogs, CSV export and the
bulk-upload path.

**`theme.py`** — the single source of design tokens (palette, type
scale, spacing, radii, toast colours, `button()` helper) that
`utils.py` re-exports for compatibility, plus `configure_theme()`,
which pins the process DPI-aware before any window is created so
screenshots and layout are native-resolution.

**`utils.py`** — shared runtime helpers: styling wrappers around
`theme` (`style_app`, `center_window`, icon/logo loaders), CSV export,
status-tile rendering — and `capture_screen_b64()`, the one screen
capture used by Observe/screenshots on the Client and by Share Screen
on the Server (scale + JPEG quality 85 at 4:4:4 chroma, so small text
survives the encode).

**`web_access.py`** — Website Access enforcement (§8): the
`WebAccessDetector` thread samples the live connection table, maps
connections to domains, asks `dns_filter.decide` for a verdict and
performs the *guarded* browser close (by PID, verified after, audited);
it also owns the hosts-file marker cleanup.

**`dns_filter.py`** — import-safe and socket-free: the pure verdict
engine (`normalize` / `domain_matches` / `decide`, suffix matching)
shared by the detector and the UI, plus `repair_adapter_dns`, which
restores an adapter's DNS settings after the removed DNS proxy.

**`startup_ids.py`** — constants only: the two scheduled-task names and
the HKCU Run entry name/path, in one place. The Client registers them,
`uninstall.py` removes them, both import this module so the two sides
can never drift apart.

**`uninstall.py`** — the standalone `dist\uninstall.exe`: elevates via
`--uac-admin`, removes the watchdog tasks and Run entry (IDs from
`startup_ids`), stops the kiosk by PID, exports the local-log CSV as
evidence, deletes the client files, **verifies** the result, and only
then self-deletes — any `FAILED` check keeps both the binary and the
audit database.

Tooling (underscore = development only, never packaged by
`build_exe.bat`):

**`_test_integration.py`** — the 606-check end-to-end suite: boots a
real `LabServer` on test port 18443 against a scripted fake TLS client,
asserts on the real database, builds the dashboard, drives the screen
share, and re-checks the guardrails, watchdog and uninstaller. Prints
PASS/FAIL lines, exits non-zero on any failure.

**`_test_client_gui.py`** — the 290-check kiosk suite: real
`ClientApp` instances with stubbed network, hotkeys and `_schtasks`;
exercises the state machine, every command handler, offline login,
the emergency-stop paths and the screen-share overlay.

**`_probe_layout.py`** — responsive-layout probe: measures each page's
content demand against its available height, the details-panel mapping
and button-label clipping at 1080×700 / 1280×780 / 1440×900; fails
loudly when anything would be clipped.

**`_make_logo_assets.py`** / **`_make_pc_icons.py`** — one-shot asset
generators: `app_icon.ico` from the official logo (centred, LANCZOS,
multi-size) and the 8 PC status icons from `render_status_tile`; run
manually whenever the artwork changes.

**`_debug_state.py`** — an ad-hoc harness for poking `ClientApp` state
transitions (stubs the config dialog and hotkeys first); not part of
the gate.

---

## 3. Startup

### 3.1 Mode detection (`main.py:35`)

1. CLI flags `--server` / `--client` win.
2. If frozen (PyInstaller), the **executable name** decides: contains
   `server` → server mode, contains `client` → client mode.
3. Otherwise (running from source) the `Launcher` chooser window appears.

This is why both EXEs are built from the same `main.py`.

### 3.2 Server mode (`run_server_mode`, `main.py:316`)

1. `database.init_db()` — creates/migrates SQLite, seeds default accounts
   (`admin/admin123`, `staff/staff123`, `2023-00001/student123`) and 13
   system settings.
2. Removes leftover client watchdog tasks and clears the Ctrl+Alt+Del
   restrictions (a server PC must never open a kiosk over the console,
   and Windows there must be fully normal).
3. `LabServer.start()` (`server.py:248`):
   - auto-generates `server.crt`/`server.key` if missing (`ensure_certificates`
     :118 — RSA-2048 self-signed, CN `LabSystem Server`, 10 years),
   - binds `0.0.0.0:<server_port>` (default **8443**), `listen(50)`,
   - **startup healing**: every PC is marked OFFLINE and every session
     `Disconnected` (state from a previous run is never trusted),
   - spawns the accept thread, the offline sweep thread and the UDP
     discovery responder.
4. `LoginWindow` opens. On success the **role routes the UI**:
   `admin | staff | maintenance` → `AdminDashboard`, `student` →
   `StudentDashboard`. Tearing the window down stops the server.

### 3.3 Client mode (`run_client`, `client.py`)

1. `acquire_single_instance()` (:915) — named mutex
   `Local\ComputerLaboratoryClient`; a second launch re-opens the first
   (`--watchdog`/`--respawn` copies exit quietly, a human second copy is
   told). A `--watchdog` tick may only start the kiosk while its own
   scheduled task still exists — an in-flight tick whose task a panic
   stop or uninstall already deleted never resurrects the kiosk.
2. Registers two unelevated Windows scheduled tasks — one *at log on*, one
   *every minute* — that relaunch the kiosk if the process ever dies
   (`register_watchdog`, which also repairs a task whose stored
   command went stale or was deleted; a running kiosk re-verifies every
   15 minutes via `_watchdog_heal_loop`). Both registration points
   honour `auto_start: false` in `lab_config.json` — an uninstall
   writes it, only a hand start clears it — so the self-heal can never
   resurrect an uninstall.
3. Builds `ClientApp` (fullscreen login card) and starts `ClientNetwork`
   with the address from `lab_config.json`, falling back to **UDP
   discovery** if no address is stored (`discover_server_ip` :211), and
   spawns the **guard process** (`--guard`, one per PC behind its own
   mutex): a 2-second poll that relaunches a killed kiosk as
   `--respawn` (Task Scheduler refuses any repetition below one minute)
   and stands down as soon as the watchdog task disappears or
   auto-start is switched off. `run_client`'s `finally` stops the
   guard, clears the Ctrl+Alt+Del policies and frees the mutex — in
   that order.

---

## 4. Wire protocol (`protocol.py`)

**Framing:** every message is `4-byte big-endian length` + UTF-8 JSON body,
capped at **10 MB** (`protocol.py:126-203`). Each `Message` carries `type`,
`payload`, an 8-char `msg_id` and a `timestamp` (:126).

**Transport:** TLS 1.2+ with ECDHE/AES-GCM ciphers (:251-266). The server
loads its self-signed chain; clients accept it without hostname
verification (LAN deployment). Frames are read/written through
`TLSSocketWrapper` (:167).

**Discovery:** UDP broadcast magic `LAB_SYSTEM_DISCOVER` → reply
`LAB_SYSTEM_SERVER` on port **TCP+1 = 8444** (:111-120); the server answers in
`_responder` (`server.py:355`), the client probes with a 1.5 s timeout.

**Message catalogue** (`MessageType`, `protocol.py:19-96`):

| Category | Types |
|---|---|
| Auth | `AUTH_REQUEST`, `AUTH_RESPONSE`, `AUTH_CHALLENGE` |
| First-login change | `PASSWORD_CHANGE_REQUEST`, `PASSWORD_CHANGE_RESPONSE` |
| Lifecycle | `CLIENT_REGISTER`, `CLIENT_HEARTBEAT`, `CLIENT_STATUS`, `CLIENT_DISCONNECT` |
| Commands (server→client) | `CMD_LOCK`, `CMD_UNLOCK`, `CMD_LOGOUT`, `CMD_RESTART`, `CMD_SHUTDOWN`, `CMD_PAUSE`, `CMD_RESUME`, `CMD_SCREEN_OBSERVE_START/STOP`, `CMD_SCREEN_SHARE_START/FRAME/STOP`, `CMD_SCREENSHOT`, `CMD_SEND_MESSAGE`, `CMD_EXECUTE`, `CMD_WEB_FILTER`, `CMD_SEND_FILE` |
| Remote input | `CMD_REMOTE_START`, `CMD_REMOTE_STOP`, `CMD_REMOTE_INPUT` (whitelisted primitives only) |
| Acks | `CMD_RESPONSE`, `WEB_POLICY_ACK`, `PONG` |
| Sessions / activity | `SESSION_START`, `SESSION_END`, `SESSION_UPDATE`, `ACTIVITY_LOG` |
| Student queries | `STU_REQUEST`, `STU_RESPONSE` (announcements, messages, borrowing, attendance, profile, …) |
| Sync | `SYNC_REQUEST`, `SYNC_RESPONSE`, `ERROR` |

There is no version field in the envelope — compatibility is maintained by
the fixed message-type set. Website policy is versioned separately with a
monotonic integer (§8).

---

## 5. Authentication and sessions (server-authoritative)

1. The kiosk sends `AUTH_REQUEST(student_id, password, pc_name, …)`.
2. The server (`_do_auth`, `server.py:840`):
   - verifies the password against `users.password` stored as
     **PBKDF2-HMAC-SHA256, 100 000 iterations, 16-byte random salt**
     (`salt_hex$hash_hex`, `database.py:40-56`),
   - enforces lockout: `max_failed_logins = 5` within
     `lockout_duration = 300` s,
   - audits `login_success` / `login_failure` with IP and role,
   - returns the account + role + `must_change_password` flag.
3. If `must_change_password` is set, the client is forced through the
   **change-password screen** (`PASSWORD_CHANGE_*`); nothing else is
   reachable until it succeeds (`server.py:915`).
4. On success the client starts a session (`SESSION_START` → a
   `client_sessions` row) and stops it on logout/server-side end.
5. **Offline login:** the server pushes an auth roster (`auth_roster`
   sync, `server.py:1178`) on connect and **re-pushes it after every
   password/account change** (fire-and-forget `CMD_ROSTER_REFRESH`
   from `notify_roster_changed`; the client syncs in a background
   thread, with a 6-hour periodic push as backstop) that the client
   caches in
   `lab_client.db.auth_cache`. If the server is unreachable, the kiosk
   verifies locally (`local_store.verify_offline_login` :309) as long as the
   roster is younger than `max_offline_days = 7`. All offline activity is
   queued and replayed later (§7.4).

**Admin Lock is server-side:** once a PC is locked, typing credentials on
the kiosk cannot release it — only an explicit Admin Unlock
(`unlock_client` → `CMD_UNLOCK`) does (`server.py:1780`). Force Unlock
always targets **every** PC (the selection never narrows it); an OFFLINE
PC's unlock is saved as its desired state (`_save_desired(pc, None)` on
the `bulk_command` OFFLINE branch) and delivered when it reconnects.
The client's own ack rides along in every bulk result line —
`PC1: SUCCESS  (force_login)` when a session was restored vs
`(login_allowed)` when the PC simply landed on its login card — so an
operator can tell the two outcomes apart per PC at a glance.

---

## 6. Server runtime (`LabServer`, `server.py:210`)

### 6.1 Connections and registry

`_accept_loop` (:431) hands each socket to a `_client_thread` (:442) that
wraps it in TLS and reads frames until EOF. Every live connection is a
`ClientEntry` (:137) — identity, snapshot, send/close — registered by
`CLIENT_REGISTER` (`_on_register` :561) into the `computers` table (IP, MAC,
hostname, CPU/RAM, version…). Disconnects flow through
`_on_connection_lost` (:785) with a **grace period** before crash
escalation (`_escalate_disconnect` :813).

**Desired state is persisted** (`_load_desired`/`_save_desired` :640-653):
lock/pause decisions survive server restarts and are re-applied to clients
via the heartbeat reply.

### 6.2 Heartbeat and status

- Client sends `CLIENT_HEARTBEAT` **every 5 s** (`client.py:3640`) with
  CPU/RAM/user/connection type; the server answers `PONG` carrying the
  authoritative lock/pause state — one round trip = liveness + state sync.
- A sweep thread (`_sweep_loop` :491) marks entries OFFLINE when
  heartbeats stop.
- `derive_status` (:65) computes the visible status by priority:
  `OFFLINE > VERIFYING > LOCKED > PAUSED > IN USE > AVAILABLE > ONLINE`.

### 6.3 Commands

`send_command` (:1434) stamps a `client_commands` row, forwards the
`CMD_*` message and waits for `CMD_RESPONSE` (`_on_cmd_response` :1259) —
de-duplicated by command id, timed out after `command_timeout = 30` s, and
audited (`_audit_command` :1743). Convenience wrappers exist for lock,
unlock, logout, restart, shutdown, pause, resume and message; `bulk_command`
(:2352) fans out to many PCs and reports per-PC
`SUCCESS / FAILED / OFFLINE / TIMEOUT`. An OFFLINE PC's `CMD_UNLOCK`
is remembered as desired state and re-delivered on reconnect; other
offline bulk commands are not.

### 6.4 Privileged observation, remote control and screen share

- `[Observe]` streams screen frames to the server; `[Remote]` forwards
  **whitelisted input primitives only** (mouse move/click/scroll, key
  down/up in normalized coordinates — `sanitize_remote_events`,
  `protocol.py:597`). There is no shell and no arbitrary-command path.
- Both are gated **server-side** by `_observe_role_ok` (:1931): a
  `staff`/`maintenance` request is refused and audited as a security event
  before any stream or session exists; non-admin input batches are dropped
  (`forward_remote_input` :2321). The dashboard doesn't even build those
  controls for non-admins.
- **Screen share** (`start_screen_share` :2041) pushes the *Server
  machine's* screen to every online lab PC — the same `_observe_role_ok`
  gate, the same per-PC `Sent → Done` audit rows. One capture worker
  (`_share_worker` :2094) grabs this screen (scale 0.75, JPEG q85, 4:4:4)
  and fans each frame out at `SHARE_INTERVAL` 0.2 s; a PC whose `send`
  fails is remembered in a `lagging` set and skipped from then on, so one
  wedged client can never stall the class. STOP is delivered by the
  worker's exit path *after* its last frame on the same socket (TCP
  order), which is what makes a late frame unable to reopen a closed
  overlay. State is `share_stop` (an Event, `None` = idle) plus
  `share_targets` (every PC streamed to); `stop_screen_share` (:2079)
  only sets the event — idempotent, and shutdown sets it too.

### 6.5 Dashboard event feed

Server state changes are pushed to the UI through the `on_event` callback
into a queue drained by `AdminDashboard` (`_emit`/`_emit_state` :399-405),
so pages update live without polling the database.

---

## 7. Client runtime (`client.py`)

### 7.1 Process lifecycle

Single instance (named mutex) → Startup entry + **two watchdog tasks**
(logon + every-minute, unelevated) + **guard process** (2 s poll →
`--respawn`) → kiosk window → optional tray icon. Every kiosk screen
(login card, admin lock, pause overlay, share) also applies the HKCU
Ctrl+Alt+Del restrictions; the bare desktop clears them, as do the
panic stop, every uninstall path and Server mode. Starting the *server*
on the same machine deletes both tasks. Hotkey:
`Ctrl+Shift+Alt+M` emergency force-unlock (locked kiosk, server
unreachable — never releases a Pause).

### 7.2 Network thread (`ClientNetwork`, :416)

A worker thread (never the Tk main loop) runs:

- `_connect_loop` (:496) — connect → optional UDP discovery →
  `CLIENT_REGISTER`; reconnects forever with backoff.
- `_recv_loop` (:537) — identity-guarded frame reads dispatched to
  `handle_command` (:2518), except two deliberate bypasses: input
  batches (`_apply_remote_input`) and screen-share frames
  (`_share_frame`), which are high-frequency content, never auditable
  commands and never acked.
- Liveness: server ACKs are expected ≤ 5 s apart; a dead server is detected
  in ~20 s and the UI switches to offline behavior.
- `_sync_on_reconnect` (:3374) — on every re-registration the client pulls
  the auth roster, re-applies the current web policy and **flushes queued
  local logs**.

### 7.3 Kiosk state machine (`_KioskState`, :1023)

```
            AUTH ok                      admin lock / pause
  login ───────────────► unlocked ◄────────────────► locked / paused
   │                        │                          │
   │ first-login change     │ session bar, dashboard   │ only admin unlock /
   ▼                        ▼                          ▼ force-unlock hotkey
  change-password        normal use                 back to login on logout
```

All state writes are guarded (`__setattr__` :1063); the tests in
`_test_client_gui.py` assert every transition.

### 7.4 Local persistence (`local_store.py`)

- `lab_client.db.local_logs` — every activity event gets a client-generated
  `event_id` (PK) so server sync is **idempotent** (`INSERT OR IGNORE`);
  `pending_logs` → server `log_sync` → `mark_synced` only after ACK.
- `auth_cache` — offline login roster (§5.5).
- `meta` — sync bookkeeping.

### 7.5 OS integration

Hotkey blocking (`HotkeyBlocker` :269) for the locked kiosk, one-shot
power tokens for restart/shutdown (armed only by an authenticated admin
command), startup/watchdog registration, and an uninstall path
(`--uninstall-startup`).

### 7.6 Screen-share overlay

`CMD_SCREEN_SHARE_START` opens `_share_open` (:2850): a borderless,
topmost, screen-sized `CTkToplevel` with a black "Waiting…" label —
the PC flips into presentation mode before the first frame lands. Each
`CMD_SCREEN_SHARE_FRAME` is size-capped, decoded and letterboxed into
that window by `_share_frame` (:2897); a frame with no overlay reopens
it, so a PC that reconnects mid-share catches up. `_share_close` (:2928)
runs on STOP, on disconnect and on an emergency client stop, and `_share_tick` (:2944)
(a 1 s heartbeat) closes the overlay once frames stop for 5 s — a dead
Server can never leave a stale slide on the lab PC.

While the share is up, `_share_open` arms a hotkey blocker (recording
whether the Client's own hotkeys were idle first) so the kiosk's
hotkeys cannot fire over the class; `_share_close` releases the
blocker only if the share armed it, so a share that ends while the
app holds its own hotkeys leaves them exactly as it found them.

---

## 8. Website Access end-to-end

1. **Authoring (admin):** the Website Access page edits rules
   (`websites` table) and picks a mode — `allow_all` / `block_list` /
   `allow_only` — then **Apply Policy** bumps `web_policy_version`
   (`bump_web_policy_version`, `server.py:1550`).
2. **Push:** `push_web_filter` (:1615) sends `CMD_WEB_FILTER` with a
   versioned payload `{version, mode, blocked, allowed, upstream_dns}`
   (`build_web_policy`, `protocol.py:388`) to every PC; per-PC state is
   tracked in `web_pc_policy`.
3. **Apply (client):** `apply_web_policy` (`client.py:805`) first repairs
   the network adapter's DNS if a previous block left it stale
   (`dns_filter.repair_adapter_dns` :282), then stores the policy and
   starts enforcement. On startup, **DNS repair runs before the saved
   policy is re-applied** (`_startup_web_policy` :888).
4. **Enforcement:** `WebAccessDetector` (`web_access.py:330`) maps live
   connections to domains, asks the shared verdict
   engine (`dns_filter.decide` :84 / `domain_matches` :75) and performs a
   **guarded browser close** (`close_browser` :736) — it verifies the
   process and window before acting. Address coverage is deliberately
   broad so a real visit can actually be matched: `resolve_domain`
   (:188) maps the **union of the upstream and system resolver views**
   (browsers resolve through the system path or DoH; the views differ
   for common sites), and each rule also resolves its **www/apex entry
   point** (`_lookup_candidates` :304 — typing `youtube.com` lands the
   browser on `www.youtube.com`, whose address pool is disjoint from
   the apex), with every address attributed to the rule domain.
5. **Ack:** the client answers `WEB_POLICY_ACK` with the version (or a
   failure reason); the server only marks a PC **SYNCED** on a matching ack
   (`_on_web_policy_ack`, `server.py:1696`), shown live in the Policy
   Status table.

---

## 9. Database (`database.py`)

Connection: `sqlite3.Row` rows, `PRAGMA foreign_keys=ON`,
`journal_mode=WAL` and a 15 s busy timeout (`timeout=15`, matching
`local_store`) (:31-37). Schema changes are handled by
`_migrate_legacy_schema` (:244), so old lab DBs upgrade in place.

| Table | Holds |
|---|---|
| `users` | accounts, salted hashes, role, `must_change_password` |
| `computers` | PC registry + live telemetry (heartbeat, CPU/RAM, state) |
| `client_sessions` | login/logout, duration, status, connection type |
| `admin_activity_log` | audit trail (action, target, details, IP, time) |
| `client_commands` | every command with status + result |
| `websites` / `web_pc_policy` | rules list; per-PC mode/version/sync status |
| `inventory`, `borrow_records`, `maintenance` | equipment workflow |
| `announcements`, `messages` | communication |
| `client_logs` | synced client events (`event_id` PK → idempotent) |
| `system_settings` | key/value settings (UPSERT via `set_setting` :510) |

Seeded defaults: `server_port=8443`, `heartbeat_interval=5`,
`command_timeout=30`, `screen_observe_interval=1.0`, `screenshot_quality=70`,
`screenshot_scale=0.75`, `max_failed_logins=5`, `lockout_duration=300`,
`low_stock_threshold=3`, `web_mode=allow_all`, `web_policy_version=0`,
`upstream_dns=1.1.1.1`, `max_offline_days=7`.

---

## 10. UI layer

- **Tokens first** — `configure_theme()` (`theme.py:131`) runs before the
  first widget: dark appearance + `dark-blue` CTk theme, pinned scaling, and
  every color (`BG_DARK/CARD/ACCENT/TEXT/SUBTLE/…`), font, radius and
  spacing constant declared once. Pages never hardcode styling.
- **Shared widgets** — `components.py`: `Card`, `PaddedFrame` (padding
  shim), `CardFrame`, `TabHost`, `Toolbar`, `StatTile`, `StatusPill`,
  `Toast`, `ActionButton`, `SearchBox`, and the PIL-drawn `eye_icon` used by
  both login pages (with the `image_master` guard that keeps Tk image
  masters on the right root window).
- **CRUD framework** — `crud_frame.py`: `CRUDFrame` renders a filterable
  table + form dialog; subclasses provide accounts, inventory, website
  rules, sessions and the staff accounts page.
- **Admin console** — `AdminDashboard` (`admin_dashboard.py:176`) is a
  sidebar shell: MAIN (Dashboard, Client PCs, Sessions) · ACCOUNTS
  (Accounts, Staff & Admin) · MANAGEMENT (Inventory, Website Access,
  Messages, Announcements) · SYSTEM (Audit Trail, Maintenance, Settings) ·
  MORE (Computers, Borrowing). Staff entry and the Observe/Remote controls
  are built **only for administrators**, mirroring the server-side gates.
- **Student console** — `StudentDashboard`: announcements, messages,
  equipment borrowing; reads via `client_api` (network first, SQLite
  fallback).

---

## 11. Build and distribution

`build_exe.bat` installs `requirements.txt` (pyinstaller, psutil,
cryptography, pillow, pystray, customtkinter) and produces two **onefile,
windowed** executables from the same `main.py`:

```
server.exe  →  --name server  (+ icons/assets/pc_icons, collect customtkinter)
client.exe  →  --name client  (+ pystray hidden imports)
```

Runtime files appear **next to the EXE**: `lab_system.db` (+ WAL), auto-
generated `server.crt`/`server.key`, and — on client PCs — `lab_config.json`
(server address + last policy) and `lab_client.db`. Mode is picked from the
EXE name, so distribution is "copy the file".

---

## 12. Testing and quality gate

Three self-contained scripts (no pytest), each printing PASS/FAIL lines and
exiting non-zero on any failure. `gate.bat` runs the whole gate in order
— both suites on fresh DBs, then the two sweep tools — and stops at the
first failing stage:

| Suite | Checks | Covers |
|---|---|---|
| `_test_integration.py` | **606** | protocol framing, auth + lockout + first-login change, sessions, audit taxonomy, Website Access (push/ack/DNS repair/detection), UDP discovery, status engine, bulk commands + client ack detail (`force_login` / `login_allowed`), dashboard layout, role gating, Accounts bulk CSV upload, Send File desktop push, screen share (admin-gated fan-out, drop-on-slow, JPEG frames, ack-resolved START/STOP), remote input normalization + key translation (VkKeyScan Shift), guardrails, watchdog registration repair + self-heal, the guard watcher (one cycle, respawn argv, single-mutex, task/flag stand-down, `--watchdog`/`--respawn` gates), CAD policy hardening (recorded registry writes, apply/clear/sync), the standalone uninstaller (static + behavioural), Force Unlock = all PCs (offline unlock remembered), roster refresh on password/account change, auth gate on student requests (roster/web policy/log flush policy), account lockout (threshold + expiry + reset), borrow validation |
| `_test_client_gui.py` | **290** | kiosk state machine, lock/pause/logout, command de-dup, offline login + local queue, web policy ack, watchdog, hotkeys, observe clamps, Send File dispatch, screen-share overlay (open/paint/drop/rejoin/stop/timeout + hotkey arming), panic stop from any state, CAD policies follow every screen (login/unlock/pause/share/panic), login accepted after a no-session unlock, `auto_start` flag vs Run entry |
| `_probe_layout.py` | **50** | responsive layout at 1080×700 / 1280×780 / 1440×900 |
| `_full_sweep.py` | — | every admin page at 3 sizes + hard resizes: fails on clipped captions, page overflow or children outside the window |
| `_contrast_all.py` | — | pixel readability of every window (login, all admin pages, dialogs, student console): any heading below a 3.0 p99/p1 luminance ratio fails |

Test ports: **TCP 18443**, **UDP 18444** (= 18443+1) — never the production
defaults, so tests can run beside a live server. No manual setup is needed.

---

## 13. Configuration reference

| Where | What |
|---|---|
| `system_settings` (server DB) | All tunables from §9 — port, intervals, lockout, screenshot quality, DNS, offline window |
| `lab_config.json` (next to client EXE) | `server_ip`, `server_port`, last received `web_policy`; written only by the client's settings dialog (login-screen ⚙ / role-gated Maintenance entry) |
| `server.crt` / `server.key` | Auto-generated on first server start if missing; never hand-managed |
| CLI | `--server`, `--client`, `--uninstall-startup` |

---

## 14. Security model in one page

- **Transport:** TLS 1.2+, ECDHE/AES-GCM; self-signed chain generated
  locally — trust is "first use on your own LAN".
- **Secrets:** PBKDF2-HMAC-SHA256 × 100 000 with per-user salt; lockout
  after 5 failures; forced first-login change; factory passwords documented
  for first run only.
- **Authority:** every security decision is made by the server — the client
  cannot unlock itself (except the audited emergency hotkey for an
  unreachable server), cannot release a Pause, and mirrors server state on
  every heartbeat.
- **Least privilege:** Observe/Remote are administrator-only **at the
  server**, not just hidden in the UI; remote input is a whitelist of mouse
  and key primitives with no command execution path.
- **Audit:** logins, commands, policy changes, role-gated refusals and
  abnormal endings all write to `admin_activity_log`; clients keep a durable
  local copy of commands and events until the server acknowledges them.
- **Offline bounds:** offline logins expire after `max_offline_days` (7)
  because the roster ages out; queued events sync idempotently on reconnect.
