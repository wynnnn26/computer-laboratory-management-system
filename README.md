# Computer Laboratory Management System — LAN Edition

> **How it works:** see [ARCHITECTURE.md](ARCHITECTURE.md) for a full
> walkthrough of the runtime architecture — startup and mode detection, the
> TLS message protocol, authentication/session flow, server and client
> lifecycles, Website Access enforcement, database schema, UI layer and the
> build/test pipeline.

A desktop application (Python + CustomTkinter + SQLite + TCP/TLS sockets) for
managing a school computer laboratory / internet café over a **LAN only**
(no cloud, no REST API), inspired by PanCafe Pro:

- **Look & feel:** every window — Server/Admin console, Client kiosk and
  Student dashboard — is drawn with **CustomTkinter** in a modern dark
  theme. All colours, fonts, corner radii, spacing and status tones are
  declared once in **`theme.py`** (re-exported by `utils.py`), so no page
  file hardcodes styling. Rounded cards, a sidebar whose active item is
  highlighted, a compact header carrying the logo, logged-in user, role and
  live server status, tabbed detail panels and a right-hand PC details
  column instead of popups. `components.py` holds the shared widgets
  (Card, CardFrame, TabHost, Toolbar, StatTile, StatusPill, Toast, ...).

- **Server / Admin PC** hosts the central SQLite database and a TLS TCP
  server; the dashboard is built around **PC management**: a compact
  stats strip (Total PCs / Online / In Use / Available / Paused /
  Locked), status filter chips with live counts (the chip row wraps so
  nothing is ever hidden under the details panel), a responsive card
  grid (monitor icon, PC name, status, IP, user), a compact bulk-action
  toolbar and a right-side PC details panel, plus a live table of every
  connected client PC (PC Name, IP, Hostname, Online/Offline, Current
  User, CPU %, RAM %, Last Seen) with remote control.
- **Client PCs** boot into a **mandatory fullscreen login screen** that
  cannot be bypassed — a clean centered card (title, subtle server
  status + `ip:port`, Student ID, password with Show/Hide, `LOGIN`,
  `PC-01 • ONLINE / LAN Connection` footer). The PC stays locked until
  an account is authenticated by the Server. Clients auto-register,
  heartbeat every 5 s and **auto-reconnect + re-register** whenever the
  server restarts.
- **Roles:** `admin`, `staff`, `maintenance`, `student` (customer).
- **Remote controls:** lock, unlock (Admin Force Login), logout, pause,
  resume, restart, shutdown, message popup, one-shot screenshot, live
  screen observation, `[ 🖱 Remote ]` (mouse + keyboard control) and
  `📤 Send File` (push a document to the selected PCs' Desktop) —
  every command is acknowledged and its success/failure is shown as a
  non-blocking toast.
- **`[ 🖱 Remote ]` remote control:** sits next to the other client
  controls and is only enabled for **exactly one selected and online
  PC**. It asks for confirmation first (PC name, current client user, IP
  address and a warning that input will be forwarded), then opens a
  *Remote Control* window with the PC/user/IP header, the live screen,
  `Connection: LIVE` and `[Stop Remote Control]`. Only whitelisted
  primitives are forwarded — mouse move / click / scroll and key
  down/up in normalised coordinates; there is no shell, no "type text"
  and no arbitrary-command path anywhere. The Client shows a persistent
  **REMOTE CONTROL ACTIVE** indicator for as long as the input gate is
  open, releases any held key on stop and disarms the moment the link
  drops. Starting, stopping and every abnormal end (client offline,
  disconnect, console closed) are audited with admin, PC, client user,
  connection type, session/command id, result and failure reason.
- **`[ 👁 Observe ]` and `[ 🖱 Remote ]` are ADMINISTRATOR-only**: the
  Server — not just the UI — refuses either one for a STAFF or
  MAINTENANCE role before any stream or session can exist, writes the
  refusal to the audit trail as the security event it is, and drops a
  non-administrator's input batch without it ever reaching the PC.
  The Dashboard correspondingly does not build those two controls for
  anyone who is not an administrator (it still shows Pause, Restart,
  Shutdown and the rest). Stopping is deliberately never role-gated, so
  a machine can always be released and never left under remote control
  by someone who is no longer allowed to hold it.
  `[Observe]` is unchanged and stays view-only.
- **`📺 Share Screen` pushes the Server machine's own screen to every
  online lab PC** — the teaching mode: PowerPoint, slides and live
  coding fill each lab monitor full-screen, borderless and topmost,
  letterboxed to the display. The Server captures its own screen
  (scale 0.75, JPEG quality 85 at 4:4:4 chroma so small text stays
  readable) and fans each frame out to every online PC about five
  times a second; a PC whose send fails is dropped from the fan-out
  instead of stalling the class, and the STOP is delivered after the
  last frame on the same socket (TCP order), so no late frame can
  reopen a closed overlay. Each Client closes its overlay on STOP,
  after a 5-second frame timeout, on disconnect and on an emergency
  client stop.
  It is **ADMINISTRATOR-only AT THE SERVER** (the same
  `_observe_role_ok` gate as Observe/Remote), the start/stop is
  audited per PC with the usual Sent → Done acknowledgement, and it
  toggles from the Client PCs toolbar (`📺 Share Screen` ⇄
  `⏹ Stop Sharing`) next to *Send to All*.
- **`[ 📤 Send File ]` pushes one file to the selected PCs' Desktop**:
  pick the file once and send it to any number of selected PCs (Ctrl+A =
  every PC); several PCs report through the existing per-PC
  `SUCCESS / OFFLINE / FAILED` bulk result list, a single PC gets a plain
  toast. Documents only — `.exe .bat .cmd .ps1 .vbs .js .msi .scr .lnk`
  are refused at the Server **and** re-refused on the Client, the size is
  capped at 5 MB, and the filename is reduced to a safe basename with a
  collision rename (`file (1).ext`). ADMINISTRATOR-only: the button is
  not even built for other roles and the Server refuses the call as an
  audited security event. The audit rows record the file name and size,
  never the payload bytes.
- **Pause never expires on its own** — it stays active until the admin
  explicitly presses Resume (and survives client/server restarts).
- **Admin Lock cannot be bypassed by typing credentials**: only an
  explicit Admin Unlock (force login) releases the PC. Force Unlock
  always targets **every** PC regardless of the current selection, and
  an offline PC's unlock is remembered and applied when it reconnects.
  Each result line carries the client's own ack —
  `PC1: SUCCESS  (force_login)` (a session was restored) vs
  `(login_allowed)` (the PC landed on its login card) — so both
  outcomes are visible per PC, and a PC with **no session accepts a
  normal login immediately** after such an unlock (never the
  "locked by the administrator" refusal).
- **Emergency hotkey Ctrl+Shift+Alt+M**: a locked Client kiosk whose
  Server is unreachable can be force-unlocked locally - it reuses the
  normal force-unlock UI path (a still-open session is restored),
  records `hotkey_force_unlock` in the audit trail whenever the link is
  up (local-only when offline), never releases a Pause (only the
  admin's Resume does) and cannot bypass the server-enforced
  first-login password change.
- **Client background + single instance:** the kiosk can be minimized
  to a notification-area (tray) icon with an *Open Dashboard* action,
  and a second launch of the same app re-opens the first one instead of
  starting a duplicate (named mutex `Local\ComputerLaboratoryClient`).
- **The Client behaves like a normal desktop application:** the session
  bar used to be a frameless `overrideredirect` + `topmost` strip pinned
  over the top of the screen, which floated above File Explorer,
  browsers, Office and IDEs. It is now an ordinary window — no frameless
  override, no always-on-top, a real title bar and a taskbar button — so
  other applications come in front of it like any other desktop app.
  Minimizing or closing the bar (and the Dashboard) only hides that
  window: it is never a logout and never stops the Client process, and
  every background duty — server connection, heartbeat, session, website
  policy, lock/pause state, commands, local logging, offline sync and
  reconnection — keeps running. The Dashboard still appears only when it
  is intentionally opened.
- **Server IP/Port has no gear on the main display:** the ⚙ button was
  removed from the session bar. Configuration is still reachable from
  the two secondary places instead — the **login-screen ⚙** (first run /
  kiosk) and a role-gated **`Maintenance`** entry in the Client
  Dashboard's header, which only exists for `admin` and `maintenance`
  accounts. Students never see a settings entry; the maintenance dialog
  itself (`lab_config.json` only) is unchanged.
- **Watchdog:** two Windows scheduled tasks — one *at log on* and one
  *every minute*, both running unelevated — relaunch the Client if it
  ever stops, so a lab PC returns to the kiosk after a crash, a
  sign-out or a reboot. Starting the Server on the same machine removes
  both tasks so it can never open a kiosk over the Admin console.
- **Observation is silent on the Client:** an active `[Observe]` stream
  no longer raises any banner, popup or toast on the watched PC — the
  overlay's source was removed outright. Observation remains fully
  visible on the Server/Admin side (the *Live Screen* viewer plus the
  `OBSERVING / Observing PC-XX` status) and in the audit trail.
  By contrast **remote control is never hidden** (see the `[ 🖱 Remote ]`
  bullet above), because the person at the PC is no longer in sole
  control of the mouse and keyboard.
- **Durable remote-control audit:** every remote command the Client
  receives is written to its own local log (command type, command id
  and whether the link was up to carry the acknowledgement), so the
  trail still exists if the PC goes offline before it can reply. On the
  server side an observation audit row is only marked *Done* when that
  acknowledgement actually arrives — never before.
- **Uninstall Client:** from *Server Settings*, an `admin` or
  `maintenance` account can export the local log to a CSV and remove the
  PC's Windows auto-start and watchdog tasks, behind a confirmation.
  It never deletes the application files or the database. For a full
  removal there is also the standalone **`dist\uninstall.exe`** (see
  *Client lifecycle* below).
- Every user session, client event and admin/staff action is recorded in
  the database (sessions table + activity audit log + command audit
  table) and is searchable/filterable in the **Audit Trail**.
- All existing standalone features are preserved (minus *Lab
  Attendance* and the *OJT system*, both intentionally removed):
  computers, inventory, equipment borrowing, maintenance,
  announcements, messaging, and CSV export on every tab. They live in
  a collapsed **More** group at the bottom of the sidebar (hidden by
  default, one click away). The Admin sidebar itself starts directly at
  **MENU → MONITORING** — the circular logo + *LABORATORY SYSTEM*
  branding block was removed from it (the header logo and title are
  unchanged).
- **Notifications:** small auto-dismissing bottom-right toasts for normal
  events (including CRUD save results and CSV export); dialogs are
  reserved for critical confirmations only
  (Restart / Shutdown / Delete / Force Logout / applying policy to
  multiple PCs) plus the data-entry forms (Add Website, Add and Edit
  Account).
- **Server-authoritative PC status engine:** every PC shows exactly one
  of `OFFLINE > VERIFYING > LOCKED > PAUSED > IN USE > AVAILABLE >
  ONLINE` (computed on the server in that priority order; `UNKNOWN`
  before anything is known) — the dashboard never guesses a status and
  renders only server-pushed state. Only PCs that actually connected to
  the server are listed; there are no pre-seeded fake offline rows. Each
  status has its own icon (`pc_icons/`: white outline monitor, colored
  screen, status dot) and one shared palette everywhere (green ONLINE,
  red OFFLINE, blue IN USE, orange PAUSED, purple LOCKED, gray
  AVAILABLE, cyan VERIFYING, dark-gray UNKNOWN).
- **PC-management dashboard:** the *Dashboard* (formerly *Overview*)
  leads with 6 compact PC statistics (Total PCs, Online, In Use,
  Available, Paused, Locked — Total PCs counts every PC that ever
  connected, so the sub-counts always add up), a `Search PCs...`
  toolbar, status **filter chips with live counts**
  (`All · Online · Offline · In Use · Paused · Locked · Available ·
  Verifying · Unknown`), and a responsive scrollable **PC card grid**
  where the monitor icon is the centerpiece (icon, PC name, status, IP,
  current user).
- **Sidebar:** the 7 primary entries — Dashboard, Client PCs, Sessions,
  Accounts, Staff & Admin, Audit Trail, Settings — plus the collapsed
  *More* group; the menu scrolls, so every entry stays reachable at any
  window size.
- **Sessions page (read-only monitor):** summary cards, instant
  Student/PC/Status filters and the server-recorded session history.
  Sessions are **never edited here** — the Server writes them; select a
  row to open the *Session Details* panel underneath the table
  (duration, connection type, PC and account) and use the two
  contextual actions: **Force Logout** (confirm dialog, audited) and
  **View Activity** (jumps to the Audit Trail pre-filtered on that
  account). Statuses are displayed as `ACTIVE / COMPLETED / FORCED
  LOGOUT / TERMINATED`; Export CSV stays available.
- **Accounts page (search → select → Account Details):** four summary
  cards (Total / Active / Disabled / Accounts in Session), instant
  Search / Status / Course filters with Clear, and the account table —
  **the password column never appears anywhere**. Row actions are
  *View Details*, *Edit*, *Delete*, *Refresh* and *Export CSV*, plus the
  accent **+ Add Account** dialog and **Bulk Upload** — import a CSV of
  student accounts (header row with at least *Student ID* and *Full
  Name*; the export's own column names work as-is, one account per
  row): blank password fields get the factory default like **+ Add
  Account** does, duplicate Student IDs (in the file or already in the
  database) are skipped, missing required fields become errors, and a
  summary dialog reports **Successfully Added / Skipped / Errors** with
  one detail line per row. *View Details* opens an **Account
  Details** window with four tabs: **Overview**, **Sessions**,
  **Activity Logs** and **PC Usage** (sessions grouped per PC). Add
  saves a salted hash with `role=student` and
  `must_change_password=1`; a blank password on **Add** falls back to
  the factory default `password123` (hashed, flagged and refused as a
  chosen password), a blank password on **Edit** keeps the stored
  hash; Delete is the only confirmation.
- **Staff & Admin page:** the same account CRUD (staff, admin and
  maintenance roles) **plus the very same Account Details window** —
  *View Details* opens Overview / Sessions / Activity Logs / PC Usage
  for the selected staff/admin/maintenance account. The window is
  shared with the Accounts page rather than duplicated, and never shows
  a password. Adding an account here requires a password, so a blank
  field can never produce an account nobody can sign in with.
- **Bulk control:** click, Ctrl+Click, Ctrl+A, *Select All* and *Clear*
  build a selection (`Selected: N PCs`, safe across
  filter/sort/refresh), then the compact toolbar's Pause, Resume,
  Logout, Lock, Force Unlock, Restart or SHUTDOWN run for the whole
  selection with a per-PC `SUCCESS / FAILED / OFFLINE / TIMEOUT` result
  list (`BULK_<ACTION>` audit rows). Restart and Shutdown are the only
  confirmations — they list the affected PCs.
- **Right-side PC details panel (Dashboard *and* Client PCs):**
  selecting a PC updates a persistent right-hand column with **PC
  INFORMATION** (PC name, IP, hostname, status, CPU, RAM, last seen,
  last heartbeat, client version) and **CURRENT USER** (username, full
  name, first/last name, role, login time, session status) — no
  popups, never passwords; it scrolls instead of clipping.
- **Client login card:** a clean centered card on the kiosk with the
  title, the live server indicator — `● Server Connected` while the
  link is up, `○ Server Offline — Local Mode` when the kiosk runs on
  its local cache (offline sign-in available) — plus `ip:port`,
  Student ID, password with a **Show/Hide** toggle, a full width
  `LOGIN` button and a footer (`PC-01 • ONLINE / LAN Connection`).
  The security model is unchanged — fullscreen, hotkeys blocked,
  locked until the Server authenticates; with the Server unreachable a
  roster-synced account can sign in from the cache and every event is
  queued locally until the link returns (see *Offline resilience*
  under Architecture).
- **Client-side admin login:** validated by the server and shown on the
  session bar as `Current User: … / Role: ADMINISTRATOR`, but it
  stays on the kiosk UI and never opens the Server/Admin Dashboard
  (audited as `client_admin_login` / `client_admin_logout`).

## Default login credentials (created automatically on first run)

| Role    | Username       | Password     |
|---------|----------------|--------------|
| Admin   | `admin`        | `admin123`   |
| Staff   | `staff`        | `staff123`   |
| Student | `2023-00001`   | `student123` |

A **Maintenance** account is *not* created automatically and its
password exists nowhere in the code: an administrator creates it from
the *Staff & Admin* page (username, password and role are chosen at
creation time).

**Change these after your first login** — the *Staff & Admin* page
(admin only) and *Accounts* page manage all accounts. Passwords
are salted and hashed with PBKDF2-HMAC-SHA256 (100k iterations) and are
never stored or transmitted in plain text (the LAN transport itself is
TLS). Deleting, disabling or updating an account automatically logs that
user out of any active client session.

Fresh client (student) accounts are created from the *Accounts* page's
**+ Add Account** dialog. Leaving **Password** blank uses the factory
default `password123` — it is salted and hashed on save (never stored,
shown or echoed back) — and the account is flagged
`must_change_password`, so it must be replaced on its first Client
login (see *First-login password* under Architecture). Typing a
password stores that one instead. Admin and staff accounts are never
forced; on the *Staff & Admin* page a password is required when adding,
because a blank one there would create an account nobody can sign in
with.

## Architecture

```
   ADMIN CONSOLE   (admin / staff account)
   ┌────────────────────────────────┐
   │    admin_dashboard.py          │   login → Admin Console
   └───────────────────┬────────────┘
                       │  runs on the Server PC (same process)
                       ▼
   ┌────────────────────────────────────────────┐
   │  SERVER PC                                 │
   │  server.py   LabServer                     │
   │              TLS TCP   :8443               │
   │              UDP       :8444  (discovery)  │
   │  lab_system.db  (central SQLite)           │
   └───────────────────┬────────────────────────┘
                       │
   ════════════════════╪═════════════════════════════  LAN (Ethernet / Wi-Fi)
                       │
                       │  heartbeats · auth · sessions · stu queries
                       │  commands: lock, unlock, logout, pause,
                       │  resume, restart, shutdown, message,
                       │  screenshot, live screen observation
         ┌─────────────┼─────────────┐
         ▼             ▼             ▼
    ┌─────────┐   ┌─────────┐   ┌─────────┐
    │  PC-01  │   │  PC-02  │   │  PC-03  │
    │ client  │   │ client  │   │ client  │
    │  .py    │   │  .py    │   │  .py    │
    │  kiosk  │   │  kiosk  │   │  kiosk  │
    └─────────┘   └─────────┘   └─────────┘
      each PC keeps its own lab_client.db - a local event log plus a
      cached login roster, so it keeps working through a disconnect
```

- **Transport:** length-prefixed JSON messages over TLS sockets
  (`protocol.py`). No REST, no cloud — LAN only.
- **Central DB on the server only** — clients never touch SQLite; all
  student-facing data (announcements, messages, borrow requests) is
  forwarded through purpose-built parameterized queries
  (`client_api.py` → `STU_REQUEST` on the server).
- **Mandatory client login:** the kiosk window is fullscreen,
  override-redirect, always-on-top, and a low-level Windows keyboard hook
  swallows Alt+Tab, Win-key, Ctrl+Esc, Alt+F4, Alt+Space while locked.
  Invalid credentials keep the PC locked; logout or a remote admin lock
  returns it to the login screen; an admin lock blocks logins until the
  admin unlocks; an unlock restores an already-authenticated session
  (Admin Force Login) or, with no session, simply allows a normal login.
- **Sessions:** a successful client login opens a session record
  automatically; logout/disconnect closes it with a duration.
- **Logout vs. close vs. crash (event taxonomy):** four distinct,
  separately-audited events. *Normal Logout* — the explicit **Log Out**
  button (spec rename of "Re-lock": it ends the whole session —
  `client_logout` + session end + the full session-end checklist). *Client Closed* — the
  Client Dashboard's title-bar X closes only that window (the session
  keeps running; reopen it from the session bar) and records
  `client_closed`, never a logout. *Network Disconnect* — the TCP link
  drops: recorded as `network_disconnect`, the PC goes OFFLINE and its
  sessions close as `Disconnected`. *Client Crash* — a drop with no
  reconnect inside the grace period escalates to `client_crash`;
  reconnecting in time records `client_reconnect` instead. Minimizing
  any window never logs the user out.
- **First-login password:** new client (student) accounts are created
  flagged (`users.must_change_password`). Their first Client login is
  authenticated by the server but answered with `must_change_password`,
  so the kiosk shows a forced *Change Password* card and **no session
  starts** (the server refuses `session_start`) until a valid new
  password (6+ characters, not `password123`, different from the current
  one) is confirmed. Only PBKDF2 hashes are stored and the change is
  audited as `password_changed`. Admin/staff accounts are never forced.
- **Account changes live-sync:** creating, editing or deleting an account
  still force-logs-out any live session, and now also pushes an
  `account_changed` event through the server channel that refreshes the
  Accounts, Staff & Admin, Sessions and Audit Trail views immediately -
  no polling wait, and every listener sees the change.
- **Heartbeats (5 s):** status, CPU %, RAM %, IP, hostname, logged-in
  user — stored on the `computers` row; the server **acknowledges every
  heartbeat** (PONG + authoritative lock/pause state), so a dead or
  restarted server is detected within ~20 s and the client reconnects
  and re-registers by itself (no more stuck "Verifying…").
- **UDP server discovery:** the Client broadcasts a discovery probe on
  UDP **8444** (TCP port + 1) at startup; the Server answers with its
  LAN address, so kiosks find it even when the saved `server_ip` is
  stale or missing. No answer within ~1.5 s falls back to the saved
  `server_ip`, and an address the server announced is persisted to
  `lab_config.json` for the next start.
- **Shutdown safety:** OS restart/shutdown runs in exactly one place in
  the client and only when a one-shot token was armed by an explicit
  authenticated admin command. It is **never automatic**: heartbeat loss,
  timeouts, reconnect failures, logout and closing the window cannot
  reach it, and the Server's own crash escalation (an unanswered
  disconnect after the grace period) only writes a `client_crash` event —
  it never sends a power command of its own. On the console, Restart and
  Shutdown are the two controls that always ask first, listing the
  affected PCs, and issue nothing if the operator declines. Every
  shutdown/restart is audited with admin, PC, timestamp and result.
- **Client lifecycle (background, watchdog, guard, uninstall):** the kiosk is a
  single instance per user (named mutex `Local\ComputerLaboratoryClient`)
  and can be parked in the notification area with an *Open Dashboard*
  action. Two scheduled tasks created at first run — **Computer
  Laboratory Client Logon** (`/SC ONLOGON`) and **Computer Laboratory
  Client Watchdog** (`/SC MINUTE /MO 1`), both unelevated — relaunch it
  with `--watchdog` if it ever stops, so a lab PC returns to the kiosk
  after a crash, a sign-out, a reboot or a Task Manager kill (worst case
  a minute later). On top of that, every kiosk start launches a tiny
  **guard process** (`client.exe --guard`, one per PC behind its own
  named mutex) that notices a kill within **2 seconds** and relaunches
  the kiosk as the quiet `--respawn` — Task Scheduler refuses any
  repetition below one minute (a `PT10S` interval is rejected outright
  by the service), so without the guard a kill would expose the desktop
  for up to 60 s. The guard never stacks respawns while one is still
  booting, and stands down by itself when the watchdog task it mirrors
  is removed (panic stop, uninstall, Server mode) or when auto-start is
  switched off — it can never undo any of them. Every registration — at
  start-up, then every 15 minutes while the kiosk runs — compares each
  task's stored command with this app and repairs a stale or deleted
  task (a refused registration lands in the local log as a throttled
  WARN); starting the Server on that same machine removes both tasks so
  a kiosk can never open on top of the Admin console. An uninstall also
  writes `auto_start: false` into `lab_config.json`, which both
  registration choke points honour, so the 15-minute self-heal can never
  resurrect an uninstall — only a hand start flips the flag back on.
  `python main.py --uninstall-startup` removes the Windows Startup
  entry, both tasks, the auto-start flag and the Ctrl+Alt+Del
  restrictions, and exits without opening the kiosk; the same cleanup is
  available from inside the app under
  *Server Settings → Maintenance → [Uninstall Client]* (admin and
  maintenance roles only, behind a confirmation), which exports the
  local log to a CSV first. Neither path ever deletes application files
  or the database. The standalone **`dist\uninstall.exe`** (built by
  `build_exe.bat`, requests admin) is the path that does: it removes
  both watchdog tasks, the Startup entry, the Ctrl+Alt+Del restrictions
  (the registry survives file deletion), **stops every running
  `client.exe`**, exports the local log to a CSV, deletes the client
  files (`client.exe`, `lab_client.db*`, `lab_config.json`) and finally
  deletes itself — each step reported honestly in a result dialog.
- **Screen observation + banner:** the server creates the observation
  audit row as *Sent* and only flips it to *Done* when the client
  actually acknowledges it (a target that is offline leaves it *Failed*
  with the reason). The client clamps the requested interval, quality
  and scale, idles instead of capturing while the link is down, and
  shows a top-most **OBSERVING** banner for exactly as long as a stream
  is live.
- **Remote-command trail:** after the de-duplication check, every
  command the client receives is appended to its own `lab_client.db`
  (`Remote command received`, command type, command id and whether the
  link was up to carry the acknowledgement), so the trail still exists
  if the PC goes offline before it can reply — and a repeated command
  id is never recorded twice.
- **Website Access (modes, per-PC policy, versioned sync):** the
  *Website Access* page (More group) is a simple control panel — a
  subtitle, three **mode option-cards** (click to select, accent
  outline on the active one), the rules table and the policy status
  underneath. The three modes are **ALLOW ALL** (nothing filtered),
  **BLOCK LIST** (only the listed domains) and **ALLOW ONLY**
  (everything unlisted is refused). Rules live in **one unified
  table** — `Domain | Category | Action | Status | Notes` — with a
  `+ Add Website` dialog (a pasted URL is normalized to a bare domain),
  instant Category / Action / Status filters, multi-select delete and
  Export CSV. Rules match by **suffix on label boundaries**:
  `example.com` blocks
  `example.com`, `www.example.com` and `a.b.example.com` but never
  `notexample.com`. A mode is applied to **All / Selected / Individual
  / Group** PCs (scope radios + the accent **Apply Policy** button;
  applying to more than one PC is the only confirmation) and the
  `Policy Status` table below shows `PC | Mode | Policy Status`;
  a row only reads `SYNCED` after that PC acknowledges exactly
  the pushed policy version (`SYNCING` while in flight, `FAILED` with
  the client's own reason, `OFFLINE` for unreachable PCs) — never
  before. Enforcement runs on the Client as **connection detection**
  (`web_access.py`): the Client resolves the watched names itself,
  matches live sockets against those addresses, attributes a match to
  the domain and then to the responsible browser (Chrome / Edge /
  Firefox) and closes **only that browser** — never a blocked domain on
  its own, never an unrelated application, and never when the
  attribution is uncertain (the event is then recorded as `UNRESOLVED`
  and nothing is touched). Nothing is written to the hosts file and the
  adapter's DNS is never repointed: the local DNS proxy that used to do
  that is gone, because a run killed before its restore left the PC
  with no name resolution at all. A Client that cannot read who owns a
  socket reports `FAILED` to the server instead of pretending to be
  synced. Every rules change is audited (`webfilter_change`) and
  bumps a monotonically increasing policy version.
- **Inventory:** the *Inventory* page (More group) tracks lab
  equipment with a status workflow (Available / In Use / Borrowed /
  Damaged / Lost / Maintenance / Out of Stock) and nine actions (add,
  edit, delete, increase, decrease, assign, return, mark damaged, mark
  lost) on top of a six-tile statistics strip (Total Items /
  Available / In Use / Borrowed / Damaged / Low Stock). A low-stock
  warning names the items at or below `system_settings.low_stock_threshold`
  (default 3). Normal actions report through toasts; only Delete
  keeps its confirmation dialog.
- **Instant list filtering:** the Dashboard search box and every list
  page (Client PCs, Accounts, Sessions, Audit Trail, Inventory,
  Website rules) filter **as you type** — each keystroke re-runs the
  query immediately, no Enter or Search button anywhere. Empty boxes
  show a placeholder hint ("Search PCs...") that returns on blur.
- **Offline resilience (spec items 9–13, 15, 17):** the Client keeps
  its own event log in `lab_client.db` (UUID event id, severity,
  category, `PENDING → SYNCED`) and flushes it in idempotent batches
  whenever the link returns — the server keys `client_logs` on
  `event_id`, ignores duplicates and ACKs exactly the ids it stored;
  rows are **never deleted before that ACK**, so logs survive
  disconnects and restarts. With the Server down the login card shows
  `○ Server Offline — Local Mode`, roster-synced Active accounts can
  still sign in from the cached hashes, and everything else queues
  locally. On reconnect the indicator flips back to
  `● Server Connected`, the queue drains, the session resumes, and
  auto-reconnect cannot create duplicate connections or duplicate log
  entries (the receive loop is identity-guarded: a stale thread can
  never drop the new connection). The session bar always reports the
  real link type: `Connection: Ethernet | Wi-Fi | LAN`.

## Running it directly (no build needed)

Python 3.13 (Tkinter included on Windows), then:

```
pip install -r requirements.txt   # pulls in customtkinter for the UI
python main.py            # mode launcher (development)
python main.py --server   # force Server + Admin Console
python main.py --client   # force Client kiosk
```

1. **Server + Admin Console** — starts the LAN server (a self-signed
   `server.crt` / `server.key` is generated automatically) and the login
   window. Logging in opens the dashboard **directly** (sidebar
   navigation, no popups); log in as `admin` to see the *Client PCs*
   page with the live monitor and remote controls. The landing page is
   the **Dashboard** — the PC management hub (stats, filters, card
   grid, bulk toolbar, right-side details).
2. **Client (Lab PC) Mode** — fullscreen kiosk. On first run it asks for
   the **Server PC's IP address** and port (saved once in
   `lab_config.json` next to the app and reused automatically). Cancelling
   just returns to the locked screen — it never loops or hides the kiosk.
   The **⚙ Server Settings** button on the lock screen is the kiosk's
   configuration entry point, and an `admin` or `maintenance` account can
   reach the same dialog from *Maintenance* in the Client Dashboard
   header while signed in — both re-point the client at a
   different IP/port at any time, online or offline, and only ever
   write `lab_config.json`. Signed in as `admin` or `maintenance` it
   also shows the *Maintenance* section with **[Uninstall Client]**.

A `lab_system.db` SQLite file is created next to the script on first run,
seeded with the demo accounts above.

### Typical lab setup

1. On the Server PC: `python main.py --server` → log in as `admin` → note
   the IP shown in the header (`Local Network Server ● :8443`).
2. On each client PC: copy the folder → `python main.py --client` →
   enter the Server IP once → the PC locks at the login screen and
   appears in the admin dashboard.

## Building the Windows executables

Run **`build_exe.bat`** on a Windows PC with Python installed. It
installs the requirements and produces:

| Executable       | Where it runs                                              |
|------------------|------------------------------------------------------------|
| `dist\server.exe` | Admin/Server PC — starts **directly** in Server/Admin mode |
| `dist\client.exe` | Every client/lab PC — starts **directly** in Client mode   |

No Server/Client choice is ever shown: the frozen mode is detected from
the executable name (`server` / `client`), and `--server` / `--client`
flags work the same when running from source.

Client machines need neither Python nor the database — only the `.exe`.
Closing or minimizing the admin dashboard never logs the admin out; only
the **Logout** button does (spec item 1: the header carries no Minimize
button — the title-bar X only iconifies the window). Closing the Client
Dashboard's X likewise
only closes that window - the session keeps running and the server
records a `client_closed` event (never a logout). The locked kiosk itself
cannot be closed, so it never returns to a configuration screen.

On start the Client also (re)registers itself in the current user's
Windows Startup list (`HKCU\...\Run`, entry name **Computer
Laboratory Client** - per-user, no admin rights, idempotent), so lab PCs
come back to the kiosk after a reboot. Run `client.exe
--uninstall-startup` (or `python main.py --uninstall-startup`) to remove
that entry and the two watchdog tasks again (it exits without opening
the kiosk). The same cleanup is available from inside a signed-in kiosk
via **Server Settings → Maintenance → [Uninstall Client]** for `admin`
and `maintenance` accounts: it exports the local log to a CSV first,
then removes the Startup entry and the watchdog tasks — application
files and the database are never deleted.

To take the Client off a lab PC completely, run **`uninstall.exe`**
(next to the Client, or copy it there from `dist\`): it removes the two
watchdog tasks and the Startup entry, stops any running `client.exe`,
exports the local log to a CSV, deletes the Client files
(`client.exe`, `lab_client.db*`, `lab_config.json`) and then deletes
itself. It asks for admin rights because an elevated Client can only
be stopped from an elevated process; if a step fails it says so
instead of pretending — fix the reported line (usually: run it as
Administrator) and run it again. Run it as the user who used the
Client; the Startup entry is per-user.

`server.exe` and `client.exe` bundle the assets they need: the single
official logo `assets/images/logo.png` (window and login branding),
`app_icon.ico` (the `.exe` file icon and the title-bar/taskbar icon)
and the eight `pc_icons/*.png` status icons.

## Tests

```
python _test_integration.py   # 606 checks: TLS framing, auth & role claim,
                              # first-login password change (flagged
                              # accounts, sessions blocked until changed,
                              # hashed storage, audit rows),
                              # close/disconnect event taxonomy
                              # (client_closed / network_disconnect /
                              # client_reconnect / client_crash),
                              # audit taxonomy + Type filter,
                              # account change live-sync,
                              # Website Access redesign (3 mode cards +
                              # unified rules table, URL normalization,
                              # suffix matching, All/Selected/Individual/
                              # Group scope with multi-PC confirmation,
                              # version push that only turns
                              # SYNCED on the client's ack, detection as
                              # the only enforcement: a leftover hosts
                              # block is cleared and never written, the
                              # DNS proxy + adapter repoint are gone, an
                              # unreadable socket table and an empty
                              # allow list are both reported FAILED),
                              # Sessions redesign (read-only monitor,
                              # Session Details panel, Force Logout +
                              # View Activity, connection_type capture),
                              # Accounts redesign (summary cards,
                              # search -> select -> Account Details
                              # window with 4 tabs, add/edit/delete
                              # semantics, no password column),
                              # inventory (statuses, actions, stats strip,
                              # low-stock) and instant filtering on every
                              # list page, offline auth roster +
                              # local-log sync (event_id de-dup, ACKed
                              # flush, rows kept until the ack),
                              # UDP LAN discovery (broadcast 8444,
                              # fallback to the saved server_ip),
                              # maintenance role (legacy-schema migration,
                              # server-authoritative login, dashboard
                              # gating, never seeded),
                              # sessions/healing, status engine, heartbeats,
                              # bulk commands, audits, admin dashboard
                              # (6-stat strip, sidebar + More group,
                              # filter chips wrapped left of the panel,
                              # right-side details panel),
                              # Staff & Admin Account Details (the very
                              # same shared 4-tab window as Accounts, role
                              # shown, password never exposed),
                              # default-password rules (blank falls back to
                              # password123 on add, blank keeps the hash on
                              # edit, never stored or echoed in clear text),
                              # observation audit rows only completed by the
                              # client's real acknowledgement,
                               # Remote-control whitelist + session
                               # start/stop/drop audit rows, plus
                               # one-online-PC gating of the Remote
                               # button,
                               # Website Access DNS repair (loopback is
                               # never a "previous" value, a failed restore
                               # is reported as FAILED, ALLOW ALL repairs
                               # with nothing enforcing anymore, a live local
                               # resolver is never wiped, and the adapter
                               # DNS query is real PowerShell, not a parse
                               # error),
                               # Website Access detection (ALLOW ALL
                               # scans nothing, a shared CDN address with
                               # mixed verdicts is UNRESOLVED rather than
                               # guessed, own PID / LAN / unknown socket
                               # states never become events) and the
                               # five-condition guarded browser close
                               # (one re-validated PID, cooldown, create-
                               # time match, missing rights reported as
                               # FAILED instead of SYNCED),
                               # watchdog hardening (missing, stale or
                               # mid-session-deleted tasks repaired with
                               # THIS app's command, healthy tasks never
                               # rewritten, refused registration recorded
                               # as a throttled WARN) and the uninstaller's
                               # real behaviour (both /Delete commands,
                               # missing task = done, verify-after stop,
                               # FAILED never self-deletes, a failed export
                               # keeps the audit database),
                               # Screen share (Server screen -> every
                               # online lab PC: ADMINISTRATOR-only at
                               # the server, a dead PC dropped after one
                               # failed send without stalling the rest,
                               # real JPEG frames on the wire, audited +
                               # ack-resolved START and STOP, a second
                               # start/stop is a no-op, a refused role
                               # and an all-offline roster refuse
                               # cleanly),
                               # and the P1.5 safety sign-off - 11
                               # guardrail checks that fail the gate if
                               # any non-negotiable erodes: no hardware/
                               # boot/driver/service surgery, hosts edits
                               # only inside our markers, adapter DNS
                               # written only by restore/repair, kills
                               # only from close_browser and only by PID,
                               # a one-shot power token, "enforced" only
                               # after a successful probe, and watchdog-
                               # only persistence,
                               # and P4 - Observe and Remote Control are
                               # ADMINISTRATOR-only AT THE SERVER, not
                               # merely hidden in the UI: a STAFF or
                               # MAINTENANCE role is refused before any
                               # stream or session exists, the refusal is
                               # audited as a security event, a refused
                               # input batch drives nothing, and the
                               # controls are not even offered to anyone
                               # who is not an administrator, and the
                               # Restart / Shutdown rule end to end: both
                               # ask first and send nothing when the
                               # operator declines, every other bulk
                               # action stays a toast, and an unanswered
                               # disconnect is logged rather than turned
                               # into a power action on its own,
                                # Force Unlock = ALL PCs always (the
                                # selection never narrows it; an offline
                                # PC's unlock is remembered and applied
                                # when it reconnects), and roster refresh
                                # on every password/account change
                                # (server pushes fire-and-forget, the
                                # client syncs in the background, a
                                # 6-hour periodic push as backstop)
python _test_client_gui.py    # 290 checks: kiosk state machine, admin lock,
                              # force login, pause, logout, command
                              # de-duplication, login card (password
                              # toggle, server line, PC footer),
                              # first-login forced Change Password card,
                              # dashboard X closes without logging out,
                              # Ctrl+Shift+Alt+M emergency force-unlock,
                              # client-admin display, cmd_web_filter ->
                              # detector snapshot (ack + failure report,
                              # legacy domain list only clears),
                              # offline login + Local Mode indicator
                              # (● Server Connected /
                              # ○ Server Offline — Local Mode), local
                              # event-log queue (pending rows survive,
                              # reconnect flush), startup registration /
                              # --uninstall-startup,
                              # [udp] discovery finds nothing in tests and
                              # the kiosk falls back to the saved server_ip,
                              # [p1-4] watchdog task registration/removal +
                              # --watchdog conflict handling,
                              # [p1-5] hidden emergency stop chord (works
                               # from ANY client state: audit first, release
                               # observation/share/remote, log out, remove
                               # the watchdog + startup tasks, exit once),
                              # [p1-6] Uninstall Client (role gate, CSV
                              # export, startup + watchdog removal),
                              # [observe] clamped stream parameters, exactly
                              # one OBSERVING banner, start/stop audit rows
                              # and a restart that retires the old stream,
                              # [p1-8] every remote command recorded on the
                              # client with its id and link state (once per
                              # id),
                              # [p1-0] startup repairs the stuck DNS BEFORE
                              # the saved policy is re-applied,
                              # [webscan] detection events reach the durable
                              # queue and the server audit in that order,
                              # UNRESOLVED and refused closes are reported
                              # instead of swallowed, and a real detector
                              # event never terminates a non-browser
                              # [share] the fullscreen overlay opens on
                              # START, paints a frame, drops an oversized
                              # payload without decoding, reopens for a
                              # mid-share rejoin, closes on STOP and
                              # times out once frames stop for 5 s;
                               # [share-hotkey] START arms the overlay's
                               # hotkey blocker only when the app's own
                               # hotkeys were idle, and release only drops
                               # what the overlay armed
python _probe_layout.py       # 50 checks: responsive layout probe -
                              # measures the Dashboard + Client PCs pages,
                              # the right-side details panel and the sidebar
                              # at 1080x700 (minimum), 1280x780 and 1440x900
                              # - fails loudly if anything would be clipped
                              # or run under the details panel, and proves
                              # the P4 UI half: a STAFF dashboard is offered
                              # no Observe and no Remote control while the
                              # ADMINISTRATOR still has both
```

Run them one after the other (the two server-backed suites target test
port `18443`, UDP discovery `18444`). Each
suite provisions what it needs (`init_db()` / a temporary
`lab_config.json`) and leaves no server running — the source tree needs
no manual setup first. `gate.bat` runs the whole regression gate in one
shot — both suites on fresh DBs, then `_full_sweep.py` (layout) and
`_contrast_all.py` (pixel contrast) — and stops at the first failing
stage.

## Project structure

```
lab_system/
├── main.py               Mode detection (server.exe/client.exe, --server/
│                         --client) + Server mode launcher + login window
├── server.py             LabServer: TLS TCP server, client registry,
│                         desired lock/pause state, command distribution,
│                         session & audit recording, force logout,
│                         server-authoritative status engine
│                         (derive_status) + bulk commands with per-PC
│                         results and BULK_* audits, web policy push /
│                         per-PC version acks (SYNCED only on ack),
│                         STU branches (auth_roster, log_sync, ...),
│                         screen-share capture→fan-out worker (a dead
│                         PC dropped after one failed send, STOP sent
│                         after the last frame)
├── client.py             ClientApp: fullscreen kiosk login, heartbeats,
│                         auto-reconnect/re-register, remote-command
│                         execution (ACKed, de-duplicated by command id),
│                         screen capture, share-screen fullscreen
│                         overlay (closed on STOP / 5 s frame timeout /
│                         disconnect), keyboard-hook
│                         bypass protection, session bar, offline
│                         login + local event log (queue/flush)
├── protocol.py           Message types (commands incl. screen share),
│                         heartbeat ACK (PONG), TLS helpers,
│                         frame encode/decode
├── client_api.py         Network-first data bridge used by the student
│                         dashboard (local-SQLite fallback preserved)
├── dns_filter.py         Website Access policy helpers + adapter-DNS
│                         repair: the pure decide()/domain_matches()
│                         verdict engine (the one implementation
│                         web_access reports against), plus
│                         repair_adapter_dns() - heals a 127.0.0.1 the
│                         adapter was left pointing at when a run was
│                         killed before it could restore (the DNS
│                         proxy that caused that is gone, P1.4)
├── web_access.py         Website Access detection: connection -> domain
│                         -> responsible browser -> verdict, plus the
│                         five-condition guarded close (one re-validated
│                         PID - never an image name, never a process
│                         tree)
├── local_store.py        Client-side SQLite (lab_client.db): local event
│                         log with UUID event ids (PENDING -> SYNCED)
│                         + cached auth roster for offline login
├── database.py           Schema (incl. sessions/activity/commands tables),
│                         hostname + admin_state migrations, PBKDF2
│                         hashing, WAL mode, legacy migration
├── crud_frame.py         Reusable "table + form" component used by every
│                         admin management page (with change hooks and
│                         toast routing for normal events) + the shared
│                         Account Details window behind AccountsFrame and
│                         StaffAccountsFrame (one implementation, two
│                         pages)
├── admin_dashboard.py    Administrator/Staff window: scrollable sidebar
│                         (7 primary entries + collapsed More group) and
│                         pages (Dashboard with the 6-stat strip,
│                         Search PCs toolbar, status filter chips with
│                         live counts, PC card grid, compact bulk
│                         toolbar & right-side PC details panel;
│                         Client PCs with remote controls, Share Screen
│                         toggle, screen viewer
│                         & right-side details; simplified Computer
│                         management, Accounts (cards + Account Details
│                         window), Staff & Admin (same CRUD and the same
│                         Account Details window), Sessions (read-only
│                         monitor with Session Details panel), filterable
│                         Audit Trail, Inventory, Borrowing,
│                         Maintenance, Website Access (mode cards +
│                         unified rules table), Announcements,
│                         Messages, Settings) + bottom-right toast
│                         notifications
├── student_dashboard.py  Student window (announcements, messages, borrow
│                         requests)
├── utils.py              Shared styling & CSV export helpers (toast-aware),
│                         design tokens (typography, spacing, borders,
│                         card/tile sizes), status constants/icons
│                         (status_key, get_status_icon,
│                         render_status_tile) & set_app_icon
├── theme.py              Single source of truth for the dark theme: every
│                         colour, font, radius, spacing and status tone,
│                         plus the DPI/font scaling pins applied before
│                         the first CustomTkinter window exists
├── components.py         Reusable CustomTkinter widgets (Card, PaddedFrame,
│                         CardFrame, TabHost, Toolbar, SearchBox, StatTile,
│                         StatusPill, Toast, ...)
├── pc_icons/             8 status PNGs (available, in_use, locked,
│                         paused, offline, online, verifying, unknown);
│                         bundled into both exes with --add-data
├── _make_pc_icons.py     Dev utility that regenerates pc_icons/*.png
├── assets/images/        logo.png — the single official app logo, shared
│                         by the Server console and the Client kiosk
│                         (login card, dashboard header, sidebar, window
│                         title and tray icon)
├── _make_logo_assets.py  Dev utility that derives app_icon.ico from
│                         assets/images/logo.png
├── app_icon.ico          Derived app icon: the .exe file icon and every
│                         window's title bar / taskbar icon (bundled into
│                         each exe with --add-data)
├── requirements.txt      psutil, pillow, cryptography, pyinstaller,
│                         pystray, customtkinter
├── build_exe.bat         One-click Windows build of both executables
│                         (--icon for the file icon, --add-data for
│                         assets/ (the logo), app_icon.ico and the
│                         pc_icons/ status icons, --collect-all
│                         customtkinter for the dark UI; client.exe also
│                         pulls in the pystray Windows backend)
├── _test_integration.py  606-check end-to-end suite (server + protocol +
│                         admin dashboard)
├── _test_client_gui.py   290-check kiosk state-machine suite
├── _probe_layout.py      Responsive layout probe (no clipped rows or
│                         panel overruns at min/default/large sizes)
├── gate.bat              Regression gate: both suites (fresh DBs) +
│                         layout sweep + contrast sweep, first failure
│                         stops the run
├── _full_sweep.py        Whole-UI layout sweep (every admin page at 3
│                         sizes + hard resizes; clipped text or page
│                         overflow fails)
├── _contrast_all.py      Pixel contrast sweep (any heading under 3.0
│                         luminance ratio fails; failing crops land in
│                         _bad/)
├── _ps_warm.py/_tail.py  Gate helpers (PowerShell warm-up, PASS/FAIL
│                         log summary)
├── server.crt/server.key Auto-generated self-signed TLS certificate
└── lab_system.db         Created automatically on first run (server only)
```

## Security notes

- Passwords: PBKDF2-HMAC-SHA256, per-user 16-byte salt, 100k iterations.
- First-login password: fresh client accounts are created from the
  Accounts page with a salted-hashed password (leaving the field blank
  falls back to the factory default `password123`) and the
  `must_change_password` flag set, and must change it on first Client
  login — enforced by the forced
  client card AND the server (`session_start` refused while flagged);
  the flag is cleared only when the new hash is stored. The shared
  default `password123` is refused as a new password by both the kiosk
  card and the server. On the Staff & Admin page a password is required
  when adding an account, so a blank field can never produce an account
  nobody can sign in with.
- Offline authentication (spec item 12): with the Server unreachable a
  Client may sign in a **roster-synced** account from its local cache.
  Only salted PBKDF2 hashes — never plaintext — of **Active** accounts
  are cached (fetched over the STU channel), and verification is
  fail-closed: roster-only, Active, not flagged
  `must_change_password` (those accounts must sign in online), within
  `max_offline_days` (seeded `7`) of the last roster sync, and the
  hash must match. The cache is replaced wholesale on every roster
  sync, so disabling, deleting or changing an account on the Server
  revokes its offline access as soon as the client syncs again. A
  sync is pushed right after any password/account change (the server
  fires and forgets; the client refreshes in the background) with a
  6-hour periodic push as backstop, so a changed password narrows the
  offline window to minutes rather than days.
- **Limitations of offline authentication** (accepted trade-offs,
  documented per spec):
  - credentials revoked or changed *after* the last roster sync stay
    usable offline until the client reconnects and refreshes the
    cache — the exposure window is bounded by `max_offline_days`;
  - password changes and first-login password resets are online-only;
  - accounts flagged `must_change_password` cannot sign in offline;
  - queued local-log rows are only acknowledged (and only then
    deleted) once the Server is reachable, so offline audit entries
    appear in the central trail after the next reconnect.
- Website Access: enforced on the Client by **connection detection**
  (`web_access.py`) — the Client resolves the watched names itself,
  attributes a live socket to a domain and then to the responsible
  browser (Chrome / Edge / Firefox) and closes only that browser. No
  hosts file is written and the adapter's DNS is never repointed (the
  local DNS proxy that used to do that is gone — a run killed before
  its restore left the PC with no name resolution at all). A Client
  that cannot read who owns a socket, or whose ALLOW ONLY list is
  empty, reports `FAILED` with its reason instead of claiming
  `SYNCED`.
- Server ↔ client traffic: TLS 1.2+ (self-signed certificate, LAN only).
- All SQL uses parameterized queries; table/column identifiers in the
  generic CRUD component are developer-supplied constants, never user input.
- The activity audit trail is read-only in the UI; the **Audit Trail**
  page groups every recorded action into a taxonomy (Login / Logout,
  Connection, Security, Website Access, Power, Monitoring, Admin
  Commands, Other) with
  human-readable labels and a **Type** filter alongside Search / Date /
  result, and can be filtered/searched by time, user, action, target PC
  and result - it shows both client events and server commands (with
  results). Every shutdown/restart is explicitly audited.
- The keyboard hook is best-effort user-mode protection: it blocks common
  bypass hotkeys while the kiosk is locked. Ctrl+Alt+Del itself cannot be
  suppressed by any user-mode software (by Windows design) — so while a
  kiosk screen is up (login card, admin lock, pause overlay, screen
  share) the client writes the documented HKCU policy values
  (`DisableTaskMgr`, `DisableLockWorkstation`, `DisableChangePassword`,
  `HideFastUserSwitching`, `NoLogoff`) and every option that screen
  offers is policy-disabled. The moment the desktop is bare again —
  unlock, panic stop, any uninstall path, Server mode — the same five
  values are deleted and Windows behaves completely normally;
  `dist\uninstall.exe` clears them through the shared
  `startup_ids.CAD_POLICY_VALUES` list, because the registry itself
  survives file deletion.

## Notes

- Back up `lab_system.db` — that file **is** your school's data.
- All data is local (SQLite) — no internet connection is required.
- Client PCs store `lab_config.json` (server address) plus
  `lab_client.db` — a local event log and a cached login roster (salted
  hashes of rostered Active accounts, never plaintext). Deleting
  `lab_client.db` purges both (log rows not yet acknowledged by the
  Server would be lost with it).
