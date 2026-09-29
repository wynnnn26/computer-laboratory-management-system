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
| Server core | `server.py` (2.1k lines) | `LabServer` (:209): accept loop, auth, heartbeats, commands, observe/remote, website policy, audit |
| Client core | `client.py` (3.8k lines) | `ClientApp` kiosk (:1057), `ClientNetwork` (:416), command handlers, watchdog/startup, offline mode |
| Wire protocol | `protocol.py` | `MessageType` enum (:19), `Message` framing (:103), TLS helpers (:210), self-signed cert generation (:228), message builders (:281-565) |
| Server database | `database.py` | Schema (:58-241), migrations (:244), PBKDF2 hashing (:40), seeding + settings (:408-516) |
| Client database | `local_store.py` | Offline log queue + auth roster cache (:64-384) |
| Website Access | `web_access.py`, `dns_filter.py` | Connection→domain detection & guarded close (:330); pure verdict engine + DNS repair (:84, :282) |
| Admin UI | `admin_dashboard.py` (3.7k lines) | `AdminDashboard` console (:168) — sidebar + 13 pages + remote controls |
| Student UI | `student_dashboard.py` | Announcements / messages / borrowing (:38) |
| Data bridge | `client_api.py` | Network-first fetches for the student UI with SQLite fallback (:17) |
| UI framework | `components.py`, `theme.py`, `utils.py`, `crud_frame.py` | Shared widgets, design tokens, styling helpers, reusable CRUD tables/pages |
| Tests | `_test_integration.py`, `_test_client_gui.py`, `_probe_layout.py` | 486 + 251 + 50 checks, self-contained |
| Build | `build_exe.bat`, `requirements.txt` | PyInstaller onefile builds for both apps |
| Assets | `assets/`, `pc_icons/`, `app_icon.ico` | Logo, 8 PC status icons, window icon |
| Design docs | `design-system/lab-management-system/MASTER.md` | Token/component spec behind `theme.py` |

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
2. Removes leftover client watchdog tasks (a server PC must never open a
   kiosk over the console).
3. `LabServer.start()` (`server.py:241`):
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

### 3.3 Client mode (`run_client`, `client.py:3760`)

1. `acquire_single_instance()` (:915) — named mutex
   `Local\ComputerLaboratoryClient`; a second launch re-opens the first.
2. Registers two unelevated Windows scheduled tasks — one *at log on*, one
   *every minute* — that relaunch the kiosk if the process ever dies
   (`register_watchdog` :3641).
3. Builds `ClientApp` (fullscreen login card) and starts `ClientNetwork`
   with the address from `lab_config.json`, falling back to **UDP
   discovery** if no address is stored (`discover_server_ip` :209).

---

## 4. Wire protocol (`protocol.py`)

**Framing:** every message is `4-byte big-endian length` + UTF-8 JSON body,
capped at **10 MB** (`protocol.py:115-201`). Each `Message` carries `type`,
`payload`, an 8-char `msg_id` and a `timestamp` (:103).

**Transport:** TLS 1.2+ with ECDHE/AES-GCM ciphers (:210-225). The server
loads its self-signed chain; clients accept it without hostname
verification (LAN deployment). Frames are read/written through
`TLSSocketWrapper` (:144).

**Discovery:** UDP broadcast magic `LAB_SYSTEM_DISCOVER` → reply
`LAB_SYSTEM_SERVER` on port **TCP+1 = 8444** (:93-99); the server answers in
`_responder` (`server.py:335`), the client probes with a 1.5 s timeout.

**Message catalogue** (`MessageType`, `protocol.py:19-84`):

| Category | Types |
|---|---|
| Auth | `AUTH_REQUEST`, `AUTH_RESPONSE`, `AUTH_CHALLENGE` |
| First-login change | `PASSWORD_CHANGE_REQUEST`, `PASSWORD_CHANGE_RESPONSE` |
| Lifecycle | `CLIENT_REGISTER`, `CLIENT_HEARTBEAT`, `CLIENT_STATUS`, `CLIENT_DISCONNECT` |
| Commands (server→client) | `CMD_LOCK`, `CMD_UNLOCK`, `CMD_LOGOUT`, `CMD_RESTART`, `CMD_SHUTDOWN`, `CMD_PAUSE`, `CMD_RESUME`, `CMD_SCREEN_OBSERVE_START/STOP`, `CMD_SCREENSHOT`, `CMD_SEND_MESSAGE`, `CMD_EXECUTE`, `CMD_WEB_FILTER` |
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
2. The server (`_do_auth`, `server.py:829`):
   - verifies the password against `users.password` stored as
     **PBKDF2-HMAC-SHA256, 100 000 iterations, 16-byte random salt**
     (`salt_hex$hash_hex`, `database.py:40-56`),
   - enforces lockout: `max_failed_logins = 5` within
     `lockout_duration = 300` s,
   - audits `login_success` / `login_failure` with IP and role,
   - returns the account + role + `must_change_password` flag.
3. If `must_change_password` is set, the client is forced through the
   **change-password screen** (`PASSWORD_CHANGE_*`); nothing else is
   reachable until it succeeds (`server.py:904`).
4. On success the client starts a session (`SESSION_START` → a
   `client_sessions` row) and stops it on logout/server-side end.
5. **Offline login:** the server periodically pushes an auth roster
   (`auth_roster` sync, `server.py:1165`) that the client caches in
   `lab_client.db.auth_cache`. If the server is unreachable, the kiosk
   verifies locally (`local_store.verify_offline_login` :309) as long as the
   roster is younger than `max_offline_days = 7`. All offline activity is
   queued and replayed later (§7.4).

**Admin Lock is server-side:** once a PC is locked, typing credentials on
the kiosk cannot release it — only an explicit Admin Unlock (`force_login`)
does (`server.py:1756`).

---

## 6. Server runtime (`LabServer`, `server.py:209`)

### 6.1 Connections and registry

`_accept_loop` (:420) hands each socket to a `_client_thread` (:431) that
wraps it in TLS and reads frames until EOF. Every live connection is a
`ClientEntry` (:136) — identity, snapshot, send/close — registered by
`CLIENT_REGISTER` (`_on_register` :550) into the `computers` table (IP, MAC,
hostname, CPU/RAM, version…). Disconnects flow through
`_on_connection_lost` (:774) with a **grace period** before crash
escalation (`_escalate_disconnect` :802).

**Desired state is persisted** (`_load_desired`/`_save_desired` :640-653):
lock/pause decisions survive server restarts and are re-applied to clients
via the heartbeat reply.

### 6.2 Heartbeat and status

- Client sends `CLIENT_HEARTBEAT` **every 5 s** (`client.py:3491`) with
  CPU/RAM/user/connection type; the server answers `PONG` carrying the
  authoritative lock/pause state — one round trip = liveness + state sync.
- A sweep thread (`_sweep_loop` :480) marks entries OFFLINE when
  heartbeats stop.
- `derive_status` (:65) computes the visible status by priority:
  `OFFLINE > VERIFYING > LOCKED > PAUSED > IN USE > AVAILABLE > ONLINE`.

### 6.3 Commands

`send_command` (:1421) stamps a `client_commands` row, forwards the
`CMD_*` message and waits for `CMD_RESPONSE` (`_on_cmd_response` :1248) —
de-duplicated by command id, timed out after `command_timeout = 30` s, and
audited (`_audit_command` :1724). Convenience wrappers exist for lock,
unlock, logout, restart, shutdown, pause, resume and message; `bulk_command`
(:1364) fans out to many PCs and reports per-PC
`SUCCESS / FAILED / OFFLINE / TIMEOUT`.

### 6.4 Privileged observation and remote control

- `[Observe]` streams screen frames to the server; `[Remote]` forwards
  **whitelisted input primitives only** (mouse move/click/scroll, key
  down/up in normalized coordinates — `sanitize_remote_events`,
  `protocol.py:476`). There is no shell and no arbitrary-command path.
- Both are gated **server-side** by `_observe_role_ok` (:1868): a
  `staff`/`maintenance` request is refused and audited as a security event
  before any stream or session exists; non-admin input batches are dropped
  (`forward_remote_input` :2133). The dashboard doesn't even build those
  controls for non-admins.

### 6.5 Dashboard event feed

Server state changes are pushed to the UI through the `on_event` callback
into a queue drained by `AdminDashboard` (`_emit`/`_emit_state` :388-394),
so pages update live without polling the database.

---

## 7. Client runtime (`client.py`)

### 7.1 Process lifecycle

Single instance (named mutex) → Startup entry + **two watchdog tasks**
(logon + every-minute, unelevated) → kiosk window → optional tray icon.
Starting the *server* on the same machine deletes both tasks. Hotkeys:
`Ctrl+Shift+Alt+M` emergency force-unlock (locked kiosk, server
unreachable — never releases a Pause), `Ctrl+Shift+Alt+K` panic stop
(unlocked kiosk → immediate logout + end observation/remote).

### 7.2 Network thread (`ClientNetwork`, :416)

A worker thread (never the Tk main loop) runs:

- `_connect_loop` (:496) — connect → optional UDP discovery →
  `CLIENT_REGISTER`; reconnects forever with backoff.
- `_recv_loop` (:537) — identity-guarded frame reads dispatched to
  `handle_command` (:2474).
- Liveness: server ACKs are expected ≤ 5 s apart; a dead server is detected
  in ~20 s and the UI switches to offline behavior.
- `_sync_on_reconnect` (:3173) — on every re-registration the client pulls
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

---

## 8. Website Access end-to-end

1. **Authoring (admin):** the Website Access page edits rules
   (`websites` table) and picks a mode — `allow_all` / `block_list` /
   `allow_only` — then **Apply Policy** bumps `web_policy_version`
   (`bump_web_policy_version`, `server.py:1531`).
2. **Push:** `push_web_filter` (:1596) sends `CMD_WEB_FILTER` with a
   versioned payload `{version, mode, blocked, allowed, upstream_dns}`
   (`build_web_policy`, `protocol.py:347`) to every PC; per-PC state is
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
   (`_on_web_policy_ack`, `server.py:1677`), shown live in the Policy
   Status table.

---

## 9. Database (`database.py`)

Connection: `sqlite3.Row` rows, `PRAGMA foreign_keys=ON`,
`journal_mode=WAL` (:31-36). Schema changes are handled by
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
- **Admin console** — `AdminDashboard` (`admin_dashboard.py:168`) is a
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
exiting non-zero on any failure:

| Suite | Checks | Covers |
|---|---|---|
| `_test_integration.py` | **486** | protocol framing, auth + lockout + first-login change, sessions, audit taxonomy, Website Access (push/ack/DNS repair/detection), UDP discovery, status engine, bulk commands, dashboard layout, role gating |
| `_test_client_gui.py` | **251** | kiosk state machine, lock/pause/logout, command de-dup, offline login + local queue, web policy ack, watchdog, hotkeys, observe clamps |
| `_probe_layout.py` | **50** | responsive layout at 1080×700 / 1280×780 / 1440×900 |

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
