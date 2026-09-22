# Computer Laboratory Management System — LAN Edition

A desktop application (Python + Tkinter + SQLite + TCP/TLS sockets) for
managing a school computer laboratory over a **LAN only** (no cloud, no REST
API), inspired by PanCafe Pro:

- **Server / Admin PC** hosts the central SQLite database and a TLS TCP
  server; the admin dashboard shows every connected client PC live
  (status, logged-in user, IP, CPU %, RAM %) and can remotely control it.
- **Client PCs** boot into a **mandatory fullscreen login screen** that
  cannot be bypassed — they stay locked until an account is authenticated
  by the Server. Clients auto-register and report heartbeats via `psutil`.
- **Roles:** `admin`, `staff`, `student` (customer).
- **Remote controls:** lock, unlock, logout, pause, resume, restart,
  shutdown, message popup, one-shot screenshot, and live screen observation.
- Every user session and every admin/staff action is recorded in the
  database (sessions table + activity audit log + command audit table).
- All existing standalone features are preserved: attendance, computers,
  inventory, equipment borrowing, maintenance, announcements, messaging,
  OJT interns/tasks, and CSV export on every tab.

## Default login credentials (created automatically on first run)

| Role    | Username       | Password     |
|---------|----------------|--------------|
| Admin   | `admin`        | `admin123`   |
| Staff   | `staff`        | `staff123`   |
| Student | `2023-00001`   | `student123` |

**Change these after your first login** — the *Staff & Admin Accounts* tab
(admin only) and *Student Accounts* tab manage all accounts. Passwords are
salted and hashed with PBKDF2-HMAC-SHA256 (100k iterations) and are never
stored or transmitted in plain text (the LAN transport itself is TLS).

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
  student-facing data (announcements, messages, borrow requests,
  attendance) is forwarded through purpose-built parameterized queries
  (`client_api.py` → `STU_REQUEST` on the server).
- **Mandatory client login:** the kiosk window is fullscreen,
  override-redirect, always-on-top, and a low-level Windows keyboard hook
  swallows Alt+Tab, Win-key, Ctrl+Esc, Alt+F4, Alt+Space while locked.
  Invalid credentials keep the PC locked; logout or a remote admin lock
  returns it to the login screen; a remote unlock only resumes a session
  that was already authenticated (otherwise the login screen appears).
- **Sessions:** a successful client login opens a session + lab attendance
  record automatically; logout/disconnect closes them with a duration.
- **Heartbeats (5 s):** status, CPU %, RAM %, IP, MAC, logged-in user —
  stored on the `computers` row; clients silent for 15 s are flagged offline.

## Running it directly (no build needed)

Python 3.9+ (Tkinter included on Windows), then:

```
pip install -r requirements.txt
python main.py
```

The launcher offers two modes:

1. **Server + Admin Console** — starts the LAN server (a self-signed
   `server.crt` / `server.key` is generated automatically) and the login
   window. Log in as `admin` to see the *Client PCs* tab with the live
   monitor and remote controls.
2. **Client (Lab PC) Mode** — fullscreen kiosk. On first run it asks for
   the **Server PC's IP address** and port (saved in `lab_config.json`).

A `lab_system.db` SQLite file is created next to the script on first run,
seeded with the demo accounts above.

### Typical lab setup

1. On the Server PC: `python main.py` → **Server + Admin Console** → note
   the IP shown in the header (`LAN Server ● :8443`).
2. On each client PC: copy the folder → `python main.py` → **Client Mode**
   → enter the Server IP → the PC locks at the login screen and appears in
   the admin dashboard.

## Building the Windows .exe

Run **`build_exe.bat`** on a Windows PC with Python installed. It installs
the requirements and produces:

| Executable         | Where it runs                    |
|--------------------|----------------------------------|
| `dist\LabServer.exe` | Admin/Server PC (launcher + server + dashboards) |
| `dist\LabClient.exe` | Every client/lab PC (fullscreen kiosk)          |

Client machines need neither Python nor the database — only the `.exe`.

## Project structure

```
lab_system/
├── main.py               Launcher: Server mode / Client mode + login window
├── server.py             LabServer: TLS TCP server, client registry,
│                         command distribution, session & audit recording
├── client.py             ClientApp: fullscreen kiosk login, heartbeats,
│                         remote-command execution, screen capture,
│                         keyboard-hook bypass protection, session bar
├── protocol.py           Message types, TLS helpers, frame encode/decode
├── client_api.py         Network-first data bridge used by the student
│                         dashboard (local-SQLite fallback preserved)
├── database.py           Schema (incl. sessions/activity/commands tables),
│                         PBKDF2 hashing, WAL mode, legacy migration
├── crud_frame.py         Reusable "table + form" component used by every
│                         admin management tab
├── admin_dashboard.py    Administrator/Staff window: Overview, Client PCs
│                         (live monitor + remote controls + screen viewer),
│                         all original tabs, Sessions, Activity Log
├── student_dashboard.py  Student window (attendance, announcements,
│                         messages, borrow requests)
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
- Activity audit trail (`admin_activity_log`) is read-only in the UI
  (delete disabled); every remote command is also logged with its result
  (`client_commands`).
- The keyboard hook is best-effort user-mode protection: it blocks common
  bypass hotkeys while the kiosk is locked. Ctrl+Alt+Del cannot be blocked
  by any user-mode software (by Windows design).

## Notes

- Back up `lab_system.db` — that file **is** your school's data.
- All data is local (SQLite) — no internet connection is required.
- Client PCs store only `lab_config.json` (server address) — no data.
