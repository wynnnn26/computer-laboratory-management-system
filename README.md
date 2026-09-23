# Computer Laboratory Management System — LAN Edition

A desktop application (Python + Tkinter + SQLite + TCP/TLS sockets) for
managing a school computer laboratory / internet café over a **LAN only**
(no cloud, no REST API), inspired by PanCafe Pro:

- **Server / Admin PC** hosts the central SQLite database and a TLS TCP
  server; the admin dashboard shows every connected client PC live
  (PC Name, IP, Hostname, Online/Offline, Current User, CPU %, RAM %,
  Last Seen) and can remotely control it.
- **Client PCs** boot into a **mandatory fullscreen login screen** that
  cannot be bypassed — they stay locked until an account is authenticated
  by the Server. Clients auto-register, heartbeat every 5 s and
  **auto-reconnect + re-register** whenever the server restarts.
- **Roles:** `admin`, `staff`, `student` (customer).
- **Remote controls:** lock, unlock (Admin Force Login), logout, pause,
  resume, restart, shutdown, message popup, one-shot screenshot, and live
  screen observation — every command is acknowledged and its
  success/failure is shown as a non-blocking toast.
- **Pause never expires on its own** — it stays active until the admin
  explicitly presses Resume (and survives client/server restarts).
- **Admin Lock cannot be bypassed by typing credentials**: only an
  explicit Admin Unlock (force login) releases the PC.
- Every user session, client event and admin/staff action is recorded in
  the database (sessions table + activity audit log + command audit
  table) and is searchable/filterable in the **Activity Log**.
- All existing standalone features are preserved (minus *Lab
  Attendance*, which was intentionally removed): computers, inventory,
  equipment borrowing, maintenance, announcements, messaging, OJT
  interns/tasks, and CSV export on every tab.
- **Notifications:** small auto-dismissing toasts for normal events;
  dialogs are reserved for critical confirmations only
  (Restart / Shutdown / Delete).

## Default login credentials (created automatically on first run)

| Role    | Username       | Password     |
|---------|----------------|--------------|
| Admin   | `admin`        | `admin123`   |
| Staff   | `staff`        | `staff123`   |
| Student | `2023-00001`   | `student123` |

**Change these after your first login** — the *Staff & Admin* page
(admin only) and *Student Accounts* page manage all accounts. Passwords
are salted and hashed with PBKDF2-HMAC-SHA256 (100k iterations) and are
never stored or transmitted in plain text (the LAN transport itself is
TLS). Deleting, disabling or updating an account automatically logs that
user out of any active client session.

## Architecture

```
        ┌──────────────────────────── LAN (Ethernet/Wi-Fi) ───────────────────────────┐
        │                              TCP port 8443 (TLS)                            │
        │                                                                             │
┌───────┴────────┐   heartbeats / auth / session / stu queries   ┌────────┐ ┌────────┐ │
│  SERVER PC     │◄─────────────────────────────────────────────►│ PC-01  │ │ PC-02  │ │
│  server.py     │◄──── commands: lock/unlock/logout/pause ──────►│ client │ │ client │ │
│  LabServer     │      resume/restart/shutdown/screenshot/      │  .py   │ │  .py   │ │
│  lab_system.db │      live screen observation ────────────────►│  kiosk │ │  kiosk │ │
│  admin dashboard│                                              └────────┘ └────────┘ │
└─────────────────┘                                                                   │
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
- **Heartbeats (5 s):** status, CPU %, RAM %, IP, hostname, logged-in
  user — stored on the `computers` row; the server **acknowledges every
  heartbeat** (PONG + authoritative lock/pause state), so a dead or
  restarted server is detected within ~20 s and the client reconnects
  and re-registers by itself (no more stuck "Verifying…").
- **Shutdown safety:** OS restart/shutdown runs in exactly one place in
  the client and only when a one-shot token was armed by an explicit
  authenticated admin command. Every shutdown/restart is audited with
  admin, PC, timestamp and result.

## Running it directly (no build needed)

Python 3.9+ (Tkinter included on Windows), then:

```
pip install -r requirements.txt
python main.py            # mode launcher (development)
python main.py --server   # force Server + Admin Console
python main.py --client   # force Client kiosk
```

1. **Server + Admin Console** — starts the LAN server (a self-signed
   `server.crt` / `server.key` is generated automatically) and the login
   window. Logging in opens the dashboard **directly** (sidebar
   navigation, no popups); log in as `admin` to see the *Client PCs*
   page with the live monitor and remote controls.
2. **Client (Lab PC) Mode** — fullscreen kiosk. On first run it asks for
   the **Server PC's IP address** and port (saved once in
   `lab_config.json` next to the app and reused automatically). Cancelling
   just returns to the locked screen — it never loops or hides the kiosk.

A `lab_system.db` SQLite file is created next to the script on first run,
seeded with the demo accounts above.

### Typical lab setup

1. On the Server PC: `python main.py --server` → log in as `admin` → note
   the IP shown in the header (`LAN Server ● :8443`).
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
the **Logout** button does. Closing the client window never returns it to
a configuration screen (the kiosk cannot be closed while locked).

## Project structure

```
lab_system/
├── main.py               Mode detection (server.exe/client.exe, --server/
│                         --client) + Server mode launcher + login window
├── server.py             LabServer: TLS TCP server, client registry,
│                         desired lock/pause state, command distribution,
│                         session & audit recording, force logout
├── client.py             ClientApp: fullscreen kiosk login, heartbeats,
│                         auto-reconnect/re-register, remote-command
│                         execution (ACKed), screen capture, keyboard-hook
│                         bypass protection, session bar
├── protocol.py           Message types, heartbeat ACK (PONG), TLS helpers,
│                         frame encode/decode
├── client_api.py         Network-first data bridge used by the student
│                         dashboard (local-SQLite fallback preserved)
├── database.py           Schema (incl. sessions/activity/commands tables),
│                         hostname + admin_state migrations, PBKDF2
│                         hashing, WAL mode, legacy migration
├── crud_frame.py         Reusable "table + form" component used by every
│                         admin management page (with change hooks)
├── admin_dashboard.py    Administrator/Staff window: sidebar navigation +
│                         pages (Overview, Client PCs with remote controls
│                         & screen viewer, simplified Computer management,
│                         Accounts, Sessions, filterable Activity Log,
│                         Inventory, Borrowing, Maintenance, Announcements,
│                         Messages, OJT) + toast notifications
├── student_dashboard.py  Student window (announcements, messages, borrow
│                         requests)
├── utils.py              Shared styling & CSV export helpers
├── requirements.txt      psutil, pillow, cryptography, pyinstaller
├── build_exe.bat         One-click Windows build of both executables
├── server.crt/server.key Auto-generated self-signed TLS certificate
└── lab_system.db         Created automatically on first run (server only)
```

## Security notes

- Passwords: PBKDF2-HMAC-SHA256, per-user 16-byte salt, 100k iterations.
- Server ↔ client traffic: TLS 1.2+ (self-signed certificate, LAN only).
- All SQL uses parameterized queries; table/column identifiers in the
  generic CRUD component are developer-supplied constants, never user input.
- The activity audit trail is read-only in the UI; the **Activity Log**
  page can be filtered/searched by time, user, action, target PC and
  result, and shows both client events and server commands (with
  results). Every shutdown/restart is explicitly audited.
- The keyboard hook is best-effort user-mode protection: it blocks common
  bypass hotkeys while the kiosk is locked. Ctrl+Alt+Del cannot be blocked
  by any user-mode software (by Windows design).

## Notes

- Back up `lab_system.db` — that file **is** your school's data.
- All data is local (SQLite) — no internet connection is required.
- Client PCs store only `lab_config.json` (server address) — no data.
