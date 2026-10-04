"""Full integration smoke test (v2.1 fix plan): DB, protocol, TLS server,
heartbeats + ACK, desired lock/pause state, force logout, sessions,
audit trail, sidebar dashboard, toasts, attendance removal, UI."""
import queue
import socket
import sys
import threading
import time
import os
import json

import customtkinter as _ctk

from utils import now_date as _today, now_datetime

# status glyphs (● ○ • …) must never crash the console printer
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

FAIL = []

def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {extra}" if extra else ""), flush=True)
    if not cond:
        FAIL.append(name)

# ---------------------------------------------------------------- database
import database
first = database.init_db()
check("init_db creates database", os.path.exists(database.get_db_path()), f"first={first}")
database.init_db()
conn = database.get_connection()
roles = [r["role"] for r in conn.execute("SELECT DISTINCT role FROM users").fetchall()]
check("roles seeded (admin, staff, student)", set(roles) == {"admin", "staff", "student"}, str(roles))
conn.execute("DELETE FROM users WHERE student_id='mig_test'")
conn.execute(
    "INSERT INTO users (student_id, password, full_name, role, status) VALUES (?,?,?,?,?)",
    ("mig_test", database.hash_password("x"), "Migration Test", "staff", "Active"))
conn.commit()
check("staff role insert works", True)
row = conn.execute("SELECT password FROM users WHERE student_id='admin'").fetchone()
check("password verify correct", database.verify_password("admin123", row["password"]))
check("password verify rejects wrong", not database.verify_password("nope", row["password"]))

# --- maintenance role: legacy migration + no seeded account ---------------
# Simulate a database built before the maintenance role existed and run
# the migration against it: the CHECK must widen AND every row (incl.
# first-login flags) must survive.  Nothing ever seeds a maintenance
# account - no hardcoded dev password exists anywhere in the code; an
# admin creates the account from the Staff & Admin page instead.
conn.execute("UPDATE users SET must_change_password=1 WHERE student_id='mig_test'")
conn.commit()
conn.executescript("""
    ALTER TABLE users RENAME TO users_bak;
    CREATE TABLE users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        full_name TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('student','admin','staff')),
        course TEXT, year_level TEXT, email TEXT, contact TEXT,
        status TEXT DEFAULT 'Active',
        must_change_password INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    INSERT INTO users (id, student_id, password, full_name, role, course,
                       year_level, email, contact, status,
                       must_change_password, created_at)
        SELECT id, student_id, password, full_name, role, course,
               year_level, email, contact, status,
               must_change_password, created_at FROM users_bak;
    DROP TABLE users_bak;
""")
# spec 3: legacy databases still carrying the removed OJT tables get
# them dropped by the same migration pass.
conn.executescript("""
    CREATE TABLE IF NOT EXISTS ojt_interns (id INTEGER PRIMARY KEY,
                                            name TEXT);
    CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY,
                                      task_title TEXT);
""")
database._migrate_legacy_schema(conn.cursor())
conn.commit()
_ojt_left = conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' "
    "AND name IN ('ojt_interns','tasks')").fetchall()
check("legacy OJT tables dropped by migration (spec 3)",
      not _ojt_left, str([r[0] for r in _ojt_left]))
conn.execute(
    "INSERT INTO users (student_id, password, full_name, role, status) "
    "VALUES (?,?,?,?,?)",
    ("maint_test", database.hash_password("x"),
     "Maintenance Tech", "maintenance", "Active"))
conn.commit()
check("maintenance role survives the legacy schema migration",
      conn.execute("SELECT COUNT(*) FROM users WHERE role='maintenance'")
      .fetchone()[0] == 1)
check("migration keeps first-login flags",
      conn.execute("SELECT must_change_password FROM users "
                   "WHERE student_id='mig_test'").fetchone()[0] == 1)
# v2.1 schema columns + settings migration
cols = [r[1] for r in conn.execute("PRAGMA table_info(computers)").fetchall()]
check("computers has hostname column", "hostname" in cols, str(cols))
check("computers has admin_state column", "admin_state" in cols, str(cols))

# --- spec 5: legacy inventory rows survive the quantity/status rebuild ----
conn.executescript("""
    ALTER TABLE inventory RENAME TO inventory_bak;
    CREATE TABLE inventory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_name TEXT NOT NULL,
        category TEXT,
        quantity TEXT,
        condition_status TEXT,
        location TEXT,
        remarks TEXT
    );
    INSERT INTO inventory (id, item_name, category, quantity,
                           condition_status, location, remarks)
        SELECT id, item_name, category, quantity,
               condition_status, location, COALESCE(notes, '')
        FROM inventory_bak;
    DROP TABLE inventory_bak;
""")
conn.execute(
    "INSERT INTO inventory (item_name, category, quantity, condition_status,"
    " location, remarks) VALUES (?,?,?,?,?,?)",
    ("Legacy Keyboard", "Peripherals", "5", "Good", "Lab A", "old stock"))
conn.execute(
    "INSERT INTO inventory (item_name, category, quantity, condition_status,"
    " location, remarks) VALUES (?,?,?,?,?,?)",
    ("Legacy Mouse", "Peripherals", "0", "Damaged", "Lab B", "broken"))
conn.commit()
database._migrate_legacy_schema(conn.cursor())
conn.commit()
lrow = conn.execute("SELECT * FROM inventory "
                    "WHERE item_name='Legacy Keyboard'").fetchone()
check("legacy inventory row survives the rebuild (spec 5)",
      lrow and lrow["quantity"] == 5 and lrow["available_qty"] == 5
      and lrow["assigned_qty"] == 0 and lrow["status"] == "AVAILABLE"
      and lrow["notes"] == "old stock" and lrow["date_added"],
      str(dict(lrow) if lrow else None))
lrow = conn.execute("SELECT * FROM inventory "
                    "WHERE item_name='Legacy Mouse'").fetchone()
check("legacy inventory derives status on rebuild",
      lrow and lrow["available_qty"] == 0 and lrow["status"] == "DAMAGED",
      str(dict(lrow) if lrow else None))
conn.close()
check("screenshot quality readable", database.get_setting("screenshot_quality") == "70",
      database.get_setting("screenshot_quality"))
check("screenshot scale readable", database.get_setting("screenshot_scale") == "0.75",
      database.get_setting("screenshot_scale"))

# ---------------------------------------------------------------- protocol
from protocol import (Message, MessageType, TLSSocketWrapper, create_ssl_context,
                      build_client_register, build_client_heartbeat,
                      build_auth_request, build_command_response,
                      build_session_start, build_session_end, build_stu_request,
                      build_password_change, build_activity_log)

# An orphaned "Active" session left behind by a crashed previous run - the
# server must heal it on startup.
conn = database.get_connection()
conn.execute("DELETE FROM client_sessions WHERE session_id='sessORPHAN'")
conn.execute(
    "INSERT INTO client_sessions "
    "(session_id, pc_name, student_id, full_name, login_time, status) "
    "VALUES ('sessORPHAN', 'ORPHAN-PC', 'orphan1', 'Ghost', ?, 'Active')",
    (now_datetime(),))
conn.commit()
conn.close()
m = Message.create(MessageType.CMD_LOCK, {"a": 1})
m2 = Message.from_bytes(m.to_bytes()[4:])
check("message frame roundtrip", m2.type == m.type and m2.payload == m.payload)

# ---------------------------------------------------------------- server up
from server import LabServer
events = queue.Queue()
srv = LabServer(host="127.0.0.1", port=18443,
                on_event=lambda k, d: events.put((k, d)))
srv.start()
time.sleep(0.4)
check("TLS server started", srv.running)
check("cert files present", os.path.exists("server.crt") and os.path.exists("server.key"))


def _keep_alive(pc="TEST-PC"):
    """Raw test clients don't heartbeat the way real clients do, so the
    server's 15 s sweep can age a PC out mid-run.  Refresh the entry's
    liveness right before any assertion that requires the PC online
    (pushes report sent>=1 / SYNCING only for online targets).

    The sweep clears is_online as well as the timestamp, so restoring
    only last_heartbeat would leave a PC it already aged out permanently
    offline (the flag is otherwise only re-set by a register/heartbeat).
    Both are refreshed here - exactly what the server's own heartbeat
    handler does."""
    with srv.clients_lock:
        _e = srv.clients.get(pc)
    if _e is not None:
        _e.last_heartbeat = time.time()
        _e.is_online = True

row = database.get_connection().execute(
    "SELECT status, logout_time FROM client_sessions "
    "WHERE session_id='sessORPHAN'").fetchone()
check("startup heals orphaned sessions",
      row and row["logout_time"] and row["status"] == "Disconnected",
      str(dict(row) if row else None))

# ---------------------------------------------------------------- TLS client
raw = socket.create_connection(("127.0.0.1", 18443), timeout=5)
tls = create_ssl_context(is_server=False).wrap_socket(raw, server_hostname="127.0.0.1")
w = TLSSocketWrapper(tls)
w.send_message(build_client_register("TEST-PC", "127.0.0.1", "testhost"))
time.sleep(0.4)
snap = next((c for c in srv.list_clients() if c["pc_name"] == "TEST-PC"), None)
check("client registered over TLS", snap is not None)
check("register carries hostname", snap and snap["hostname"] == "testhost", str(snap))
check("register carries ip", snap and snap["ip"] == "127.0.0.1")
check("no hardware specs collected", snap and "specs" not in snap)

# ---------------------------------------------------------------- auth
w.send_message(build_auth_request("admin", "admin123", "admin", {}))
r = w.recv_message(timeout=4)
check("auth success", r and r.payload.get("success") is True)
w.send_message(build_auth_request("admin", "WRONG", "admin", {}))
r = w.recv_message(timeout=4)
check("auth rejects bad password", r and r.payload.get("success") is False)
w.send_message(build_auth_request("ghost", "x", "student", {}))
r = w.recv_message(timeout=4)
check("auth rejects unknown user", r and r.payload.get("success") is False)
w.send_message(build_auth_request("staff", "staff123", "staff", {}))
r = w.recv_message(timeout=4)
check("staff login works", r and r.payload.get("success") is True
      and r.payload.get("user_data", {}).get("role") == "staff")

# Maintenance accounts exist only when an admin creates one (no
# hardcoded dev password), and the server always answers with the DB
# role - never the role the client claims in the auth request.
conn = database.get_connection()
conn.execute("DELETE FROM users WHERE student_id='maint01'")
conn.execute(
    "INSERT INTO users (student_id, password, full_name, role, status) "
    "VALUES (?,?,?,?,?)",
    ("maint01", database.hash_password(database.DEFAULT_CLIENT_PASSWORD),
     "Lab Technician", "maintenance", "Active"))
conn.commit()
conn.close()
w.send_message(build_auth_request(
    "maint01", database.DEFAULT_CLIENT_PASSWORD, "admin", {}))
r = w.recv_message(timeout=4)
check("maintenance login works (server-authoritative role)",
      r and r.payload.get("success") is True
      and r.payload.get("user_data", {}).get("role") == "maintenance",
      str(r and r.payload.get("user_data")))
check("maintenance is never forced to change its password",
      r and r.payload.get("user_data", {}).get("must_change_password") is False)

# ------------------------------------------- first-login password (spec)
# Fresh client (student) accounts are flagged at creation, and the
# factory default password itself always counts as still-default.
conn = database.get_connection()
conn.execute("DELETE FROM users WHERE student_id IN ('fresh01','fresh02')")
conn.execute(
    "INSERT INTO users (student_id, password, full_name, role, status, "
    "must_change_password) VALUES (?,?,?,?,?,?)",
    ("fresh01", database.hash_password(database.DEFAULT_CLIENT_PASSWORD),
     "Fresh Account", "student", "Active", 1))
conn.execute(
    "INSERT INTO users (student_id, password, full_name, role, status, "
    "must_change_password) VALUES (?,?,?,?,?,?)",
    ("fresh02", database.hash_password("customtemp99"),
     "Temp Account", "student", "Active", 1))
conn.commit()
check("users table has the first-login flag column",
      "must_change_password" in
      [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()])
row = conn.execute(
    "SELECT must_change_password FROM users "
    "WHERE student_id='2023-00001'").fetchone()
check("seeded demo student is not flagged",
      row and not row["must_change_password"], str(row and dict(row)))
conn.close()

# a SEPARATE TLS connection so the main TEST-PC session flow is untouched
raw2 = socket.create_connection(("127.0.0.1", 18443), timeout=5)
tls2 = create_ssl_context(is_server=False).wrap_socket(
    raw2, server_hostname="127.0.0.1")
w2 = TLSSocketWrapper(tls2)

w2.send_message(build_auth_request("fresh01", "password123", "student", {}))
r = w2.recv_message(timeout=4)
check("default-password login succeeds but forces a change",
      r and r.payload.get("success") is True
      and r.payload.get("user_data", {}).get("must_change_password") is True,
      str(r and r.payload.get("user_data")))

# no normal usage before the change: session_start is refused server-side
w2.send_message(build_session_start("FRESH-PC", "fresh01", "Fresh Account",
                                    "sessFRESH"))
time.sleep(0.4)
row = database.get_connection().execute(
    "SELECT id FROM client_sessions WHERE session_id='sessFRESH'").fetchone()
check("session start blocked until the password changes", row is None)

# invalid replacements are rejected before anything is written
w2.send_message(build_password_change("abc"))
r = w2.recv_message(timeout=4)
check("short new password rejected",
      r and r.payload.get("success") is False
      and "at least 6" in r.payload.get("error", ""),
      str(r and r.payload))
w2.send_message(build_password_change("password123"))
r = w2.recv_message(timeout=4)
check("new password cannot be the default",
      r and r.payload.get("success") is False
      and "default" in r.payload.get("error", "").lower(),
      str(r and r.payload))
w2.send_message(build_password_change("labpass99"))
r = w2.recv_message(timeout=4)
check("valid replacement accepted",
      r and r.payload.get("success") is True, str(r and r.payload))
row = database.get_connection().execute(
    "SELECT password, must_change_password FROM users "
    "WHERE student_id='fresh01'").fetchone()
check("new password stored hashed + flag cleared",
      row and not row["must_change_password"]
      and database.verify_password("labpass99", row["password"])
      and not database.verify_password("password123", row["password"]),
      str(dict(row) if row else None))
check("no plaintext password stored",
      row and "labpass99" not in row["password"])

# flag-based detection: a custom temp password forces the change too
w2.send_message(build_auth_request("fresh02", "customtemp99", "student", {}))
r = w2.recv_message(timeout=4)
check("custom temp login also forces a change",
      r and r.payload.get("success") is True
      and r.payload.get("user_data", {}).get("must_change_password") is True,
      str(r and r.payload.get("user_data")))
w2.send_message(build_password_change("customtemp99"))
r = w2.recv_message(timeout=4)
check("replacement must differ from the current password",
      r and r.payload.get("success") is False
      and "different from your current" in r.payload.get("error", ""),
      str(r and r.payload))
w2.send_message(build_password_change("newsecret42"))
r = w2.recv_message(timeout=4)
check("flagged account can complete the change",
      r and r.payload.get("success") is True, str(r and r.payload))

# after the change the account logs in normally (no forced screen)
w2.send_message(build_auth_request("fresh02", "newsecret42", "student", {}))
r = w2.recv_message(timeout=4)
check("changed account logs in without a forced change",
      r and r.payload.get("success") is True
      and r.payload.get("user_data", {}).get("must_change_password") is False,
      str(r and r.payload.get("user_data")))
w2.send_message(build_session_start("FRESH-PC", "fresh02", "Temp Account",
                                    "sessFRESH2"))
time.sleep(0.3)
row = database.get_connection().execute(
    "SELECT status FROM client_sessions "
    "WHERE session_id='sessFRESH2'").fetchone()
check("session allowed after the password change",
      row and row["status"] == "Active", str(dict(row) if row else None))
w2.send_message(build_session_end("sessFRESH2", time.time(), 1))
time.sleep(0.2)

# admin/staff are never forced, and the old default login stops working
w2.send_message(build_auth_request("admin", "admin123", "admin", {}))
r = w2.recv_message(timeout=4)
check("admin default login is not forced to change",
      r and r.payload.get("success") is True
      and r.payload.get("user_data", {}).get("must_change_password") is False,
      str(r and r.payload.get("user_data")))
w2.send_message(build_auth_request("fresh01", "password123", "student", {}))
r = w2.recv_message(timeout=4)
check("old default password no longer works",
      r and r.payload.get("success") is False, str(r and r.payload))

# audit trail records the change (who / PC / timestamp / action)
rows = database.get_connection().execute(
    "SELECT admin_user, target FROM admin_activity_log "
    "WHERE action='password_changed' "
    "AND admin_user IN ('fresh01','fresh02')").fetchall()
check("audit trail has both password_changed events",
      len(rows) >= 2, str([dict(x) for x in rows]))
try:
    w2.sock.close()
except Exception:
    pass

# --------------------------- close / disconnect event taxonomy (P3):
# Normal Logout / Client Closed / Client Crash / Network Disconnect must
# stay four distinct server-side events.
import server as server_mod
server_mod.CRASH_GRACE_SECONDS = 2       # escalate quickly for this test

# start from a clean slate: scratch-PC rows from any earlier run must not
# pollute these target-scoped activity assertions (stale lab_system.db)
conn = database.get_connection()
conn.execute("DELETE FROM admin_activity_log "
             "WHERE target IN ('DROP-PC','CRASH-PC')")
conn.execute("DELETE FROM computers "
             "WHERE pc_name IN ('DROP-PC','CRASH-PC')")
conn.execute("DELETE FROM client_sessions "
             "WHERE pc_name IN ('DROP-PC','CRASH-PC')")
conn.commit()
conn.close()

# (a) Client Closed: the dashboard's X reports itself over the normal
#     activity channel while the session keeps running.
# (b) Network Disconnect: a silent socket drop is recorded as a
#     disconnect - never as a logout.
raw3 = socket.create_connection(("127.0.0.1", 18443), timeout=5)
tls3 = create_ssl_context(is_server=False).wrap_socket(
    raw3, server_hostname="127.0.0.1")
wcon = TLSSocketWrapper(tls3)
wcon.send_message(build_client_register("DROP-PC", "127.0.0.1", "drophost"))
wcon.send_message(build_auth_request("2023-00001", "student123", "student", {}))
r = wcon.recv_message(timeout=4)
check("disconnect-PC authenticated", r is not None
      and r.payload.get("success") is True, str(r and r.payload))
wcon.send_message(build_session_start("DROP-PC", "2023-00001",
                                      "Juan Dela Cruz", "sessDROP"))
time.sleep(0.3)
wcon.send_message(build_activity_log(
    "2023-00001", "client_closed", "DROP-PC",
    "Client window closed by user (session continues)"))
time.sleep(0.3)
wcon.sock.close()                        # silent drop = Network Disconnect
time.sleep(1.0)                          # server detects the EOF at once
conn = database.get_connection()
drop_actions = [x["action"] for x in conn.execute(
    "SELECT action FROM admin_activity_log "
    "WHERE target='DROP-PC'").fetchall()]
sess = conn.execute("SELECT status FROM client_sessions "
                    "WHERE session_id='sessDROP'").fetchone()
conn.close()
check("silent drop recorded as network_disconnect",
      "network_disconnect" in drop_actions, str(drop_actions))
check("drop never recorded as a logout",
      "client_logout" not in drop_actions, str(drop_actions))
check("client_closed recorded as its own event",
      "client_closed" in drop_actions, str(drop_actions))
check("dropped session closed as Disconnected",
      sess and sess["status"] == "Disconnected",
      str(dict(sess) if sess else None))

# (c) reconnect inside the grace window -> client_reconnect, no crash
raw4 = socket.create_connection(("127.0.0.1", 18443), timeout=5)
tls4 = create_ssl_context(is_server=False).wrap_socket(
    raw4, server_hostname="127.0.0.1")
wcon2 = TLSSocketWrapper(tls4)
wcon2.send_message(build_client_register("DROP-PC", "127.0.0.1", "drophost"))
time.sleep(2.6)                          # well past the 2s crash window
conn = database.get_connection()
drop_actions = [x["action"] for x in conn.execute(
    "SELECT action FROM admin_activity_log "
    "WHERE target='DROP-PC'").fetchall()]
conn.close()
check("reconnect inside grace -> client_reconnect",
      "client_reconnect" in drop_actions, str(drop_actions))
check("reconnect suppresses the crash escalation",
      "client_crash" not in drop_actions, str(drop_actions))
wcon2.sock.close()                       # asserted above already

# (d) Client Crash: a drop with no reconnect escalates after the grace
raw5 = socket.create_connection(("127.0.0.1", 18443), timeout=5)
tls5 = create_ssl_context(is_server=False).wrap_socket(
    raw5, server_hostname="127.0.0.1")
wcrash = TLSSocketWrapper(tls5)
wcrash.send_message(build_client_register("CRASH-PC", "127.0.0.1",
                                          "crashhost"))
time.sleep(0.3)
wcrash.sock.close()                      # drop and never come back
time.sleep(3.0)                          # 2s grace + margin
conn = database.get_connection()
crash_actions = [x["action"] for x in conn.execute(
    "SELECT action FROM admin_activity_log "
    "WHERE target='CRASH-PC'").fetchall()]
# keep every later page check identical to the baseline: drop the
# scratch PCs again
conn.execute("DELETE FROM computers "
             "WHERE pc_name IN ('DROP-PC','CRASH-PC')")
conn.execute("DELETE FROM client_sessions "
             "WHERE pc_name IN ('DROP-PC','CRASH-PC')")
conn.commit()
conn.close()
check("no-reconnect drop escalates to client_crash",
      "network_disconnect" in crash_actions
      and "client_crash" in crash_actions, str(crash_actions))
server_mod.CRASH_GRACE_SECONDS = 30

# --------------------- Website Access: push, apply, enforcement (P1.4)
import tempfile
from protocol import MessageType, build_stu_request
import client as client_mod

check("web filter list starts empty on a fresh server",
      srv.get_web_filter() == [], str(srv.get_web_filter()))
conn = database.get_connection()
conn.execute("INSERT INTO websites (domain) VALUES ('bad.example')")
conn.execute("INSERT INTO websites (domain) VALUES ('spam.test')")
conn.commit()
conn.close()
check("get_web_filter reads the central website list",
      srv.get_web_filter() == ["bad.example", "spam.test"],
      str(srv.get_web_filter()))
# the client fetches the full policy snapshot over the stu channel
for _ in range(50):                        # keep the raw socket in step
    if w.recv_message(timeout=0.4) is None:
        break
w.send_message(build_stu_request("web_filter"))
r = w.recv_message(timeout=4)
_pol = (r.payload.get("data") or {}) if r is not None else {}
check("web_filter request answers the full policy snapshot",
      r is not None and r.type == MessageType.STU_RESPONSE.value
      and r.payload.get("kind") == "web_filter"
      and r.payload.get("ok") is True
      and isinstance(_pol, dict)
      and _pol.get("mode") == "allow_all"          # fresh-server default
      and _pol.get("blocked") == ["bad.example", "spam.test"]
      and isinstance(_pol.get("version"), int),
      str(dict(r.payload) if r else None))
# an admin change pushes the versioned policy to every online client
_keep_alive()
res = srv.push_web_filter("admin")
check("push_web_filter reaches the online client",
      res.get("sent", 0) >= 1
      and res.get("targets", 0) >= 1
      and isinstance(res.get("version"), int), str(res))
# spec item 8: the row only says SYNCED after the client's ack - never
# from the push alone
_prow = database.get_connection().execute(
    "SELECT sync_status, version FROM web_pc_policy "
    "WHERE pc_name='TEST-PC'").fetchone()
check("push marks the target SYNCING until the ack arrives",
      _prow is not None and _prow["sync_status"] == "SYNCING"
      and _prow["version"] == res["version"],
      str(dict(_prow) if _prow else None))
pushed = []
for _ in range(50):
    m = w.recv_message(timeout=0.6)
    if m is None:
        break
    pushed.append(m.type)
check("online client received cmd_web_filter",
      MessageType.CMD_WEB_FILTER.value in pushed, str(pushed))
# the hosts applier is now a CLEARER ONLY (P1.4): enforcement moved to
# connection detection, so a domain list is never written back - a block
# left by an older build would keep the connection from ever happening
# and make detection blind
_hosts = os.path.join(tempfile.gettempdir(), "lab_hosts_probe.txt")
with open(_hosts, "w", encoding="utf-8") as f:
    f.write("127.0.0.1 localhost\n# user line stays\n"
            + client_mod.HOSTS_BLOCK_BEGIN + "\n"
            "0.0.0.0 legacy.example\n"
            + client_mod.HOSTS_BLOCK_END + "\n")
ok = client_mod.apply_web_filter(
    ["https://Bad.Example/page", "spam.test"], hosts_path=_hosts)
txt = open(_hosts, encoding="utf-8").read()
check("hosts applier clears the managed block, never writes one",
      ok and client_mod.HOSTS_BLOCK_BEGIN not in txt
      and client_mod.HOSTS_BLOCK_END not in txt
      and "legacy.example" not in txt
      and txt.startswith("127.0.0.1 localhost")
      and "# user line stays" in txt, repr(txt))
client_mod.apply_web_filter(["bad.example", "spam.test"], hosts_path=_hosts)
txt2 = open(_hosts, encoding="utf-8").read()
check("hosts applier is idempotent on a clean file", txt2 == txt, repr(txt2))
client_mod.apply_web_filter([], hosts_path=_hosts)
txt3 = open(_hosts, encoding="utf-8").read()
check("a clean file needs no write at all",
      txt3 == txt and "0.0.0.0" not in txt3, repr(txt3))
# an unwritable hosts file with no leftover block is NOT a failure any more
_ro = os.path.join(tempfile.gettempdir(), "lab_hosts_absent.txt")
check("a missing hosts file with no block reports success",
      client_mod.apply_web_filter([], hosts_path=_ro) is True, _ro)
os.remove(_hosts)
# apply_web_policy: detection is the enforcement, the snapshot is
# persisted, and ALLOW ALL only claims success after really repairing
_hosts2 = os.path.join(tempfile.gettempdir(), "lab_hosts_policy.txt")
with open(_hosts2, "w", encoding="utf-8") as f:
    f.write("127.0.0.1 localhost\n")
_ok, _enf, _err = client_mod.apply_web_policy(
    {"version": 1, "mode": "block_list", "blocked": ["bad.example"],
     "allowed": []}, hosts_path=_hosts2, manage_dns=False)
_txt = open(_hosts2, encoding="utf-8").read()
check("BLOCK LIST is enforced by detection, not by a hosts block",
      _ok is True and _enf == "detect" and _err is None
      and "0.0.0.0" not in _txt
      and (client_mod._web_detector.get_policy() or {}).get("blocked")
      == ["bad.example"], f"{_ok},{_enf},{_txt},"
      f"{(client_mod._web_detector or None) and client_mod._web_detector.get_policy()}")
_ok, _enf, _err = client_mod.apply_web_policy(
    {"version": 2, "mode": "allow_all", "blocked": [], "allowed": []},
    hosts_path=_hosts2, manage_dns=False)
_txt = open(_hosts2, encoding="utf-8").read()
check("apply_web_policy clears the hosts block on ALLOW ALL",
      _ok is True and _enf == "none" and "0.0.0.0" not in _txt,
      f"{_ok},{_enf},{_txt}")
_ok, _enf, _err = client_mod.apply_web_policy(
    {"version": 3, "mode": "allow_only", "blocked": [],
     "allowed": ["s.test"]}, hosts_path=_hosts2, manage_dns=False)
check("ALLOW ONLY is enforced by detection when the socket table is readable",
      _ok is True and _enf == "detect" and _err is None,
      f"{_ok},{_enf},{_err}")
check("applied policy persists to lab_config (offline restarts)",
      client_mod.load_config().get("web_policy", {}).get("version") == 3,
      str(client_mod.load_config().get("web_policy")))
# honesty: an empty allow list is a misconfiguration, never "enforced"
_ok, _enf, _err = client_mod.apply_web_policy(
    {"version": 4, "mode": "allow_only", "blocked": [], "allowed": []},
    hosts_path=_hosts2, manage_dns=False)
check("ALLOW ONLY with an empty list is reported FAILED, not enforced",
      _ok is False and _enf == "none" and "ALLOW ONLY" in (_err or ""),
      f"{_ok},{_enf},{_err}")
# honesty: without socket access nothing can be attributed -> FAILED
_o_probe = client_mod.web_access.probe_access
client_mod.web_access.probe_access = lambda: (
    False, "cannot identify which process owns another application's "
           "connection - run the Client as Administrator")
_ok, _enf, _err = client_mod.apply_web_policy(
    {"version": 5, "mode": "block_list", "blocked": ["bad.example"],
     "allowed": []}, hosts_path=_hosts2, manage_dns=False)
client_mod.web_access.probe_access = _o_probe
check("an unreadable socket table is reported FAILED, never SYNCED",
      _ok is False and _enf == "none" and "Administrator" in (_err or ""),
      f"{_ok},{_enf},{_err}")
client_mod._web_forget()
os.remove(_hosts2)

# ---- P1.0: DNS repair + honest restore reporting ----------------------
# A restart mid-policy used to record 127.0.0.1 as the adapter's own
# "previous" value, so ALLOW ALL restored 127.0.0.1 and the PC lost name
# resolution for good.  These checks pin down the fix: loopback is never
# a "previous" value, a failed restore is reported as a failure, and
# repair only ever touches an adapter that is actually repointed.
import dns_filter as _df
check("loopback DNS recognised and stripped (never its own previous)",
      _df.is_local_dns(["127.0.0.1"]) and _df.is_local_dns(["8.8.8.8", "::1"])
      and _df.drop_local_dns(["127.0.0.1", "192.168.1.1"]) == ["192.168.1.1"]
      and _df.drop_local_dns(["127.0.0.1"]) == []
      and not _df.is_local_dns([]) and not _df.is_local_dns(None),
      str(_df.drop_local_dns(["127.0.0.1", "192.168.1.1"])))
_pk = _df._query_packet("example.com")
check("repair probe builds a well-formed DNS question",
      isinstance(_pk, (bytes, bytearray)) and len(_pk) > 12
      and _pk[2:4] == b"\x01\x00"          # standard query, recursion wanted
      and _pk[4:6] == b"\x00\x01"           # QDCOUNT = 1
      and b"\x07example\x03com\x00" in _pk  # QNAME: example.com
      and _pk[-4:] == b"\x00\x01\x00\x01",  # QTYPE=A, QCLASS=IN
      repr(_pk))
_t0 = time.time()
_dead = _df.probe_local_resolver(port=15999, timeout=0.3)
check("probe answers 'no resolver' when nothing is listening",
      _dead is False and time.time() - _t0 < 3.0,
      f"{_dead},{time.time() - _t0:.2f}s")
# the guardrail half: a resolver that ANSWERS must be detectable, so
# repair_adapter_dns() can hands-off an administrator's own local DNS
_lz = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
_lz.bind(("127.0.0.1", 0))
_lz_port = _lz.getsockname()[1]
def _lz_reply():
    try:
        _lz.settimeout(3.0)
        _d, _a = _lz.recvfrom(512)
        if _d:
            _lz.sendto(b"local-resolver", _a)
    except Exception:
        pass
threading.Thread(target=_lz_reply, daemon=True).start()
time.sleep(0.1)
_live = _df.probe_local_resolver(port=_lz_port, timeout=2.0)
_lz.close()
check("probe detects a LIVE local resolver (never wipe one)",
      _live is True, str(_live))
# regression: hanging -ErrorAction off the .ServerAddresses property is a
# PowerShell PARSE ERROR (rc=1) and made the query return nothing, so the
# real previous servers were never read and repair could not see a stuck
# adapter at all.
_rc, _out = _df._ps("(Get-DnsClientServerAddress -InterfaceAlias "
                    "'__no_such_adapter__' -AddressFamily IPv4 "
                    "-ErrorAction SilentlyContinue).ServerAddresses "
                    "| ConvertTo-Json -Compress")
check("adapter DNS query is valid PowerShell (rc=0, not a parse error)",
      _rc == 0, f"rc={_rc} out={_out!r}")
check("adapter DNS can be read back as a list",
      isinstance(_df.get_adapter_dns(_df.active_adapter() or ""), list),
      str(_df.get_adapter_dns(_df.active_adapter() or "")))
check("restore refuses to guess an adapter (touch nothing)",
      _df.restore_adapter_dns("", ["192.168.1.1"]) ==
      (False, "no adapter to restore"),
      str(_df.restore_adapter_dns("", ["192.168.1.1"])))
_act, _okr, _errr = _df.repair_adapter_dns(None,
                                            adapter="no-such-adapter-xyz")
check("repair does nothing when the adapter is not repointed",
      _act is False and _okr is True and not _errr,
      f"{_act},{_okr},{_errr}")
# P1.4: the local DNS proxy and the adapter repoint it drove are gone
# (a run killed before its restore left the PC with no name resolution),
# so the only thing that ever writes an adapter is repair_adapter_dns
check("the enforcement transport is really gone",
      not hasattr(_df, "DnsPolicyFilter")
      and not hasattr(_df, "set_adapter_dns")
      and not hasattr(client_mod, "_dns_filter_ensure")
      and not hasattr(client_mod, "_dns_filter_stop"),
      str([hasattr(_df, "DnsPolicyFilter"), hasattr(_df, "set_adapter_dns"),
           hasattr(client_mod, "_dns_filter_ensure"),
           hasattr(client_mod, "_dns_filter_stop")]))

# ALLOW ALL must repair even when nothing is enforcing (the state after a
# watchdog kill), and must never report success it did not verify
_orig_repair = client_mod.repair_local_dns
_orig_awf = client_mod.apply_web_filter
_rep = []
client_mod.repair_local_dns = lambda *a, **k: (_rep.append(1), (True, ""))[1]
client_mod.apply_web_filter = lambda domains, hosts_path=None: True
_ok, _enf, _err = client_mod.apply_web_policy(
    {"version": 40, "mode": "allow_all", "blocked": [], "allowed": []},
    manage_dns=True)
check("ALLOW ALL repairs DNS with nothing enforcing anymore",
      _rep == [1] and _ok is True and _enf == "none" and _err is None,
      f"{_rep},{_ok},{_enf},{_err}")
_rep.clear()
client_mod.apply_web_policy(
    {"version": 41, "mode": "allow_all", "blocked": [], "allowed": []},
    manage_dns=False)
check("ALLOW ALL leaves the system alone when manage_dns=False",
      _rep == [], str(_rep))
client_mod.repair_local_dns = lambda *a, **k: (False, "repair failed")
_ok, _enf, _err = client_mod.apply_web_policy(
    {"version": 43, "mode": "allow_all", "blocked": [], "allowed": []},
    manage_dns=True)
check("ALLOW ALL reports a failed DNS repair honestly",
      _ok is False and "repair failed" in (_err or ""), f"{_ok},{_err}")
client_mod.repair_local_dns = lambda *a, **k: (True, "")
_hb = os.path.join(tempfile.gettempdir(), "lab_hosts_stuck.txt")
with open(_hb, "w", encoding="utf-8") as f:
    f.write("127.0.0.1 localhost\n" + client_mod.HOSTS_BLOCK_BEGIN + "\n"
            + "0.0.0.0 bad.example\n" + client_mod.HOSTS_BLOCK_END + "\n")
check("host-block detector sees our managed block",
      client_mod._hosts_block_present(_hb) is True
      and client_mod._hosts_block_present(_hosts + ".missing") is False,
      str(client_mod._hosts_block_present(_hb)))
client_mod.apply_web_filter = lambda domains, hosts_path=None: False
_ok, _enf, _err = client_mod.apply_web_policy(
    {"version": 44, "mode": "allow_all", "blocked": [], "allowed": []},
    hosts_path=_hb, manage_dns=False)
check("ALLOW ALL fails when a leftover block cannot be cleared",
      _ok is False and "hosts file not writable" in (_err or ""),
      f"{_ok},{_err}")
_hb2 = os.path.join(tempfile.gettempdir(), "lab_hosts_clean.txt")
with open(_hb2, "w", encoding="utf-8") as f:
    f.write("127.0.0.1 localhost\n")
_ok, _enf, _err = client_mod.apply_web_policy(
    {"version": 45, "mode": "allow_all", "blocked": [], "allowed": []},
    hosts_path=_hb2, manage_dns=False)
check("ALLOW ALL does not fail on a hosts file that has no block of ours",
      _ok is True and _enf == "none", f"{_ok},{_enf},{_err}")
client_mod.repair_local_dns = _orig_repair
client_mod.apply_web_filter = _orig_awf
client_mod._web_forget()
for _p in (_hb, _hb2):
    if os.path.exists(_p):
        os.remove(_p)

# ---- P1.1: connection-based Website Access detection ---------------------
# The detector replaces "repoint the adapter at 127.0.0.1 and hope" with
# proof of access: connection -> domain -> responsible browser -> verdict.
# Everything is driven by injected resolvers/socket tables so no test ever
# touches real DNS, real sockets or a real browser.
import struct as _struct
import web_access as _wa


def _mk_wa(mode, blocked=(), allowed=(), cmap=None, conns=(), rev=""):
    """Detector with an injected resolver, socket table and reverse
    lookup.  The background thread is stopped immediately: these tests
    refresh by hand so results are deterministic."""
    cmap = dict(cmap or {})
    holder = {"c": list(conns)}

    def _res(dom, upstream=None):
        return set(cmap.get(dom, ()))

    det = _wa.WebAccessDetector(resolver=_res,
                                connections=lambda: list(holder["c"]))
    det._reverse = lambda ip: rev
    det.set_policy({"version": 1, "mode": mode, "blocked": list(blocked),
                    "allowed": list(allowed), "upstream_dns": ""})
    det.stop()
    det.refresh()
    return det, holder


def _boom():
    raise AssertionError("ALLOW ALL must never read the socket table")


_det, _ = _mk_wa("allow_all", conns=[("203.0.113.5", 4242, "ESTABLISHED")])
_det._connections_fn = _boom
check("[web] ALLOW ALL scans nothing and emits nothing",
      _det.poll() == [], str(_det.poll()))
_det.stop()

_det, _ = _mk_wa("block_list", blocked=["bad.example"],
                 cmap={"bad.example": ["203.0.113.5"],
                       "www.bad.example": ["203.0.113.6"]})
check("[web] block_list verdict keeps dns_filter suffix semantics",
      _det.violations("bad.example") == (True, "blocked")
      and _det.violations("WWW.Bad.Example") == (True, "blocked")
      and _det.violations("sub.bad.example") == (True, "blocked")
      and _det.violations("notbad.example") == (False, "")
      and _det.violations("ok.example") == (False, ""),
      str(_det.violations("notbad.example")))
_detw, _ = _mk_wa("block_list", blocked=["bad.example", "x.example"],
                  allowed=["ok.example"])
check("[web] both rule lists are watched (shared addresses stay detectable)",
      _detw._watch == ["bad.example", "x.example", "ok.example"]
      and _mk_wa("allow_only", allowed=["ok.example"],
                 blocked=["bad.example"])[0]._watch
      == ["ok.example", "bad.example"]
      and _mk_wa("allow_all")[0]._watch == [],
      str(_detw._watch))
_detw.stop()
_det.stop()

# ---- P1.5: the two coverage gaps that made real blocking silently fail --
# (a) a bare rule must also watch its www entry point: typing
#     youtube.com lands the browser on www.youtube.com, whose address
#     pool is DISJOINT from the apex - a connection to it could never be
#     matched.  _watch stays the rule list itself (pinned above); the
#     variant is a refresh-time lookup candidate, and every address it
#     learns is attributed to the RULE domain.
# (b) resolve_domain must map BOTH resolver views (upstream + system),
#     because the browser resolves through the system path.
_calls = []


def _res_www(dom, upstream=None):
    _calls.append(dom)
    return {"bad.example": ["203.0.113.5"],
            "www.bad.example": ["198.51.100.9"]}.get(dom, ())


_detv = _wa.WebAccessDetector(resolver=_res_www,
                              connections=lambda: [("198.51.100.9", 4242,
                                                    "ESTABLISHED")])
_detv.set_policy({"version": 1, "mode": "block_list",
                  "blocked": ["bad.example"], "allowed": [],
                  "upstream_dns": ""})
_detv.stop()
_detv.refresh()
check("[web] a rule also resolves its www entry point, attributed to the rule",
      "www.bad.example" in _calls and _detv._watch == ["bad.example"]
      and _detv._ip_map.get("198.51.100.9") == {"bad.example"},
      f"{_calls},{_detv._ip_map}")
_ev = _detv.poll()
check("[web] a browser on the www pool is DETECTED for the bare rule",
      len(_ev) == 1 and _ev[0]["status"] == "DETECTED"
      and _ev[0]["domain"] == "bad.example"
      and _ev[0]["reason"] == "blocked", str(_ev))
_detv.stop()

# the symmetric direction: a rule copied WITH www. still matches the apex
_calls = []


def _res_apex(dom, upstream=None):
    _calls.append(dom)
    return {"www.x.example": ["203.0.113.6"],
            "x.example": ["198.51.100.4"]}.get(dom, ())


_detv = _wa.WebAccessDetector(resolver=_res_apex,
                              connections=lambda: [("198.51.100.4", 4242,
                                                    "ESTABLISHED")])
_detv.set_policy({"version": 1, "mode": "block_list",
                  "blocked": ["www.x.example"], "allowed": [],
                  "upstream_dns": ""})
_detv.stop()
_detv.refresh()
_ev = _detv.poll()
check("[web] a www-prefixed rule still matches the apex host",
      "x.example" in _calls and len(_ev) == 1
      and _ev[0]["status"] == "DETECTED"
      and _ev[0]["domain"] == "www.x.example", f"{_calls},{_ev}")
_detv.stop()

# resolve_domain: upstream answers AND the system view, unioned
_pkt = (_struct.pack("!HHHHHH", 0x1111, 0x8180, 1, 1, 0, 0)
        + b"\x06x\x04test\x00" + _struct.pack("!HH", 1, 1)
        + b"\xc0\x0c" + _struct.pack("!HHIH", 1, 1, 60, 4)
        + socket.inet_aton("203.0.113.5"))
_raw_orig, _gai_orig = _wa._raw_query, socket.getaddrinfo
try:
    _wa._raw_query = lambda server, name, qtype, timeout=2.0: (
        _pkt if qtype == 1 else None)
    socket.getaddrinfo = lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.51.100.9", 0))]
    _union = _wa.resolve_domain("test.example", upstream="9.9.9.9")
finally:
    _wa._raw_query, socket.getaddrinfo = _raw_orig, _gai_orig
check("[web] resolve_domain maps the upstream AND the system view",
      _union == {"203.0.113.5", "198.51.100.9"}, str(_union))

# a real connection to a blocked address -> one DETECTED event, and the
# same still-open connection must not be reported over and over
_det, _ = _mk_wa("block_list", blocked=["bad.example"],
                 cmap={"bad.example": ["203.0.113.5"]},
                 conns=[("203.0.113.5", 4242, "ESTABLISHED")])
_ev = _det.poll()
check("[web] blocked connection is DETECTED with its domain",
      len(_ev) == 1 and _ev[0]["status"] == "DETECTED"
      and _ev[0]["reason"] == "blocked"
      and _ev[0]["domain"] == "bad.example"
      and _ev[0]["ip"] == "203.0.113.5" and _ev[0]["pid"] == 4242,
      str(_ev))
check("[web] one event per connection episode (no duplicates)",
      _det.poll() == [], str(_det.poll()))
# ...but once the connection is gone for longer than an episode the next
# visit is a new event again
_det._episodes.clear()
_ev2 = _det.poll()
check("[web] a new visit to the same address reports again",
      len(_ev2) == 1, str(_ev2))
_det.stop()

# one address serving policy domains with OPPOSITE verdicts (shared CDN):
# never guess, never produce a kill signal
_det, _ = _mk_wa("block_list", blocked=["bad.example"],
                 allowed=["ok.example"],
                 cmap={"bad.example": ["203.0.113.9"],
                       "ok.example": ["203.0.113.9"]},
                 conns=[("203.0.113.9", 4242, "ESTABLISHED")])
_ev = _det.poll()
check("[web] shared address with mixed verdicts is UNRESOLVED, not guessed",
      len(_ev) == 1 and _ev[0]["status"] == "UNRESOLVED"
      and _ev[0]["reason"] == "ambiguous-domain", str(_ev))
_det.stop()

# our own PID, LAN addresses and unknown socket states are never events
_det, _ = _mk_wa("block_list", blocked=["bad.example", "lan.example"],
                 cmap={"bad.example": ["203.0.113.5"],
                       "lan.example": ["192.168.1.10"]},
                 conns=[("203.0.113.5", os.getpid(), "ESTABLISHED"),
                        ("203.0.113.5", 0, "ESTABLISHED"),
                        ("203.0.113.5", 4242, "WEIRD-STATE"),
                        ("192.168.1.10", 4242, "ESTABLISHED")])
check("[web] own PID, PID-less, unknown state and LAN are never events",
      _det.poll() == [], str(_det.poll()))
_det.stop()

# a socket table we cannot read must be contained, never raised
_det, _ = _mk_wa("block_list", blocked=["bad.example"],
                 cmap={"bad.example": ["203.0.113.5"]})
_det._connections_fn = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
check("[web] a failing socket scan is contained, never raised",
      _det.poll() == [] and "boom" in _det.last_error,
      f"{_det.poll()},{_det.last_error}")
_det.stop()

# ALLOW ONLY: an address it cannot name is UNRESOLVED (never DETECTED),
# and an empty allow-list flags nothing at all
_det, _ = _mk_wa("allow_only", allowed=["ok.example"],
                 cmap={"ok.example": ["203.0.113.20"]},
                 conns=[("198.51.100.7", 4242, "ESTABLISHED")], rev="")
_ev = _det.poll()
check("[web] ALLOW ONLY records UNRESOLVED for an address it cannot name",
      len(_ev) == 1 and _ev[0]["status"] == "UNRESOLVED"
      and _ev[0]["reason"] == "unnameable" and not _ev[0]["domain"],
      str(_ev))
_det.stop()

_det, _ = _mk_wa("allow_only", allowed=["ok.example"],
                 cmap={"ok.example": ["203.0.113.20"]},
                 conns=[("198.51.100.7", 4242, "ESTABLISHED")],
                 rev="evil.example")
_ev = _det.poll()
check("[web] ALLOW ONLY flags a named address that is not allowed",
      len(_ev) == 1 and _ev[0]["status"] == "DETECTED"
      and _ev[0]["reason"] == "not_allowed"
      and _ev[0]["domain"] == "evil.example", str(_ev))
_det.stop()

_det, _ = _mk_wa("allow_only", allowed=[],
                 conns=[("198.51.100.7", 4242, "ESTABLISHED")])
_ev = _det.poll()
check("[web] empty ALLOW ONLY list flags nothing (never mass-detect)",
      _ev == [] and "no allowed domains" in _det.last_error,
      f"{_ev},{_det.last_error}")
_det.stop()

# address classification + packet parsing (pure, no I/O)
check("[web] only public addresses are treated as websites",
      _wa.is_public_ip("203.0.113.5")
      and _wa.is_public_ip("2606:4700:4700::1111")
      and _wa.is_public_ip("172.200.1.1")      # 172.16/12 stops at .31
      and not _wa.is_public_ip("192.168.1.5")
      and not _wa.is_public_ip("10.1.2.3")
      and not _wa.is_public_ip("172.16.0.1")
      and not _wa.is_public_ip("172.31.255.1")
      and not _wa.is_public_ip("127.0.0.1")
      and not _wa.is_public_ip("169.254.1.1")
      and not _wa.is_public_ip("fe80::1%12")
      and not _wa.is_public_ip("0.0.0.0") and not _wa.is_public_ip(""),
      str(_wa.is_public_ip("172.31.255.1")))
check("[web] an IPv6 zone id never reaches the match table",
      _wa._clean_ip("fe80::1%12") == "fe80::1"
      and _wa._clean_ip("[2606:4700::1111]") == "2606:4700::1111",
      repr(_wa._clean_ip("fe80::1%12")))
_qn = b"\x06x\x04test\x00"
_hdr = _struct.pack("!HHHHHH", 0x1111, 0x8180, 1, 1, 0, 0)
_qst = _qn + _struct.pack("!HH", 1, 1)
_ans = (b"\xc0\x0c" + _struct.pack("!HHIH", 1, 1, 60, 4)
        + socket.inet_aton("203.0.113.5"))
check("[web] A response is parsed to its address (own lookups, not DNS)",
      _wa.parse_answers(_hdr + _qst + _ans) == [("A", "203.0.113.5")]
      and _wa.parse_answers(b"") == [], str(_wa.parse_answers(_hdr + _qst + _ans)))
check("[web] capability probe reports a bool, never raises",
      isinstance(_wa.probe_access()[0], bool), str(_wa.probe_access()))
check("[web] our own process is never named as the responsible browser",
      _wa.process_name(0) == "" and _wa.process_name(os.getpid()) == ""
      and _wa.browser_root(os.getpid()) == (0, ""),
      f"{_wa.process_name(os.getpid())},{_wa.browser_root(os.getpid())}")
_det, _ = _mk_wa("allow_all")
check("[web] status() is OK without scanning while ALLOW ALL is active",
      _det.status() == ("OK", ""), str(_det.status()))
_det.stop()

# ---- P1.2: the five-condition guarded close ------------------------------
# A browser is closed through ONE re-validated PID.  The process factory
# is injected so these tests prove the gate and the validation - they can
# never terminate a real process.
import psutil as _ps


class _FakeProc:
    """Stands in for a single OS process (never a real one)."""

    def __init__(self, name="chrome.exe", pid=1234, created=1111.0):
        self._name, self.pid, self._created = name, pid, created
        self.terminated = self.killed = False
        self.exists = True
        self.calls = []

    def name(self):
        return self._name

    def create_time(self):
        return self._created

    def is_running(self):
        return self.exists

    def terminate(self):
        self.calls.append("terminate")
        self.terminated = True
        self.exists = False                 # it exits at once

    def kill(self):
        self.calls.append("kill")
        self.killed = True
        self.exists = False


def _mk_close(mode="block_list", name="chrome.exe", pid=1234,
              created=1111.0, factory=None, ev=None):
    """Detector + fake process + a well-formed DETECTED event."""
    proc = _FakeProc(name, pid, created)
    det = _wa.WebAccessDetector(
        resolver=lambda d, u=None:
            {"bad.example": ["203.0.113.5"]}.get(d, set()),
        connections=lambda: [],
        process_factory=factory or (lambda _i, p=proc: p))
    det.set_policy({"version": 1, "mode": mode, "blocked": ["bad.example"],
                    "allowed": [], "upstream_dns": ""})
    det.stop()
    det.refresh()
    event = dict({"domain": "bad.example", "ip": "203.0.113.5", "pid": 55,
                  "process": name, "browser": name, "browser_pid": pid,
                  "browser_created": created, "status": "DETECTED",
                  "reason": "blocked", "policy_version": 1}, **(ev or {}))
    return det, proc, event


_d_alw, _p_alw, _ = _mk_close(mode="allow_all")
_d, _proc, _ev = _mk_close()

_r = _d.can_close(_ev)
check("[close] a fully identified browser passes the five conditions",
      _r == (True, ""), str(_r))
_r = _d_alw.can_close(_ev)
check("[close] condition 1: ALLOW ALL closes nothing",
      _r[0] is False and "ALLOW ALL" in _r[1], str(_r))
_r = _d.can_close(dict(_ev, status="UNRESOLVED"))
check("[close] condition 2: UNRESOLVED is never acted on",
      _r[0] is False and "UNRESOLVED" in _r[1], str(_r))
check("[close] condition 3: no domain, or ambiguous, means no close",
      _d.can_close(dict(_ev, domain="", reason="unnameable"))[0] is False
      and "ambiguous" in _d.can_close(
          dict(_ev, reason="ambiguous-domain"))[1],
      str(_d.can_close(dict(_ev, domain=""))))
_r = _d.can_close(dict(_ev, browser="", process="opencode-cli.exe"))
check("[close] condition 4: only chrome/edge/firefox is ever closed",
      _r[0] is False and "not chrome" in _r[1], str(_r))
_r = _d.can_close(dict(_ev, browser_pid=0))
check("[close] condition 5: an unidentified browser_pid never closes",
      _r[0] is False and "positively identified" in _r[1], str(_r))

_r = _d.close_browser(_ev)
check("[close] one validated PID is terminated exactly once",
      _r[0] is True and _proc.terminated is True
      and _proc.calls == ["terminate"] and _proc.killed is False,
      f"{_r},{_proc.calls}")
_r2 = _d.can_close(_ev)
check("[close] a closed browser goes on cooldown (never a kill loop)",
      _r2[0] is False and "cooldown" in _r2[1], str(_r2))
check("[close] the attempt lands in the audit trail",
      len(_d.kills) == 1 and _d.kills[0]["ok"] is True
      and _d.kills[0]["browser"] == "chrome.exe"
      and _d.kills[0]["domain"] == "bad.example", str(_d.kills))

_d2, _p2, _ev2 = _mk_close(name="notepad.exe", ev={"browser": "chrome.exe"})
_r = _d2.close_browser(_ev2)
check("[close] a PID that is no longer a browser is not terminated",
      _r[0] is False and "not a browser we may touch" in _r[1]
      and _p2.terminated is False, str(_r))


def _gone(_i):
    raise _ps.NoSuchProcess(99999)


_d3, _p3, _ev3 = _mk_close(factory=_gone)
_r = _d3.close_browser(_ev3)
check("[close] a vanished browser is reported, not retried forever",
      _r[0] is False and "already gone" in _r[1], str(_r))

_d4, _p4, _ev4 = _mk_close(created=1111.0, ev={"browser_created": 2222.0})
_r = _d4.close_browser(_ev4)
check("[close] a recycled PID is refused (create-time mismatch)",
      _r[0] is False and "no longer the browser" in _r[1]
      and _p4.terminated is False, str(_r))


def _denied(_i):
    raise _ps.AccessDenied(pid=7)


_d5, _p5, _ev5 = _mk_close(factory=_denied)
_r = _d5.close_browser(_ev5)
check("[close] missing rights are reported as FAILED, never as SYNCED",
      _r[0] is False and "Administrator" in _r[1], str(_r))

_d6, _p6, _ev6 = _mk_close(pid=os.getpid())
_r = _d6.close_browser(_ev6)
check("[close] the Client itself can never be terminated",
      _r[0] is False and "Client itself" in _r[1], str(_r))
_d_alw.stop()
_d.stop()
for _dX in (_d2, _d3, _d4, _d5, _d6):
    _dX.stop()

conn = database.get_connection()
conn.execute("DELETE FROM websites")
conn.commit()
conn.close()

# ---- Website Access (spec items 6/7/8): schema, matching, versioning ----
from protocol import build_web_policy_ack
import struct as _struct

conn = database.get_connection()
_wcols = [r["name"] for r in conn.execute(
    "PRAGMA table_info(websites)").fetchall()]
check("websites schema carries list_type/enabled/category/notes",
      {"list_type", "enabled", "category", "notes", "created_at"}
      <= set(_wcols), str(_wcols))
_pcols = [r["name"] for r in conn.execute(
    "PRAGMA table_info(web_pc_policy)").fetchall()]
check("web_pc_policy schema (mode/version/sync_status/enforced)",
      {"pc_name", "mode", "version", "sync_status", "enforced",
       "last_error", "updated_at"} <= set(_pcols), str(_pcols))
_ccols = [r["name"] for r in conn.execute(
    "PRAGMA table_info(computers)").fetchall()]
check("computers carries the minimal group tag", "group_name" in _ccols,
      str(_ccols))
_seeds = {r["key"]: r["value"] for r in conn.execute(
    "SELECT key, value FROM system_settings WHERE key IN "
    "('web_mode','web_policy_version','upstream_dns')").fetchall()}
check("settings seeds exist for mode/version/upstream DNS",
      _seeds.get("web_mode") == "allow_all"
      and "web_policy_version" in _seeds
      and _seeds.get("upstream_dns") == "1.1.1.1", str(_seeds))
conn.close()

# suffix matching (spec item 6): example.com blocks www./sub. but never
# notexample.com or example.com.evil.test
from dns_filter import decide, domain_matches
check("domain matching covers www and subdomains",
      domain_matches("example.com", "example.com")
      and domain_matches("www.example.com", "example.com")
      and domain_matches("deep.sub.example.com", "example.com"),
      "example.com rule")
check("domain matching never hits lookalike domains",
      not domain_matches("notexample.com", "example.com")
      and not domain_matches("example.com.evil.test", "example.com"),
      "lookalikes")
_pblk = {"mode": "block_list", "blocked": ["example.com", "spam.test"]}
check("decide blocks exact/suffix, forwards the rest (BLOCK LIST)",
      decide("example.com", _pblk) is False
      and decide("www.example.com", _pblk) is False
      and decide("a.b.spam.test", _pblk) is False
      and decide("notexample.com", _pblk) is True
      and decide("unlisted.org", _pblk) is True, str(_pblk))
check("decide forwards everything in ALLOW ALL",
      decide("anything.test", {"mode": "allow_all",
                               "blocked": ["example.com"]}) is True)
_palw = {"mode": "allow_only", "allowed": ["school.edu"]}
check("decide allows only listed domains (ALLOW ONLY)",
      decide("portal.school.edu", _palw) is True
      and decide("evil.test", _palw) is False, str(_palw))

# versioning + per-PC scope (spec items 7-8)
_v0 = srv.get_web_policy()["version"]
_v1 = srv.bump_web_policy_version()
_v2 = srv.bump_web_policy_version()
check("policy version bumps monotonically",
      _v1 == _v0 + 1 and _v2 == _v1 + 1, f"{_v0} -> {_v1} -> {_v2}")
_keep_alive()
_res = srv.set_web_mode("block_list", pc_names=["TEST-PC"],
                        admin_user="admin")
check("set_web_mode targets exactly the chosen PC",
      _res.get("success") is True and _res.get("targets") == 1
      and _res.get("version", 0) > _v2
      and _res.get("sent", 0) >= 1, str(_res))
check("per-PC override applies to that PC only",
      srv.get_web_policy("TEST-PC")["mode"] == "block_list"
      and srv.get_web_policy()["mode"] == "allow_all",
      str(srv.get_web_policy("TEST-PC")))
for _ in range(50):            # keep w's socket clean: the push above
    if w.recv_message(timeout=0.4) is None:   # reached TEST-PC
        break

# acks drive the status (spec item 8): FAILED, then SYNCED, stale ignored
def _wait_pc_status(pc, status, timeout=4.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = database.get_connection().execute(
            "SELECT sync_status FROM web_pc_policy WHERE pc_name=?",
            (pc,)).fetchone()
        if row and row["sync_status"] == status:
            return row
        time.sleep(0.15)
    return database.get_connection().execute(
        "SELECT sync_status FROM web_pc_policy WHERE pc_name=?",
        (pc,)).fetchone()

w.send_message(build_web_policy_ack(
    _res["version"], "block_list", False, "none",
    "ALLOW ONLY has no allowed domains - nothing can be enforced"))
_row = _wait_pc_status("TEST-PC", "FAILED")
check("a failed ack records FAILED with the client's error",
      _row is not None and _row["sync_status"] == "FAILED", str(_row))
_row = database.get_connection().execute(
    "SELECT last_error FROM web_pc_policy WHERE pc_name='TEST-PC'"
).fetchone()
check("FAILED keeps the client's reason",
      "ALLOW ONLY" in (_row["last_error"] or ""), str(dict(_row)))
w.send_message(build_web_policy_ack(
    _res["version"], "block_list", True, "detect", None))
_row = _wait_pc_status("TEST-PC", "SYNCED")
check("a success ack at the same version records SYNCED",
      _row is not None and _row["sync_status"] == "SYNCED", str(_row))
_row = database.get_connection().execute(
    "SELECT enforced FROM web_pc_policy WHERE pc_name='TEST-PC'"
).fetchone()
check("SYNCED remembers how it was enforced", _row["enforced"] == "detect",
      str(dict(_row)))
w.send_message(build_web_policy_ack(
    _res["version"] - 99, "block_list", False, "none", "stale"))
time.sleep(0.6)
_row = database.get_connection().execute(
    "SELECT sync_status, last_error FROM web_pc_policy "
    "WHERE pc_name='TEST-PC'").fetchone()
check("a stale ack for an older version is ignored",
      _row["sync_status"] == "SYNCED"
      and "stale" not in (_row["last_error"] or ""), str(dict(_row)))

# rule change -> new version pushed (the dashboard hook's server half)
conn = database.get_connection()
conn.execute("INSERT INTO websites (domain) VALUES ('x.test')")
conn.commit()
conn.close()
_keep_alive()                      # the sweep must not age TEST-PC out here
_v3 = srv.bump_web_policy_version()
_pushed = srv.push_web_filter("admin", version=_v3)
check("every rules change is pushed as a new version",
      _pushed.get("version") == _v3
      and _pushed.get("sent", 0) >= 1, str(_pushed))
for _ in range(50):            # keep w's socket clean for the reads below
    if w.recv_message(timeout=0.4) is None:
        break
_row = database.get_connection().execute(
    "SELECT sync_status FROM web_pc_policy WHERE pc_name='TEST-PC'"
).fetchone()
check("the new push re-enters SYNCING until the next ack",
      _row["sync_status"] == "SYNCING", str(dict(_row)))
w.send_message(build_web_policy_ack(_v3, "block_list", True, "detect", None))
_row = _wait_pc_status("TEST-PC", "SYNCED")
check("the client can re-ack the new version", _row is not None, str(_row))
conn = database.get_connection()
conn.execute("DELETE FROM websites")
conn.execute("UPDATE web_pc_policy SET mode='allow_all', sync_status='OFFLINE'")
conn.commit()
conn.close()

# ---- Website Access end-to-end: versioned snapshot -> real verdict -----
# P1.4 removed the local DNS proxy that used to answer on 127.0.0.1:53.
# What must never drift is the VERDICT: the detector has to decide the
# same way decide() does, for the very snapshot the server versions -
# otherwise the ack would say one thing and the events another.
import dns_filter as _df
from dns_filter import decide as _decide_e2e
_det_e2e = _wa.WebAccessDetector(
    resolver=lambda d, u=None: {"bad.example": ["203.0.113.9"]}.get(d, set()),
    connections=lambda: [])
_det_e2e.set_policy({"version": 6, "mode": "block_list",
                      "blocked": ["bad.example"], "allowed": []})
_det_e2e.stop()
_pol_e2e = _det_e2e.get_policy()
check("the detector keeps the versioned snapshot it was given",
      _pol_e2e.get("version") == 6
      and _pol_e2e.get("mode") == "block_list"
      and _pol_e2e.get("blocked") == ["bad.example"], str(_pol_e2e))
check("the detector and decide() agree on every domain",
      _det_e2e.violations("bad.example")[0] is True
      and _det_e2e.violations("sub.bad.example")[0] is True
      and _det_e2e.violations("notbad.example")[0] is False
      and _decide_e2e("bad.example", _pol_e2e) is False
      and _decide_e2e("sub.bad.example", _pol_e2e) is False
      and _decide_e2e("notbad.example", _pol_e2e) is True,
      str([_det_e2e.violations(d)[0]
           for d in ("bad.example", "sub.bad.example", "notbad.example")]))
_det_e2e.stop()

# ---- local event log sync + auth roster (spec items 9/11/12/15/17) ----
conn = database.get_connection()
_clcols = [r["name"] for r in conn.execute(
    "PRAGMA table_info(client_logs)").fetchall()]
check("client_logs keys on the client's event_id",
      "event_id" in _clcols
      and {"pc_name", "user_id", "severity", "category", "message",
           "created_at", "received_at"} <= set(_clcols), str(_clcols))
conn.close()
check("max_offline_days setting is seeded (default 7)",
      database.get_setting("max_offline_days", "") == "7",
      database.get_setting("max_offline_days", ""))

_ev1 = {"event_id": "evt-111", "created_at": "2026-01-01 10:00:00",
        "severity": "WARN", "category": "auth",
        "message": "Offline sign-in denied", "detail": "expired",
        "user_id": "2023-00001", "pc_name": "TEST-PC"}
_ev2 = {"event_id": "evt-222", "created_at": "2026-01-01 10:00:01",
        "severity": "INFO", "category": "net",
        "message": "Server connection established", "detail": "",
        "user_id": "", "pc_name": "TEST-PC"}
for _ in range(50):                        # keep w's socket in step
    if w.recv_message(timeout=0.4) is None:
        break
w.send_message(build_stu_request("log_sync",
                                 {"events": [_ev1, _ev2],
                                  "pc_name": "TEST-PC"}))
r = w.recv_message(timeout=4)
_d = r.payload.get("data") if r and r.payload.get("ok") else {}
check("log_sync stores the batch and acks every id",
      r is not None and r.payload.get("ok") is True
      and set(_d.get("accepted") or []) == {"evt-111", "evt-222"}
      and _d.get("stored") == 2, str(dict(r.payload) if r else None))
# the exact same batch again (lost ack + retry): deduped by event_id,
# still fully acknowledged so the client can safely mark it SYNCED
w.send_message(build_stu_request("log_sync",
                                 {"events": [_ev1, _ev2],
                                  "pc_name": "TEST-PC"}))
r = w.recv_message(timeout=4)
_d = r.payload.get("data") if r and r.payload.get("ok") else {}
check("resending the batch is deduped but fully acked",
      r is not None and r.payload.get("ok") is True
      and set(_d.get("accepted") or []) == {"evt-111", "evt-222"}
      and _d.get("stored") == 0 and _d.get("duplicates") == 2,
      str(dict(r.payload) if r else None))
_rows = database.get_connection().execute(
    "SELECT COUNT(*) AS n FROM client_logs "
    "WHERE event_id IN ('evt-111','evt-222')").fetchone()
check("event-id dedupe keeps exactly one row per event",
      _rows["n"] == 2, str(_rows["n"]))
_row = database.get_connection().execute(
    "SELECT pc_name, severity, received_at FROM client_logs "
    "WHERE event_id='evt-111'").fetchone()
check("stored log keeps client fields + server receive time",
      _row is not None and _row["pc_name"] == "TEST-PC"
      and _row["severity"] == "WARN" and bool(_row["received_at"]),
      str(dict(_row) if _row else None))

# auth roster for offline login (spec item 12): Active accounts only,
# salted hashes only, plus the expiry cap that travels with the roster
w.send_message(build_stu_request("auth_roster"))
r = w.recv_message(timeout=4)
_d = r.payload.get("data") if r and r.payload.get("ok") else {}
_users = _d.get("users") or []
_sids = [u.get("student_id") for u in _users]
check("auth roster serves every Active account",
      "admin" in _sids and "2023-00001" in _sids and "staff" in _sids,
      str(_sids))
check("auth roster carries salted hashes, never plaintext",
      len(_users) > 0
      and all("password_hash" in u and "password" not in u
              and "$" in (u.get("password_hash") or "") for u in _users),
      str(sorted(_users[0].keys()) if _users else None))
check("auth roster carries max_offline_days",
      str(_d.get("max_offline_days")) == "7",
      str(_d.get("max_offline_days")))
conn = database.get_connection()
conn.execute("UPDATE users SET status='Inactive' "
             "WHERE student_id='mig_test'")
conn.commit()
conn.close()
w.send_message(build_stu_request("auth_roster"))
r = w.recv_message(timeout=4)
_sids = [u.get("student_id")
         for u in ((r.payload.get("data") or {}).get("users") or [])]
check("disabled accounts drop out of the roster (revocation)",
      "mig_test" not in _sids, str(_sids))
conn = database.get_connection()
conn.execute("UPDATE users SET status='Active' "
             "WHERE student_id='mig_test'")
conn.commit()
conn.close()
conn = database.get_connection()
conn.execute("DELETE FROM client_logs WHERE event_id IN "
             "('evt-111','evt-222')")
conn.commit()
conn.close()

# ------------------------------------------------------- UDP LAN discovery
from protocol import discovery_udp_port
check("discovery UDP port is TCP+1 (8443 -> 8444)",
      discovery_udp_port(8443) == 8444, str(discovery_udp_port(8443)))
ip_found = client_mod.discover_server_ip(18443, timeout=2.0)
check("UDP discovery finds the running server",
      ip_found == "127.0.0.1", str(ip_found))
# garbage probes must not break the responder
gsock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
gsock.sendto(b"not-json-garbage",
             ("127.0.0.1", discovery_udp_port(18443)))
gsock.close()
ip_again = client_mod.discover_server_ip(18443, timeout=2.0)
check("UDP discovery ignores garbage probes",
      ip_again == "127.0.0.1", str(ip_again))
# nobody answers on an unused port -> caller falls back to saved server_ip
ip_none = client_mod.discover_server_ip(19999, timeout=0.6)
check("UDP discovery falls back when nobody answers",
      ip_none is None, str(ip_none))

# the client can never self-claim a role: the server always answers with
# the role stored in the central DB row (spec #10)
w.send_message(build_auth_request("2023-00001", "student123", "admin", {}))
r = w.recv_message(timeout=4)
check("client cannot self-claim a role",
      r and r.payload.get("success") is True
      and r.payload.get("user_data", {}).get("role") == "student",
      str(r and r.payload.get("user_data", {}).get("role")))

# Re-auth as admin BEFORE the client_loop reader thread starts (recv on this
# socket must never race the reader).  auth_user=admin is what the hardened
# session_start further down records as the session owner.
w.send_message(build_auth_request("admin", "admin123", "admin", {}))
r = w.recv_message(timeout=4)
check("admin re-auth before session start",
      r and r.payload.get("success") is True)

# ---------------------------------------------------------------- heartbeat
w.send_message(build_client_heartbeat("TEST-PC", "logged_in", 12.5, 44.0,
                                      logged_in_user="2023-00001",
                                      ip="127.0.0.1", hostname="testhost",
                                      conn_type="Wi-Fi"))
r = w.recv_message(timeout=4)
check("heartbeat is ACKed (PONG)", r and r.type == MessageType.PONG.value, str(r and r.type))
check("PONG carries admin state field",
      r and "admin_state" in r.payload)
time.sleep(0.3)
snap = next((c for c in srv.list_clients() if c["pc_name"] == "TEST-PC"), None)
check("heartbeat cpu/ram/status/user",
      snap and snap["cpu_percent"] == 12.5 and snap["ram_percent"] == 44.0
      and snap["status"] == "logged_in" and snap["logged_in_user"] == "2023-00001")
crow = database.get_connection().execute(
    "SELECT connection_type FROM computers WHERE pc_name='TEST-PC'").fetchone()
check("heartbeat conn_type persisted (Ethernet/Wi-Fi reporting)",
      crow and crow["connection_type"] == "Wi-Fi",
      str(crow and crow["connection_type"]))

# ---------------------------------------------------------------- stu queries
w.send_message(build_stu_request("announcements", {}))
r = w.recv_message(timeout=4)
check("stu_query announcements", r and r.payload.get("ok") and
      isinstance(r.payload.get("data"), list))
w.send_message(build_stu_request("pcs_available", {}))
r = w.recv_message(timeout=4)
check("stu_query pc list", r and r.payload.get("ok"))
w.send_message(build_stu_request("evil_sql", {}))
r = w.recv_message(timeout=4)
check("stu_query rejects unknown kind", r and r.payload.get("ok") is False)
w.send_message(build_stu_request("attendance_list", {"student_id": "2023-00001"}))
r = w.recv_message(timeout=4)
check("attendance feature removed server-side",
      r and r.payload.get("ok") is False, str(r and r.payload.get("error")))

# ---------------------------------------------------------------- commands
stop_reader = threading.Event()
def client_loop():
    while not stop_reader.is_set():
        msg = w.recv_message(timeout=0.3)
        if msg is None:
            try:
                w.sock.getpeername(); continue
            except Exception:
                break
        if msg.type == MessageType.CMD_SCREENSHOT.value:
            out = Message(type=MessageType.CMD_SCREENSHOT.value,
                          payload={"image": "ZmFrZQ==", "pc_name": "TEST-PC",
                                   "ref": msg.msg_id})
            out.msg_id = msg.msg_id
            w.send_message(out)
        elif msg.payload.get("command_id"):
            # ACK every command (never executes anything for real)
            w.send_message(build_command_response(msg.payload.get("command_id"), True))
threading.Thread(target=client_loop, daemon=True).start()
time.sleep(0.2)

res = srv.lock_client("TEST-PC", admin="tester", msg="hi")
check("lock command roundtrip + ack", res.get("success") is True, str(res))
row = database.get_connection().execute(
    "SELECT admin_state FROM computers WHERE pc_name='TEST-PC'").fetchone()
check("lock state persisted as desired state",
      row and row["admin_state"] and json.loads(row["admin_state"])["cmd"] == "lock",
      str(row and row["admin_state"]))
res = srv.unlock_client("TEST-PC", admin="tester")
check("unlock roundtrip + ack", res.get("success") is True, str(res))
time.sleep(0.3)
row = database.get_connection().execute(
    "SELECT admin_state FROM computers WHERE pc_name='TEST-PC'").fetchone()
check("unlock clears desired state", row and not row["admin_state"],
      str(row and row["admin_state"]))
res = srv.lock_client("GONE-PC", admin="tester")
check("offline target rejected", res.get("success") is False)
shot = srv.request_screenshot("TEST-PC", "tester")
check("screenshot roundtrip", shot.get("success") and shot["data"]["image"] == "ZmFrZQ==")
check("observe start works", srv.start_screen_observe(
    "TEST-PC", "t", role="admin") is True)
time.sleep(0.3)
srv.stop_screen_observe("TEST-PC", "t")
check("observe watchers cleared", "TEST-PC" not in srv.screen_watchers)
# P1-8: the observe audit row must be LINKED to the command it describes,
# so the acknowledgement the client already sends resolves it.  It used to
# be stamped "Done" on send - a confirmation nobody had given yet.
conn = database.get_connection()
row = conn.execute(
    "SELECT command_id, status, executed_at FROM client_commands "
    "WHERE command_type='cmd_screen_observe_start' "
    "ORDER BY id DESC LIMIT 1").fetchone()
conn.close()
check("observe audit row carries the id sent to the client",
      row is not None and len(str(row["command_id"] or "")) >= 8,
      str(dict(row) if row else None))
check("observe audit row is resolved by the real acknowledgement",
      row is not None and row["executed_at"] is not None,
      f"executed_at={row['executed_at'] if row else None}")

# --- Task 5: remote mouse/keyboard control -------------------------------
from protocol import sanitize_remote_events, build_remote_input
_wl = sanitize_remote_events([
    {"kind": "mouse", "action": "move", "x": 1.7, "y": -3},
    {"kind": "mouse", "action": "click", "x": 0.5, "y": 0.5},   # not a verb
    {"kind": "key", "action": "down", "keysym": "a", "char": "a"},
    {"kind": "key", "action": "run", "keysym": "a"},            # not allowed
    {"kind": "exec", "action": "down", "keysym": "rm"},         # not allowed
    {"kind": "key", "action": "down", "keysym": ""},
    "not-a-dict",
    {"kind": "mouse", "action": "down", "x": 0.2, "y": 0.2, "button": 9},
])
check("remote whitelist keeps only mouse move/click + key down/up",
      len(_wl) == 3, str(_wl))
check("remote whitelist clamps coordinates into the frame",
      _wl[0]["x"] == 1.0 and _wl[0]["y"] == 0.0, str(_wl[0]))
check("remote whitelist repairs an unknown mouse button",
      _wl[2]["button"] == 1, str(_wl[2]))
check("remote whitelist has no text/shell/exec primitive at all",
      not any(k in str(_wl) for k in ("run", "exec", "shell")))
check("remote whitelist caps one batch",
      len(sanitize_remote_events(
          [{"kind": "key", "action": "down", "keysym": "a"}] * 500)) == 64)
_b = build_remote_input("sess1", [{"kind": "mouse", "action": "move",
                                   "x": 0.5, "y": 0.5}])
check("remote input message carries the session id it belongs to",
      _b.type == MessageType.CMD_REMOTE_INPUT.value
      and _b.payload.get("session_id") == "sess1"
      and len(_b.payload.get("events") or []) == 1, str(_b.payload))

check("remote start rejects an offline target",
      srv.start_remote_control("GONE-PC", "tester",
                               role="admin").get("success") is False)
conn = database.get_connection()
_off = conn.execute(
    "SELECT status, executed_at FROM client_commands "
    "WHERE command_type='cmd_remote_start' AND target_pc='GONE-PC' "
    "ORDER BY id DESC LIMIT 1").fetchone()
conn.close()
check("failed remote start is still audited with a failure reason",
      _off is not None and _off["status"] == "Failed"
      and _off["executed_at"] is not None, str(dict(_off) if _off else None))

_r = srv.start_remote_control("TEST-PC", "tester", role="admin")
check("remote start works for an online PC", _r.get("success") is True, str(_r))
check("remote session is registered (one per PC)",
      "TEST-PC" in srv.remote_sessions
      and srv.remote_sessions["TEST-PC"]["session_id"] == _r.get("session_id"),
      str(srv.remote_sessions))
check("remote start opens the live screen stream",
      "TEST-PC" in srv.screen_watchers, str(list(srv.screen_watchers)))
_r2 = srv.start_remote_control("TEST-PC", "tester2", role="admin")
check("a second remote session on the same PC is refused",
      _r2.get("already") is True, str(_r2))
check("remote input is forwarded only to an active session",
      srv.forward_remote_input("TEST-PC", [
          {"kind": "mouse", "action": "move", "x": 0.5, "y": 0.5}],
          role="admin") is True
      and srv.forward_remote_input("NO-SESSION-PC", [
          {"kind": "mouse", "action": "move", "x": 0.5, "y": 0.5}],
          role="admin") is False)
check("remote input refuses a batch the whitelist removes",
      srv.forward_remote_input("TEST-PC", [
          {"kind": "exec", "action": "run", "keysym": "rm -rf"}],
          role="admin") is False)
check("remote input echoes the audited session id",
      build_remote_input(srv.remote_sessions["TEST-PC"]["session_id"],
                         [{"kind": "key", "action": "down",
                           "keysym": "Return"}]
                         ).payload["session_id"]
      == _r["session_id"])
time.sleep(0.3)
conn = database.get_connection()
_rstart = conn.execute(
    "SELECT command_id, status, executed_at, admin_user FROM client_commands "
    "WHERE command_type='cmd_remote_start' AND target_pc='TEST-PC' "
    "ORDER BY id DESC LIMIT 1").fetchone()
conn.close()
check("remote start audit row is written and acknowledged",
      _rstart is not None and len(str(_rstart["command_id"])) >= 8
      and _rstart["executed_at"] is not None
      and _rstart["admin_user"] == "tester",
      str(dict(_rstart) if _rstart else None))
conn = database.get_connection()
_ract = conn.execute(
    "SELECT action, details FROM admin_activity_log WHERE action LIKE "
    "'remote_control%' ORDER BY id DESC LIMIT 4").fetchall()
conn.close()
check("remote start lands in the audit trail with its session id",
      any(r["action"] == "remote_control_start" and
          _r["session_id"] in str(r["details"]) for r in _ract),
      str([dict(r) for r in _ract]))
check("the audit row names the client user, connection type and admin",
      any(r["action"] == "remote_control_start"
          and "type=LAN" in str(r["details"])
          and "client_user=" in str(r["details"])
          and "admin=tester" in str(r["details"]) for r in _ract),
      str([dict(r) for r in _ract]))

_s = srv.stop_remote_control("TEST-PC", "tester", reason="test stop")
time.sleep(0.3)
check("remote stop ends the session", _s.get("success") is True
      and "TEST-PC" not in srv.remote_sessions, str(_s))
check("remote stop tears the stream down", "TEST-PC" not in srv.screen_watchers)
conn = database.get_connection()
_rstop = conn.execute(
    "SELECT status, executed_at, params FROM client_commands "
    "WHERE command_type='cmd_remote_stop' AND target_pc='TEST-PC' "
    "ORDER BY id DESC LIMIT 1").fetchone()
_ract2 = conn.execute(
    "SELECT action, details FROM admin_activity_log "
    "WHERE action='remote_control_stop' ORDER BY id DESC LIMIT 1").fetchone()
conn.close()
check("remote stop audit row records start+end for the same session",
      _rstop is not None and _rstop["executed_at"] is not None
      and _r["session_id"] in str(_rstop["params"])
      and _ract2 is not None, str(dict(_rstop) if _rstop else None))

# a drop must end (and audit) the session even though the Client is gone
_r3 = srv.start_remote_control("TEST-PC", "tester", role="admin")
check("remote restart after a clean stop works", _r3.get("success") is True)
check("force-end on drop clears the session",
      srv.end_remote_control_on_drop("TEST-PC") is True
      and "TEST-PC" not in srv.remote_sessions)
conn = database.get_connection()
_rdrop = conn.execute(
    "SELECT status, result FROM client_commands "
    "WHERE command_type='cmd_remote_stop' AND target_pc='TEST-PC' "
    "ORDER BY id DESC LIMIT 1").fetchone()
conn.close()
check("the end audit is written even though the Client could never ack",
      _rdrop is not None and _rdrop["status"] == "Failed"
      and "disconnect" in str(_rdrop["result"]),
      str(dict(_rdrop) if _rdrop else None))
check("an already-ended session force-ends cleanly (no double row)",
      srv.end_remote_control_on_drop("TEST-PC") is False)
check("input can never be forwarded into an ended session",
      srv.forward_remote_input("TEST-PC", [
          {"kind": "mouse", "action": "move", "x": 0.5, "y": 0.5}],
          role="admin") is False)
srv.stop_screen_observe("TEST-PC", "tester")
import admin_dashboard as _adm
check("remote audit rows have a human-readable Action label",
      _adm.audit_action_label("remote_control_start") == "Remote Control Start"
      and _adm.audit_action_label("remote_control_stop") == "Remote Control Stop",
      str(_adm.audit_action_label("remote_control_start")))
check("remote audit rows are categorised as Monitoring",
      _adm.audit_action_category("remote_control_start") == "Monitoring"
      and _adm.audit_action_category("remote_control_stop") == "Monitoring",
      str(_adm.audit_action_category("remote_control_start")))

# --- P4: Observe + Remote Control are ADMINISTRATOR-only AT THE SERVER ----
# The dashboard simply does not offer the two controls to anybody else;
# this is what actually refuses them, so reaching these methods by any
# other route still changes nothing.
check("P4: an observe stream refuses a non-administrator",
      srv.start_screen_observe("TEST-PC", "staff1",
                               role="staff") is False)
check("P4: a refused observe opens no stream",
      "TEST-PC" not in srv.screen_watchers, str(list(srv.screen_watchers)))
_rr = srv.start_remote_control("TEST-PC", "staff1", role="staff")
check("P4: remote start refuses a non-administrator",
      _rr.get("success") is False
      and "ADMINISTRATOR" in str(_rr.get("error") or ""), str(_rr))
_rr2 = srv.start_remote_control("TEST-PC", "maint1", role="maintenance")
_rr3 = srv.start_remote_control("TEST-PC", "nobody", role="")
check("P4: MAINTENANCE and an absent role are refused too (fail closed)",
      _rr2.get("success") is False and _rr3.get("success") is False,
      f"{_rr2.get('error')},{_rr3.get('error')}")
check("P4: no refused attempt created a session or a stream",
      "TEST-PC" not in srv.remote_sessions
      and "TEST-PC" not in srv.screen_watchers,
      f"{list(srv.remote_sessions)},{list(srv.screen_watchers)}")

# a refused takeover is a security event, so it is kept either way
conn = database.get_connection()
_p4obs = conn.execute(
    "SELECT details FROM admin_activity_log "
    "WHERE action='screen_observe_start' AND details LIKE 'REJECTED%' "
    "ORDER BY id DESC LIMIT 1").fetchone()
_p4rem = conn.execute(
    "SELECT details FROM admin_activity_log "
    "WHERE action='remote_control_start' AND details LIKE 'REJECTED%' "
    "ORDER BY id DESC LIMIT 1").fetchone()
_p4cmd = conn.execute(
    "SELECT status, result, executed_at FROM client_commands "
    "WHERE command_type='cmd_remote_start' "
    "AND result LIKE '%administrator role required%' "
    "ORDER BY id DESC LIMIT 1").fetchone()
conn.close()
check("P4: a refused observe and a refused start are both audited",
      _p4obs is not None and "staff" in str(_p4obs["details"])
      and _p4rem is not None, f"{_p4obs and dict(_p4obs)},"
      f"{_p4rem and dict(_p4rem)}")
check("P4: a refused start gets the same command row as any other start",
      _p4cmd is not None and _p4cmd["status"] == "Failed"
      and _p4cmd["executed_at"] is not None,
      str(dict(_p4cmd) if _p4cmd else None))

# on a LIVE session the ADMINISTRATOR still drives it, and nobody else
_r4 = srv.start_remote_control("TEST-PC", "tester", role="admin")
check("P4: the ADMINISTRATOR still starts a session", _r4.get("success") is True,
      str(_r4))
_mv = [{"kind": "mouse", "action": "move", "x": 0.5, "y": 0.5}]
check("P4: a non-administrator's input batch drives nothing",
      srv.forward_remote_input("TEST-PC", _mv, admin="staff1",
                               role="staff") is False
      and srv.forward_remote_input("TEST-PC", _mv, admin="staff1",
                                   role="staff") is False)
conn = database.get_connection()
_p4n = conn.execute(
    "SELECT COUNT(*) AS c FROM admin_activity_log "
    "WHERE action='remote_input_reject'").fetchone()["c"]
conn.close()
check("P4: repeated refusals are audited once, not once per movement",
      _p4n == 1, str(_p4n))
check("P4: the ADMINISTRATOR's own batch still goes through",
      srv.forward_remote_input("TEST-PC", _mv, admin="tester",
                               role="admin") is True)
srv.stop_remote_control("TEST-PC", "tester", reason="P4 test end")
srv.stop_screen_observe("TEST-PC", "tester")
check("P4: the P4 test session is closed again",
      "TEST-PC" not in srv.remote_sessions
      and "TEST-PC" not in srv.screen_watchers, str(list(srv.remote_sessions)))

# --- connection resilience: a send must not inherit the reader's poll timeout
# recv_message() parks the SHARED socket in a short timeout (0.5 s in the
# live loops), and since Python 3.5 that timeout is the MAX TOTAL duration
# of sendall() - so a large screen frame on a momentarily congested link
# would raise socket.timeout mid-send and be misread as a dead server,
# dropping the connection (and the active remote session) for no reason.
# send_message() must send BLOCKING and restore the reader's timeout.
import socket as _sockmod
_pa, _pb = _sockmod.socketpair()
_wx = TLSSocketWrapper(_pa)
_wx.recv_message(timeout=0.1)                # idle -> None; parks at 0.1 s
class _SendSpy:
    """Stand-in socket that records the timeout in effect during sendall."""
    def __init__(self, real):
        self._real = real
        self.seen = []
    def sendall(self, data):
        self.seen.append(self._real.gettimeout())
        return self._real.sendall(data)
    def gettimeout(self):
        return self._real.gettimeout()
    def settimeout(self, value):
        return self._real.settimeout(value)
    def close(self):
        return self._real.close()
_spy = _SendSpy(_pa)
_wx.sock = _spy
_ok_send = _wx.send_message(Message.create(MessageType.CMD_SCREEN_OBSERVE_STOP, {}))
_wx.sock = _pa
check("[conn] send_message reports success on a healthy socket", _ok_send is True)
check("[conn] the send itself runs on a BLOCKING socket (no recv timeout)",
      _spy.seen == [None], str(_spy.seen))
check("[conn] the reader's timeout is restored after the send",
      _pa.gettimeout() is not None and abs(_pa.gettimeout() - 0.1) < 1e-9,
      str(_pa.gettimeout()))
_pb.settimeout(2.0)
try:
    _got = len(_pb.recv(65536)) > 0
except OSError:
    _got = False
check("[conn] the frame reaches the peer", _got)
_wx.close(); _pb.close()

# ---------------------------------------------------------------- sessions
def attendance_today():
    c = database.get_connection()
    n = c.execute("SELECT COUNT(*) c FROM attendance WHERE date=?",
                  (_today(),)).fetchone()["c"]
    c.close()
    return n
att_before = attendance_today()

# a stale open session on this PC must be healed by the next login
conn = database.get_connection()
conn.execute("DELETE FROM client_sessions WHERE session_id='sessSTALE'")
conn.execute(
    "INSERT INTO client_sessions "
    "(session_id, pc_name, student_id, full_name, login_time, status) "
    "VALUES ('sessSTALE', 'TEST-PC', '2023-00001', 'Juan', ?, 'Active')",
    (now_datetime(),))
conn.commit()
conn.close()

# NOTE: the admin re-auth happens EARLIER (right after the role-claim check,
# before client_loop starts) - TLSSocketWrapper.recv_message is not thread-
# safe, so a second reader on this socket would corrupt the frame buffer.
# With auth_user=admin the hardened session_start below records the ADMIN
# account (identity + role come from the central DB row, never the payload),
# so its session_end audits client_admin_logout.
w.send_message(build_session_start("TEST-PC", "2023-00001", "Juan Dela Cruz", "sess999"))
time.sleep(0.4)
conn = database.get_connection()
row = conn.execute("SELECT * FROM client_sessions WHERE session_id='sess999'").fetchone()
check("session recorded active", row and row["status"] == "Active")
# redesign (Phase B): the PC's reported medium is captured once, at start
_cols = [r[1] for r in conn.execute(
    "PRAGMA table_info(client_sessions)").fetchall()]
check("client_sessions has the connection_type column",
      "connection_type" in _cols, str(_cols))
_medium = conn.execute("SELECT connection_type FROM computers "
                       "WHERE pc_name='TEST-PC'").fetchone()
check("session start captures the PC's connection type",
      row and row["connection_type"] == (_medium[0] if _medium else ""),
      f"session={row['connection_type'] if row else None} "
      f"pc={_medium[0] if _medium else None}")
check("session sets computer In Use",
      conn.execute("SELECT status FROM computers WHERE pc_name='TEST-PC'").fetchone()["status"]
      == "In Use")
row_stale = conn.execute(
    "SELECT status, logout_time FROM client_sessions "
    "WHERE session_id='sessSTALE'").fetchone()
check("session start heals stale sessions on the same PC/user",
      row_stale and row_stale["logout_time"] and row_stale["status"] == "Disconnected",
      str(dict(row_stale) if row_stale else None))
check("no attendance time-in created (feature removed)",
      attendance_today() == att_before)
w.send_message(build_session_end("sess999"))
time.sleep(0.4)
row = conn.execute("SELECT * FROM client_sessions WHERE session_id='sess999'").fetchone()
check("session closed with duration",
      row["logout_time"] and row["duration_seconds"] is not None)
check("no attendance time-out created (feature removed)",
      attendance_today() == att_before)
n = conn.execute("SELECT COUNT(*) c FROM client_commands").fetchone()["c"]
check("command audit rows", n >= 3, f"count={n}")
n = conn.execute("SELECT COUNT(*) c FROM admin_activity_log").fetchone()["c"]
check("activity log has entries", n >= 1, f"count={n}")
# the audit row is written by the server thread right after the session
# commit (separate connection) - poll briefly instead of racing a fixed sleep
c = 0
for _ in range(20):
    c = conn.execute("SELECT COUNT(*) c FROM admin_activity_log "
                     "WHERE action='client_admin_logout'").fetchone()["c"]
    if c >= 1:
        break
    time.sleep(0.1)
check("client-admin logout audited", c >= 1, f"count={c}")
conn.close()

# ------------------------------------------------ desired state on reconnect
# make sure a lock is outstanding before the (re)connection
srv.lock_client("TEST-PC", admin="tester", msg="reconnect")
time.sleep(0.3)
w3raw = socket.create_connection(("127.0.0.1", 18443), timeout=5)
w3tls = create_ssl_context(is_server=False).wrap_socket(w3raw, server_hostname="127.0.0.1")
w3 = TLSSocketWrapper(w3tls)
w3.send_message(build_client_register("TEST-PC", "127.0.0.1", "testhost"))
r = w3.recv_message(timeout=4)
check("re-register re-applies admin lock",
      r and r.type == MessageType.CMD_LOCK.value, str(r and r.type))

seen3 = []
stop3 = threading.Event()
def ack_loop3():
    while not stop3.is_set():
        msg = w3.recv_message(timeout=0.3)
        if msg is None:
            try:
                w3.sock.getpeername(); continue
            except Exception:
                break
        if msg.type == MessageType.PONG.value:
            continue
        seen3.append(msg.type)
        if msg.payload.get("command_id"):
            w3.send_message(build_command_response(msg.payload.get("command_id"), True))
threading.Thread(target=ack_loop3, daemon=True).start()

# an open session on this entry must be closed when the server stops
w3.send_message(build_session_start("TEST-PC", "ghost42", "Ghost User", "sessSTOP"))
time.sleep(0.4)
row = database.get_connection().execute(
    "SELECT status FROM client_sessions WHERE session_id='sessSTOP'").fetchone()
check("session active before server stop",
      row and row["status"] == "Active", str(dict(row) if row else None))

# pause persists until an explicit resume (never auto-expires)
res = srv.pause_client("TEST-PC", admin="tester", seconds=0, msg="brb")
check("pause command acked", res.get("success") is True, str(res))
time.sleep(0.3)
row = database.get_connection().execute(
    "SELECT admin_state FROM computers WHERE pc_name='TEST-PC'").fetchone()
check("pause state persisted", row and json.loads(row["admin_state"])["cmd"] == "pause",
      str(row and row["admin_state"]))
# a heartbeat whose status does not match makes the server re-send the pause
ent = srv.get_client("TEST-PC")
if ent:
    ent._last_resync = 0            # bypass the flood-throttle for this check
w3.send_message(build_client_heartbeat("TEST-PC", "logged_in", 5, 9,
                                       ip="127.0.0.1", hostname="testhost"))
time.sleep(1.2)
check("mismatched heartbeat re-sends pause",
      MessageType.CMD_PAUSE.value in seen3, str(seen3))
row = database.get_connection().execute(
    "SELECT admin_state FROM computers WHERE pc_name='TEST-PC'").fetchone()
check("pause still active (no auto resume)",
      row and json.loads(row["admin_state"])["cmd"] == "pause")
res = srv.resume_client("TEST-PC", admin="tester")
check("resume acked", res.get("success") is True, str(res))
time.sleep(0.3)
row = database.get_connection().execute(
    "SELECT admin_state FROM computers WHERE pc_name='TEST-PC'").fetchone()
check("resume clears pause state", row and not row["admin_state"],
      str(row and row["admin_state"]))

# ------------------------------------- Send File: admin push to a PC Desktop
import base64 as _b64
from protocol import DENY_FILE_EXTS, MAX_PUSH_FILE_BYTES
from admin_dashboard import audit_action_category, audit_action_label
check("protocol has CMD_SEND_FILE wired into bulk actions",
      MessageType.CMD_SEND_FILE.value == "cmd_send_file"
      and srv.BULK_ACTIONS.get("send_file") == MessageType.CMD_SEND_FILE
      and all(e in DENY_FILE_EXTS for e in (".exe", ".bat", ".ps1", ".lnk")),
      str(srv.BULK_ACTIONS.get("send_file")))
# the client handler writes into a patched Desktop dir - the real one is
# never touched (module patch; nothing else in this run calls desktop_dir)
_sf_desk = tempfile.mkdtemp(prefix="sendfile_")
client_mod.desktop_dir = lambda: _sf_desk
_sf_hello = _b64.b64encode(b"lab handout").decode("ascii")
_ok, _det = client_mod.ClientApp._save_desktop_file(
    None, {"filename": "..\\evil.txt", "data": _sf_hello})
check("send file lands on the Desktop with a sanitized name",
      _ok and open(os.path.join(_sf_desk, "evil.txt"), "rb").read()
      == b"lab handout", str((_ok, _det)))
_ok, _det = client_mod.ClientApp._save_desktop_file(
    None, {"filename": "evil.txt", "data": _sf_hello})
check("send file renames on a collision",
      _ok and os.path.basename(str(_det)) == "evil (1).txt", str(_det))
_ok, _det = client_mod.ClientApp._save_desktop_file(
    None, {"filename": "payload.exe", "data": _sf_hello})
check("client refuses executable payloads",
      not _ok and "Executable" in str(_det)
      and not os.path.exists(os.path.join(_sf_desk, "payload.exe")),
      str((_ok, _det)))
# server-side validation refuses BEFORE the wire
with open(os.path.join(_sf_desk, "payload.exe"), "wb") as fh:
    fh.write(b"MZ")
res = srv.push_desktop_file(["TEST-PC"], os.path.join(_sf_desk, "payload.exe"),
                            admin="tester", role="admin")
check("server refuses executable files",
      len(res) == 1 and all(
          not r.get("success") and "Executable" in str(r.get("error"))
          for r in res.values()), str(res))
with open(os.path.join(_sf_desk, "big.pdf"), "wb") as fh:
    fh.write(b"\0" * (MAX_PUSH_FILE_BYTES + 1))
res = srv.push_desktop_file(["TEST-PC"], os.path.join(_sf_desk, "big.pdf"),
                            admin="tester", role="admin")
check("server refuses oversized files",
      all(not r.get("success") and "too large" in str(r.get("error"))
          for r in res.values()),
      str({k: v.get("error") for k, v in res.items()}))
with open(os.path.join(_sf_desk, "notes.txt"), "wb") as fh:
    fh.write(b"note")
res = srv.push_desktop_file(["TEST-PC"], os.path.join(_sf_desk, "notes.txt"),
                            admin="tester", role="staff")
check("non-admin send refused at the server",
      all(not r.get("success") and "ADMINISTRATOR" in str(r.get("error"))
          for r in res.values()), str(res))
# live wire: TEST-PC acks the command (SUCCESS), GONE-PC never gets one
res = srv.push_desktop_file(["TEST-PC", "GONE-PC"],
                            os.path.join(_sf_desk, "notes.txt"),
                            admin="tester", role="admin")
check("bulk push reports one honest result per PC",
      res.get("TEST-PC", {}).get("success") is True
      and res.get("TEST-PC", {}).get("result") == "SUCCESS"
      and res.get("GONE-PC", {}).get("result") == "OFFLINE", str(res))
conn = database.get_connection()
row = conn.execute("SELECT params FROM client_commands "
                   "WHERE command_type='cmd_send_file' "
                   "ORDER BY id DESC LIMIT 1").fetchone()
n_bulk = conn.execute("SELECT COUNT(*) c FROM admin_activity_log "
                      "WHERE action='BULK_SEND_FILE'").fetchone()["c"]
n_refuse = conn.execute("SELECT COUNT(*) c FROM admin_activity_log "
                        "WHERE action='send_file_refuse'").fetchone()["c"]
n_cmd = conn.execute("SELECT COUNT(*) c FROM admin_activity_log "
                     "WHERE action='command:cmd_send_file'").fetchone()["c"]
conn.close()
check("send-file audits are summaries (no payload bytes) and categorized",
      row and len(row["params"]) < 500 and "notes.txt" in row["params"]
      and n_bulk >= 1 and n_refuse >= 1 and n_cmd >= 1
      and audit_action_category("BULK_SEND_FILE") == "Admin Commands"
      and audit_action_category("command:cmd_send_file") == "Admin Commands"
      and audit_action_category("send_file_refuse") == "Security"
      and audit_action_label("send_file_refuse") == "Send File Refused",
      f"params_len={row and len(row['params'])} BULK={n_bulk} "
      f"refuse={n_refuse} cmd={n_cmd}")

# shutdown audit: command + explicit result in the activity log
res = srv.shutdown_client("TEST-PC", admin="boss")
check("shutdown command acked", res.get("success") is True, str(res))
time.sleep(0.5)
conn = database.get_connection()
row = conn.execute("SELECT status, result FROM client_commands "
                   "WHERE command_type='cmd_shutdown' ORDER BY id DESC LIMIT 1").fetchone()
check("shutdown result recorded", row and row["status"] == "Done", str(dict(row) if row else None))
c = conn.execute("SELECT COUNT(*) c FROM admin_activity_log "
                 "WHERE action='cmd_shutdown'").fetchone()["c"]
check("shutdown in activity audit", c >= 1, f"count={c}")
c = conn.execute("SELECT COUNT(*) c FROM admin_activity_log "
                 "WHERE action='command:cmd_shutdown'").fetchone()["c"]
check("shutdown command send audited", c >= 1, f"count={c}")

# force logout on account change (Phase I)
conn.execute(
    "INSERT OR REPLACE INTO client_sessions "
    "(session_id, pc_name, student_id, full_name, login_time, status) "
    "VALUES ('sessFL', 'TEST-PC', '2023-00001', 'Juan', ?, 'Active')",
    (now_datetime(),))
conn.commit()
conn.close()
res = srv.force_logout_user("2023-00001", admin="tester", reason="account update")
check("force logout closes active session", res.get("forced") == 1, str(res))
row = database.get_connection().execute(
    "SELECT status, logout_time FROM client_sessions WHERE session_id='sessFL'").fetchone()
check("forced session marked",
      row and row["logout_time"] and row["status"] == "Forced Logout", str(dict(row) if row else None))

# ------------------------------------------- server-authoritative status engine
from server import derive_status
from types import SimpleNamespace as NS

def _mk(**kw):
    base = dict(pc_name="X", is_online=True, verifying=False, desired=None,
                status="idle", logged_in_user=None)
    base.update(kw)
    return NS(**base)

check("priority OFFLINE beats everything",
      derive_status(_mk(is_online=False, verifying=True,
                        desired={"cmd": "lock"}, status="logged_in",
                        logged_in_user="u")) == "offline")
check("priority VERIFYING beats LOCKED",
      derive_status(_mk(verifying=True, desired={"cmd": "lock"})) == "verifying")
check("LOCKED from an admin lock",
      derive_status(_mk(desired={"cmd": "lock"}, status="logged_in",
                        logged_in_user="u")) == "locked")
check("PAUSED beats IN USE",
      derive_status(_mk(desired={"cmd": "pause"}, status="logged_in",
                        logged_in_user="u")) == "paused")
check("IN USE from a live session",
      derive_status(_mk(status="logged_in", logged_in_user="u")) == "in_use")
check("AVAILABLE at the kiosk login screen",
      derive_status(_mk(status="locked")) == "available")
check("ONLINE fallback while connected",
      derive_status(_mk(status="weird")) == "online")
check("UNKNOWN when nothing is known", derive_status(None, None) == "unknown")
check("never-connected DB row -> UNKNOWN",
      derive_status(None, {"is_online": 0, "last_heartbeat": None}) == "unknown")
check("ever-connected DB row -> OFFLINE",
      derive_status(None, {"is_online": 0,
                           "last_heartbeat": "2026-01-01 00:00:00"}) == "offline")

# ------------------------------------- no fake pre-seeded offline PCs in the grid
conn = database.get_connection()
conn.execute("DELETE FROM computers WHERE pc_name IN ('SEED-PC','MANUAL-PC')")
conn.execute("INSERT INTO computers (pc_name, specs, location, status) "
             "VALUES ('SEED-PC','Intel i5 / 8GB RAM / 256GB SSD','Lab 1',"
             "'Available')")
conn.execute("INSERT INTO computers (pc_name, location, status) "
             "VALUES ('MANUAL-PC','Lab 1','Available')")
conn.commit()
conn.close()
names = {s["pc_name"] for s in srv.list_pc_states()}
check("grid lists the PC that really connected", "TEST-PC" in names,
      str(sorted(names)))
check("grid never pre-seeds offline PCs",
      "SEED-PC" not in names and "MANUAL-PC" not in names, str(sorted(names)))
st_by = {s["pc_name"]: s for s in srv.list_pc_states()}
check("list_pc_states carries the server state field",
      st_by.get("TEST-PC", {}).get("state") in
      ("online", "offline", "in_use", "paused", "locked", "available",
       "verifying", "unknown"),
      str(st_by.get("TEST-PC", {}).get("state")))

# ------------------------------------- bulk commands with honest per-PC results
res = srv.bulk_command("pause", ["TEST-PC", "GONE-PC"], admin_user="tester",
                       params={"seconds": 0, "message": "bulk pause"})
check("bulk returns a result per PC",
      set(res) == {"TEST-PC", "GONE-PC"}, str(res))
check("bulk SUCCESS only after the client acks",
      res["TEST-PC"]["result"] == "SUCCESS", str(res["TEST-PC"]))
check("bulk reports OFFLINE honestly",
      res["GONE-PC"]["result"] == "OFFLINE", str(res["GONE-PC"]))
res = srv.bulk_command("resume", ["TEST-PC"], admin_user="tester")
check("bulk resume acked", res["TEST-PC"]["result"] == "SUCCESS", str(res))
conn = database.get_connection()
c = conn.execute("SELECT COUNT(*) c FROM admin_activity_log "
                 "WHERE action='BULK_PAUSE'").fetchone()["c"]
check("bulk audited per PC (BULK_PAUSE)", c >= 2, f"count={c}")
c = conn.execute("SELECT COUNT(*) c FROM admin_activity_log "
                 "WHERE action='client_admin_login'").fetchone()["c"]
check("client-admin login audited", c >= 1, f"count={c}")
conn.close()

# ------------------------------------- status_changed pushed on transitions
while not events.empty():
    events.get()
srv.lock_client("TEST-PC", admin="tester", msg="state-event")
time.sleep(0.5)
evs = []
while not events.empty():
    evs.append(events.get())
check("status_changed pushed on transitions",
      any(k == "status_changed" and (d or {}).get("state") == "locked"
          for k, d in evs),
      str([(k, (d or {}).get("state")) for k, d in evs
           if isinstance(d, dict)][:8]))
srv.unlock_client("TEST-PC", admin="tester")
time.sleep(0.3)

# ---------------------------------------------------------------- settings
check("system settings seeded",
      database.get_setting("server_port", "") == "8443")
check("system settings default fallback",
      database.get_setting("no_such_key", "d") == "d")

# ---------------------------------------------------------------- UI build
import tkinter as tk
from admin_dashboard import AdminDashboard
from student_dashboard import StudentDashboard, g as row_g
from crud_frame import UserCRUDFrame, AccountsFrame, StaffAccountsFrame

root = tk.Tk(); root.withdraw()
logout_called = []
dash = AdminDashboard(root, {"id": 1, "student_id": "admin",
                             "full_name": "Test Admin", "role": "admin"},
                      lambda: logout_called.append(1), server=srv, server_events=events)
root.update()
check("admin dashboard built with sidebar",
      "overview" in dash.pages and "computers" in dash.pages)
check("sidebar replaces notebook tabs", not hasattr(dash, "notebook"),
      str(getattr(dash, "notebook", None)))
check("Client PCs page present", "clients" in dash._sidebar_btns)
check("sidebar menu has >= 12 entries", len(dash._sidebar_btns) >= 12,
      str(len(dash._sidebar_btns)))
# --- Remote viewer coordinate mapping (cursor-accuracy regression) --------
# CTkLabel.bind() delivers events from the label's INTERNAL tk widgets (the
# image-sized label under the pointer, or the full-size canvas in the
# margins), so event.x/y live in two different spaces.  _remote_norm must
# normalise the ABSOLUTE pointer position against the photo's origin: a
# synthetic pointer at a known point of a photo smaller than the label has
# to come back as exactly that point - the old outer-label formula shifted
# it by the centring offset (measured -9 % / -13 % on a 1440x810 frame).
import types as _types
from PIL import Image as _PILImage, ImageTk as _PILImageTk
_rw = _ctk.CTkToplevel(root)
_rl = _ctk.CTkLabel(_rw, fg_color="#0d1117", text="Waiting for frames...")
_rl.pack(fill="both", expand=True, padx=8, pady=8)
_rw.label = _rl                                  # what _remote_norm reads
_rp = _PILImageTk.PhotoImage(_PILImage.new("RGB", (320, 180), (30, 60, 120)))
_rl.configure(image=_rp, text="")
_rl.image = _rp
_rw.geometry("1150x780+60+60")
root.update(); root.update_idletasks()
_iw, _ih = _rp.width(), _rp.height()
_lw, _lh = _rl.winfo_width(), _rl.winfo_height()
check("[remote] test photo fits inside the viewer label",
      100 <= _iw <= _lw and 50 <= _ih <= _lh, f"photo {_iw}x{_ih} label {_lw}x{_lh}")
_ox = _rl.winfo_rootx() + (_lw - _iw) // 2
_oy = _rl.winfo_rooty() + (_lh - _ih) // 2
_mid = dash._remote_norm(_rw, _types.SimpleNamespace(
    x_root=_ox + _iw // 2, y_root=_oy + _ih // 2))
check("[remote] centre of the photo maps to (0.5, 0.5)",
      _mid is not None and abs(_mid[0] - 0.5) < 0.01 and abs(_mid[1] - 0.5) < 0.01,
      str(_mid))
_pt75 = dash._remote_norm(_rw, _types.SimpleNamespace(
    x_root=_ox + int(_iw * 0.75), y_root=_oy + int(_ih * 0.6)))
check("[remote] a point at 75 % / 60 % maps exactly",
      _pt75 is not None and abs(_pt75[0] - 0.75) < 0.01
      and abs(_pt75[1] - 0.6) < 0.01, str(_pt75))
check("[remote] a pointer left of the picture is never forwarded",
      dash._remote_norm(_rw, _types.SimpleNamespace(
          x_root=_ox - 10, y_root=_oy + 5)) is None)
_rw.destroy()
# --- spec 1: header keeps Logout, Minimize button removed -------------------
def _is_button(w):
    """A pressable button.

    CustomTkinter widgets are Frames all the way down (winfo_class() is
    'Frame' for CTkButton too), so the widget *type* - not the class name -
    is what identifies a button now."""
    return w.winfo_class() in ("Button", "TButton") or isinstance(w, _ctk.CTkButton)


def _is_label(w):
    return w.winfo_class() in ("Label", "TLabel") or isinstance(w, _ctk.CTkLabel)


def _all_button_texts(rootw):
    out, stack = [], [rootw]
    while stack:
        w = stack.pop()
        try:
            stack.extend(w.winfo_children())
            if _is_button(w):
                out.append(str(w.cget("text")))
        except Exception:
            pass
    return out

_hdr_btns = _all_button_texts(dash)
check("header keeps Logout, has NO Minimize button (spec 1)",
      any("Logout" in t for t in _hdr_btns)
      and not any("Minimize" in t for t in _hdr_btns),
      str([t for t in _hdr_btns if "Minimize" in t or "Logout" in t]))
check("Lab Attendance page removed",
      "attendance" not in dash.pages and "attendance" not in dash._sidebar_btns)
check("OJT pages + menu entries removed (spec 3)",
      "ojt" not in dash.pages and "tasks" not in dash.pages
      and "ojt" not in dash._sidebar_btns and "tasks" not in dash._sidebar_btns
      and not any("ojt" in str(b.cget("text")).lower()
                  for b in dash._more_btns))
check("settings page present",
      "settings" in dash._sidebar_btns and "settings" in dash.pages)
# --- Staff & Admin page offers the maintenance role (created by an admin
# with a password of their own - nothing is ever seeded) -------------
sf = next(cf for cf in dash.pages["staff"].winfo_children()
          if isinstance(cf, UserCRUDFrame))
role_field = next((f for f in sf.fields if f["name"] == "role"), None)
check("staff page offers the maintenance role",
      role_field is not None
      and "maintenance" in (role_field.get("options") or []),
      str(role_field))
check("staff page lists maintenance accounts",
      "maintenance" in (sf.where_clause or ""), str(sf.where_clause))
# --- P2-9: the Staff & Admin page opens the SAME Account Details window ----
_sf_btns = _all_button_texts(sf)
check("staff page offers a View Details action",
      any("View Details" in t for t in _sf_btns), str(_sf_btns))
check("staff page frame is still a UserCRUDFrame",
      isinstance(sf, UserCRUDFrame), str(type(sf).__name__))
_sf_kids = sf.tree.get_children()
check("staff table lists staff/admin/maintenance rows",
      len(_sf_kids) >= 1, str(len(_sf_kids)))
sf.tree.selection_set(_sf_kids[0])
_sf_win = sf.open_details()
check("staff Account Details window opens", _sf_win is not None)
check("staff Account Details has the 4 tabs",
      [str(sf.details_tabs.tab(t, "text")) for t in sf.details_tabs.tabs()]
      == ["Overview", "Sessions", "Activity Logs", "PC Usage"],
      str([str(sf.details_tabs.tab(t, "text"))
           for t in sf.details_tabs.tabs()]))
check("the window code is AccountsFrame's, not a copy",
      sf.open_details.__func__ is AccountsFrame.open_details
      and sf.close_details.__func__ is AccountsFrame.close_details,
      str(getattr(sf.open_details, "__func__", None)))
_sf_role = str(sf._detail_labels["role"].cget("text"))
check("staff Details shows the account role",
      _sf_role in ("admin", "staff", "maintenance"), _sf_role)
check("staff Details overview fields never include a password",
      all(str(k) not in ("password", "password_hash")
          for _, k in sf.OVERVIEW),
      str([k for _, k in sf.OVERVIEW]))
_conn = database.get_connection()
_acc = _conn.execute(
    "SELECT password FROM users WHERE student_id=?",
    (str(sf._detail_labels["student_id"].cget("text")),)).fetchone()
_conn.close()
_hash = str(_acc["password"]) if _acc and _acc["password"] else ""
_ov_text = " | ".join(str(lab.cget("text"))
                      for lab in sf._detail_labels.values())
check("staff Details never reports the password or its hash",
      "$" not in _ov_text and (not _hash or _hash not in _ov_text),
      _ov_text)
sf.close_details()
check("staff Details window closes cleanly", sf.details_win is None)

# --- P2-10: a blank password is NEVER written as "" (staff/admin page) -----
# On the student Accounts page blank now falls back to the factory default;
# this page has no forced-change guarantee for admin/staff/maintenance, so
# it must refuse instead of silently creating an unusable account.
_toasts_p210, _orig_notify_p210, _orig_toast_p210 = [], sf.notify, dash.toast
sf.notify = lambda t, k="info": _toasts_p210.append((k, t))
dash.toast = lambda t, k="info": _toasts_p210.append((k, t))
sf.entries["student_id"][1].set("staffblank1")
sf.entries["full_name"][1].set("Staff Blank Fixture")
sf.entries["password"][1].set("")
sf.add_record()
_p210_row = database.get_connection().execute(
    "SELECT COUNT(*) FROM users WHERE student_id='staffblank1'").fetchone()
check("staff add never stores an empty password",
      int(_p210_row[0]) == 0, str(int(_p210_row[0])))
check("staff add explains that a password is required",
      any("password" in str(t).lower() for _k, t in _toasts_p210),
      str(_toasts_p210))
_toasts_p210.clear()
sf.entries["password"][1].set("real-secret-1")
sf.add_record()
_p210_row = database.get_connection().execute(
    "SELECT password FROM users WHERE student_id='staffblank1'"
).fetchone()
check("staff add with a password stores a hash only",
      _p210_row is not None
      and database.verify_password("real-secret-1", _p210_row["password"])
      and _p210_row["password"] != "real-secret-1",
      str(_p210_row and _p210_row["password"]))
_p210_conn = database.get_connection()
_p210_conn.execute("DELETE FROM users WHERE student_id='staffblank1'")
_p210_conn.commit()
_p210_conn.close()
sf.clear_form()
sf.refresh()
sf.notify = _orig_notify_p210
dash.toast = _orig_toast_p210
check("staff blank-password fixtures are cleaned up",
      database.get_connection().execute(
          "SELECT COUNT(*) FROM users WHERE student_id='staffblank1'"
      ).fetchone()[0] == 0)
# --- simplified sidebar: 7 primary entries + collapsed "More" group -----
_primary = {"overview": "Dashboard", "clients": "Client PCs",
            "sessions": "Sessions", "students": "Accounts",
            "staff": "Staff & Admin", "activity": "Audit Trail",
            "settings": "Settings"}
check("sidebar has the 7 simplified primary entries",
      all(k in dash._sidebar_btns
          and _primary[k] in str(dash._sidebar_btns[k].cget("text"))
          for k in _primary),
      str({k: str(dash._sidebar_btns[k].cget("text"))
           for k in _primary if k in dash._sidebar_btns}))
check("More group exists and is collapsed by default",
      bool(getattr(dash, "_more_btns", None))
      and not dash._more_expanded
      and all(not b.winfo_ismapped() for b in dash._more_btns),
      f"n={len(getattr(dash, '_more_btns', []))} "
      f"expanded={getattr(dash, '_more_expanded', None)}")
dash._toggle_more()
root.update()
check("More group expands",
      dash._more_expanded and all(b.winfo_ismapped() for b in dash._more_btns))
dash._toggle_more()
root.update()
check("More group collapses again",
      not dash._more_expanded
      and all(not b.winfo_ismapped() for b in dash._more_btns))

# --- spec 5: inventory system - stats, actions, quantity math -------------
from crud_frame import InventoryCRUDFrame
inv = next((cf for cf in dash.pages["inventory"].winfo_children()
            if isinstance(cf, InventoryCRUDFrame)), None)
check("inventory page uses InventoryCRUDFrame", inv is not None)
check("inventory stats strip has 6 tiles",
      inv is not None and len(getattr(inv, "stat_labels", {})) == 6
      and hasattr(inv, "low_stock_lbl"))
_inv_btns = _all_button_texts(dash.pages["inventory"])
check("inventory has all 6 stock actions",
      all(t in _inv_btns for t in
          ("Increase Stock", "Decrease Stock", "Assign Item",
           "Return Item", "Mark Damaged", "Mark Lost")),
      str(_inv_btns))
# two clean rows: one normal, one at/below the low-stock threshold (3)
conn = database.get_connection()
conn.execute(
    "INSERT INTO inventory (item_name, category, quantity, available_qty,"
    " assigned_qty, condition_status, status, location, notes,"
    " date_added, last_updated)"
    " VALUES ('Test Keyboard','Peripherals',10,10,0,'Good','AVAILABLE',"
    " 'Lab A','demo','2026-01-01','2026-01-01')")
conn.execute(
    "INSERT INTO inventory (item_name, category, quantity, available_qty,"
    " assigned_qty, condition_status, status, location, notes,"
    " date_added, last_updated)"
    " VALUES ('Test Cable','Cables',2,2,0,'New','AVAILABLE',"
    " 'Lab B','','2026-01-01','2026-01-01')")
conn.commit()
kb_id = conn.execute("SELECT id FROM inventory "
                     "WHERE item_name='Test Keyboard'").fetchone()["id"]
conn.close()
inv.refresh()
st = inv._stats()
check("inventory statistics computed",
      st["total"] >= 2 and st["available"] >= 2
      and st["inuse"] == 0 and st["borrowed"] == 0 and st["damaged"] >= 1,
      str(st))
check("inventory low-stock list flags items at/below threshold",
      st["low"] >= 1 and ("Test Cable", 2) in st["low_items"], str(st))
check("inventory low-stock warning shown",
      "Low Stock" in str(inv.low_stock_lbl.cget("text"))
      and "Test Cable" in str(inv.low_stock_lbl.cget("text"))
      and "2 remaining" in str(inv.low_stock_lbl.cget("text")),
      str(inv.low_stock_lbl.cget("text")))
check("inventory stats tiles display the counts",
      str(inv.stat_labels["total"].cget("text")) == str(st["total"])
      and str(inv.stat_labels["low"].cget("text")) == str(st["low"]))

# stock math: increase / decrease (clamped) / assign / return / status
inv._stock_change(kb_id, 5, True)
r = inv._row(kb_id)
check("increase adds to quantity + available",
      r["quantity"] == 15 and r["available_qty"] == 15
      and r["status"] == "AVAILABLE", str(dict(r)))
inv._stock_change(kb_id, 20, False)
r = inv._row(kb_id)
check("decrease clamps at zero -> OUT OF STOCK",
      r["quantity"] == 0 and r["available_qty"] == 0
      and r["status"] == "OUT OF STOCK", str(dict(r)))
inv._stock_change(kb_id, 4, True)
r = inv._row(kb_id)
check("increase revives OUT OF STOCK",
      r["quantity"] == 4 and r["available_qty"] == 4
      and r["status"] == "AVAILABLE", str(dict(r)))
check("assign moves available to assigned",
      inv._assign_qty(kb_id, 3)
      and inv._row(kb_id)["available_qty"] == 1
      and inv._row(kb_id)["assigned_qty"] == 3
      and inv._row(kb_id)["status"] == "BORROWED")
check("assign beyond available is refused",
      not inv._assign_qty(kb_id, 5))
check("partial return brings stock back",
      inv._return_qty(kb_id, 2) == 2
      and inv._row(kb_id)["available_qty"] == 3
      and inv._row(kb_id)["assigned_qty"] == 1
      and inv._row(kb_id)["status"] == "BORROWED")
check("full return -> AVAILABLE",
      inv._return_qty(kb_id, 5) == 1
      and inv._row(kb_id)["available_qty"] == 4
      and inv._row(kb_id)["assigned_qty"] == 0
      and inv._row(kb_id)["status"] == "AVAILABLE")
check("mark damaged / mark lost set the status",
      inv._set_status(kb_id, "DAMAGED")
      and inv._row(kb_id)["status"] == "DAMAGED"
      and inv._set_status(kb_id, "LOST")
      and inv._row(kb_id)["status"] == "LOST")

# add form: blank available derives from quantity - assigned
inv.clear_form()
inv.entries["item_name"][1].set("Form Mouse")
inv.entries["quantity"][1].set("7")
inv.entries["assigned_qty"][1].set("2")
inv.add_record()
conn = database.get_connection()
fm = conn.execute("SELECT * FROM inventory "
                  "WHERE item_name='Form Mouse'").fetchone()
conn.close()
check("add derives available + status from the stock split",
      fm and fm["quantity"] == 7 and fm["available_qty"] == 5
      and fm["assigned_qty"] == 2 and fm["status"] == "BORROWED"
      and fm["date_added"] and fm["last_updated"],
      str(dict(fm) if fm else None))
# validation: available + assigned can never exceed quantity
inv.selected_id = fm["id"]
inv.entries["quantity"][1].set("5")
inv.entries["available_qty"][1].set("10")
inv.entries["assigned_qty"][1].set("0")
inv.update_record()
conn = database.get_connection()
qty_after = conn.execute(
    "SELECT quantity FROM inventory WHERE id=?",
    (fm["id"],)).fetchone()["quantity"]
conn.close()
check("update rejects available+assigned > quantity",
      qty_after == 7, str(qty_after))
from utils import now_date as _nd
inv.clear_form()
inv.entries["item_name"][1].set("Form Mouse")
inv.entries["quantity"][1].set("9")
inv.entries["available_qty"][1].set("9")
inv.entries["assigned_qty"][1].set("0")
inv.selected_id = fm["id"]
inv.update_record()
conn = database.get_connection()
fm2 = conn.execute("SELECT * FROM inventory WHERE id=?", (fm["id"],)).fetchone()
conn.close()
check("valid update saves and stamps last_updated",
      fm2["quantity"] == 9 and fm2["available_qty"] == 9
      and str(fm2["last_updated"]) == _nd(), str(dict(fm2)))
from utils import get_status_icon, STATUS_ORDER
def _tree_show(t):
    # ttk cget("show") returns a tuple of index objects, not a plain string
    return " ".join(str(x) for x in t.cget("show"))
check("trees have a status-icon column",
      _tree_show(dash.client_tree) == "tree headings"
      and _tree_show(dash.comp_tree) == "tree headings"
      and _tree_show(dash.audit_tree) == "tree headings",
      str(_tree_show(dash.client_tree)))
check("all 8 status icons available",
      all(get_status_icon(k, 16) is not None for k in STATUS_ORDER),
      str(STATUS_ORDER))
check("status icons are square (no distortion)",
      get_status_icon("locked", 64).width() == 64
      and get_status_icon("locked", 64).height() == 64)
check("client tree populated", len(dash.client_tree.get_children()) > 0)
check("overview stats strip has exactly the 7 PC stats",
      len(dash.cards_frame.winfo_children()) == 7
      and list(getattr(dash, "_stat_tiles", [])) == list(dash.STAT_TILES),
      str(getattr(dash, "_stat_tiles", None)))
check("filter chips: All + 8 statuses with live counts",
      len(dash._legend_btns) == 9
      and all("(" in str(b.cget("text")) for b in dash._legend_btns.values()),
      str([str(b.cget("text")) for b in dash._legend_btns.values()]))
root.update()   # let the chip row rewrap settle before measuring
_ov_edge = dash.detail_panels[0]["frame"].winfo_rootx() - 1
_bad_chips = [str(b.cget("text")) for b in dash._legend_btns.values()
              if not b.winfo_ismapped()
              or b.winfo_rootx() + b.winfo_width() > _ov_edge]
check("filter chips fully visible left of the details panel (wrapped)",
      not _bad_chips, f"edge={_ov_edge} bad={_bad_chips}")
check("search shows the Search PCs... placeholder",
      dash._pc_search_ph and str(dash.pc_search_var.get()) == "Search PCs...",
      str(dash.pc_search_var.get()))
check("toast notification helper", callable(dash.toast))
check("controls disabled until a PC is selected",
      bool(dash._ctrl_btns) and all(str(b.cget("state")) == "disabled"
                                    for b in dash._ctrl_btns))
# close button must minimize, never log out / destroy (Phase E)
script = root.tk.call("wm", "protocol", dash._w, "WM_DELETE_WINDOW")
root.tk.call(script)
root.update()
check("close button does not log out",
      dash.winfo_exists() and not logout_called)
check("close button minimizes instead of closing",
      dash.state() in ("icon", "iconic"), dash.state())
dash.deiconify(); root.update()

# computers page columns (Phase H)
check("computers page tree exists", hasattr(dash, "comp_tree"))
headers = [dash.comp_tree.heading(c, "text") for c in dash.comp_tree["columns"]
           if c != "id"]
check("computers view has 9 useful columns incl. Group",
      headers == ["PC Name", "Group", "IP Address", "Hostname",
                  "Online/Offline", "Current User", "CPU %", "RAM %",
                  "Last Seen"], str(headers))
comp_names = [dash.comp_tree.item(i, "values")[1]
              for i in dash.comp_tree.get_children()]
check("computers page hides never-connected seed rows",
      "SEED-PC" not in comp_names, str(comp_names))
check("computers page keeps manual inventory rows",
      "MANUAL-PC" in comp_names, str(comp_names))

# PC group tag (spec item 7): saved through the form, shown in the tree,
# offered back in the combo for Website Access scopes
dash.comp_pc_var.set("GROUP-TEST-PC")
dash.comp_group_var.set("Lab Alpha")
dash.comp_status_var.set("Available")
dash._computer_save()
_grow = database.get_connection().execute(
    "SELECT group_name FROM computers WHERE pc_name='GROUP-TEST-PC'"
).fetchone()
check("computers save persists the group tag",
      _grow is not None and _grow["group_name"] == "Lab Alpha",
      str(dict(_grow) if _grow else None))
_gvals = [dash.comp_tree.item(i, "values")[2]
          for i in dash.comp_tree.get_children()]
check("computers tree shows the group column",
      "Lab Alpha" in _gvals, str(_gvals))
check("group combo offers distinct existing groups",
      "Lab Alpha" in dash.comp_group_cb.cget("values"),
      str(list(dash.comp_group_cb.cget("values"))))
conn = database.get_connection()
conn.execute("DELETE FROM computers WHERE pc_name='GROUP-TEST-PC'")
conn.commit()
conn.close()
dash.comp_group_var.set("")
dash._refresh_computers()

# audit page modes + filters (Phase J)
check("activity page built", hasattr(dash, "audit_tree"))
dash._set_audit_mode("commands")
check("audit command columns",
      list(dash.audit_tree["columns"]) ==
      ["created_at", "admin_user", "command_type", "target_pc", "status", "result"],
      str(dash.audit_tree["columns"]))
check("audit has command rows", len(dash.audit_tree.get_children()) > 0)
dash._set_audit_mode("events")
check("audit event columns",
      list(dash.audit_tree["columns"]) ==
      ["timestamp", "admin_user", "action", "target", "details"])
dash.audit_status_var.set("Done")     # filter must not crash
dash._refresh_activity()

# ---- audit taxonomy + event-type filtering ------------------------------
from admin_dashboard import (AUDIT_TAXONOMY, AUDIT_LABELS,
                             audit_action_label, audit_action_category)
conn = database.get_connection()
db_actions = [r["action"] for r in conn.execute(
    "SELECT DISTINCT action FROM admin_activity_log").fetchall()]
conn.close()
check("audit taxonomy covers every recorded action",
      len(db_actions) > 0 and
      all(audit_action_category(a) != "Other" for a in db_actions),
      str([a for a in db_actions if audit_action_category(a) == "Other"]))
check("audit labels are human-readable",
      audit_action_label("network_disconnect") == "Network Disconnect"
      and audit_action_label("client_crash") == "Client Crash"
      and audit_action_label("BULK_PAUSE") == "Bulk Pause"
      and audit_action_label("command:cmd_lock").startswith("Command: "),
      str(audit_action_label("BULK_PAUSE")))
check("Type filter lists the taxonomy categories in order",
      list(AUDIT_TAXONOMY) == ["Login / Logout", "Connection", "Security",
                               "Website Access", "Power", "Monitoring",
                               "Admin Commands"],
      str(list(AUDIT_TAXONOMY)))
check("Website Access findings land in their own audit category",
      all(audit_action_category(a) == "Website Access" and
          audit_action_label(a) != a
          for a in ("web_access_detected", "web_access_unresolved",
                    "web_browser_closed", "web_close_skipped",
                    "web_close_failed")),
      str([audit_action_category(a) for a in ("web_access_detected",
                                              "web_browser_closed")]))
check("Type combobox values match the taxonomy",
      list(dash.audit_type_cb.cget("values")) ==
      ["All Types"] + list(AUDIT_TAXONOMY) + ["Other"],
      str(list(dash.audit_type_cb.cget("values"))))
_inv = {v: k for k, v in AUDIT_LABELS.items()}
dash.audit_status_var.set("All")
dash.audit_type_var.set("Connection")
dash._refresh_activity()
shown = [_inv.get(str(dash.audit_tree.item(i, "values")[2]),
                  str(dash.audit_tree.item(i, "values")[2]))
         for i in dash.audit_tree.get_children()]
check("Connection filter shows only connection events",
      len(shown) > 0 and
      all(audit_action_category(a) == "Connection" for a in shown),
      str(shown[:8]))
dash.audit_type_var.set("Login / Logout")
dash._refresh_activity()
shown2 = [_inv.get(str(dash.audit_tree.item(i, "values")[2]),
                   str(dash.audit_tree.item(i, "values")[2]))
          for i in dash.audit_tree.get_children()]
check("Login / Logout filter excludes connection events",
      all(audit_action_category(a) == "Login / Logout" for a in shown2),
      str(shown2[:8]))
# restore the default view for everything that follows
dash.audit_type_var.set("All Types")
dash._refresh_activity()

# account-change hook logs the user out (Phase I)
conn = database.get_connection()
conn.execute(
    "INSERT OR REPLACE INTO client_sessions "
    "(session_id, pc_name, student_id, full_name, login_time, status) "
    "VALUES ('sessUI', 'TEST-PC', '2023-00001', 'Juan', ?, 'Active')",
    (now_datetime(),))
conn.commit(); conn.close()
dash._on_account_change("update", {"student_id": "2023-00001"})
time.sleep(0.4)
row = database.get_connection().execute(
    "SELECT status FROM client_sessions WHERE session_id='sessUI'").fetchone()
check("account update auto-logs-out session",
      row and row["status"] == "Forced Logout", str(dict(row) if row else None))

# live-sync: the change is pushed through the server event channel ...
check("account change pushes an account_changed server event",
      any(k == "account_changed" for k, _ in list(events.queue)),
      str([k for k, _ in list(events.queue)]))
# ... and the handler refreshes every account-bearing page at once
_live_calls = []
def _spy(key, fn):
    def _wrapped():
        _live_calls.append(key)
        return fn()
    return _wrapped
_orig_pages = {k: dash.page_refresh.get(k)
               for k in ("students", "staff", "sessions", "activity")}
for _k, _fn in _orig_pages.items():
    if _fn:
        dash.page_refresh[_k] = _spy(_k, _fn)
dash._handle_server_event("account_changed",
                          {"action": "update", "student_id": "2023-00001"})
_expect_live = {k for k, fn in _orig_pages.items() if fn}
dash.page_refresh.update({k: v for k, v in _orig_pages.items() if v})
check("account_changed live-syncs every account view",
      set(_live_calls) == _expect_live and len(_expect_live) >= 3,
      str(sorted(set(_live_calls))))

# website changes from the dashboard are audited + re-pushed
conn = database.get_connection()
conn.execute("INSERT INTO websites (domain) VALUES ('social.test')")
conn.commit()
conn.close()
dash._on_website_change("update", {"domain": "social.test"})
row = database.get_connection().execute(
    "SELECT admin_user, target, details FROM admin_activity_log "
    "WHERE action='webfilter_change' ORDER BY id DESC LIMIT 1").fetchone()
check("website change audits webfilter_change",
      row is not None and row["target"] == "social.test"
      and "pushed to" in (row["details"] or ""),
      str(dict(row) if row else None))
try:                                   # keep w's raw socket clean: swallow
    for _ in range(50):                # the cmd_web_filter the hook pushed
        if w.recv_message(timeout=0.4) is None:
            break
except Exception:
    pass
conn = database.get_connection()
conn.execute("DELETE FROM websites")
conn.commit()
conn.close()

# ---- Website Access page (spec items 6-7-8) ----
check("website page has the mode selector",
      getattr(dash, "web_mode_var", None) is not None
      and dash.web_mode_var.get() in dash.WEB_MODE_LABELS,
      getattr(dash, "web_mode_var", "missing").get()
      if hasattr(dash, "web_mode_var") else "missing")
check("website page has scope radios + target combos",
      hasattr(dash, "web_scope_var") and hasattr(dash, "web_individual_cb")
      and hasattr(dash, "web_group_cb"), "scope widgets")
check("website page shows the per-PC status table (PC|Mode|Status)",
      getattr(dash, "web_status_tree", None) is not None
      and list(dash.web_status_tree["columns"]) == ["pc", "mode", "status"],
      str(list(dash.web_status_tree["columns"]))
      if hasattr(dash, "web_status_tree") else "missing")
check("website page has one unified rules table",
      getattr(dash, "web_rules", None) is not None
      and list(dash.web_rules.tree["columns"]) ==
      ["domain", "category", "action", "status", "notes"]
      and getattr(dash, "_web_blocked", None) is None
      and getattr(dash, "_web_allowed", None) is None,
      str(list(dash.web_rules.tree["columns"]))
      if hasattr(dash, "web_rules") else "missing")
check("rules table has Search + Category/Action/Status filters",
      all(hasattr(dash.web_rules, a) for a in
          ("search_var", "cat_var", "action_var", "status_var")),
      str([a for a in ("search_var", "cat_var", "action_var", "status_var")
           if not hasattr(dash.web_rules, a)]))
check("mode picker shows three option cards with descriptions",
      getattr(dash, "_mode_cards", None) is not None
      and set(dash._mode_cards) == {"allow_all", "block_list", "allow_only"}
      and set(dash.WEB_MODE_DESCS) == set(dash.WEB_MODE_LABELS),
      str(sorted(getattr(dash, "_mode_cards", {}))))
_ws_slaves = dash.pages["websites"].pack_slaves()
check("Policy Status sits below the unified rules table",
      _ws_slaves.index(dash.web_rules.master)
      < _ws_slaves.index(dash.web_status_tree.master),
      str([w.winfo_class() for w in _ws_slaves]))
dash._refresh_web_status()
_status_rows = [dash.web_status_tree.item(i, "values")
                for i in dash.web_status_tree.get_children()]
check("status table lists every PC with mode + status",
      any(str(v[0]) == "TEST-PC" for v in _status_rows)
      and all(len(v) == 3 for v in _status_rows),
      str(_status_rows[:8]))
# scope = individual: the mode lands on exactly that PC (spec item 7)
_keep_alive()                      # TEST-PC must be online to be SYNCING
_orig_toast = dash.toast
_toasts = []
dash.toast = lambda msg, kind=None: _toasts.append((kind, msg))
dash.web_mode_var.set("block_list")
dash.web_scope_var.set("individual")
dash.web_individual_cb.set("TEST-PC")
dash._apply_web_mode()
_grow = database.get_connection().execute(
    "SELECT mode, sync_status FROM web_pc_policy WHERE pc_name='TEST-PC'"
).fetchone()
check("apply to an individual PC stores its mode",
      _grow is not None and _grow["mode"] == "block_list"
      and _grow["sync_status"] == "SYNCING",
      str(dict(_grow) if _grow else None))
check("apply reports the pushed version in a toast",
      any("Policy v" in m for _k, m in _toasts), str(_toasts))
check("apply audits the scope push",
      database.get_connection().execute(
          "SELECT COUNT(*) AS n FROM admin_activity_log "
          "WHERE action='webfilter_change' AND details LIKE '%policy v%'"
      ).fetchone()["n"] >= 1, "webfilter_change with policy version")
# group scope with no members warns instead of pushing anything
_toasts.clear()
dash.web_scope_var.set("group")
dash.web_group_cb.set("Ghost Group")
dash._apply_web_mode()
check("group scope with no PCs warns and pushes nothing",
      any(_k == "warn" for _k, m in _toasts)
      and not any("Policy v" in m for _k, m in _toasts), str(_toasts))
# selected scope without a selection also warns
_toasts.clear()
dash.web_scope_var.set("selected")
dash.web_status_tree.selection_remove(dash.web_status_tree.selection())
dash._apply_web_mode()
check("selected scope without a selection warns",
      any(_k == "warn" for _k, m in _toasts), str(_toasts))

# ---- redesign: multi-PC Apply Policy must confirm first (spec: dialogs
#      are reserved for bulk/destructive actions) ----
import tkinter.messagebox as _mb
from utils import ACCENT as _ACCENT, BORDER as _BORDER
_orig_yesno = _mb.askyesno
_asked = []
_mb.askyesno = lambda *a, **k: (_asked.append(a), False)[1]
_audit0 = database.get_connection().execute(
    "SELECT COUNT(*) AS n FROM admin_activity_log "
    "WHERE action='webfilter_change'").fetchone()["n"]
_toasts.clear()
dash.web_scope_var.set("all")
dash._apply_web_mode()
check("multi-PC apply asks for confirmation first",
      len(_asked) == 1, str(_asked))
check("declining the confirmation pushes nothing",
      not any("Policy v" in m for _k, m in _toasts)
      and database.get_connection().execute(
          "SELECT COUNT(*) AS n FROM admin_activity_log "
          "WHERE action='webfilter_change'").fetchone()["n"] == _audit0,
      str(_toasts))
_asked.clear()
_mb.askyesno = lambda *a, **k: (_asked.append(a), True)[1]
_toasts.clear()
dash._apply_web_mode()
check("accepting the confirmation applies the policy",
      len(_asked) == 1 and any("Policy v" in m for _k, m in _toasts),
      str((_asked, _toasts)))
_mb.askyesno = _orig_yesno

# ---- redesign: mode option cards (select + accent highlight) ----
dash._pick_mode("allow_only")
check("clicking a mode card selects that mode",
      dash.web_mode_var.get() == "allow_only", dash.web_mode_var.get())
check("the selected mode card is outlined in the accent color",
      dash._mode_cards["allow_only"].cget("border_color") == _ACCENT
      and dash._mode_cards["block_list"].cget("border_color") == _BORDER,
      str({m: c.cget("border_color")
           for m, c in dash._mode_cards.items()}))
dash._pick_mode("block_list")

# ---- redesign: Add Website dialog (fields + URL normalization) ----
dash.web_rules._open_dialog()
check("Add Website dialog collects Domain/Category/Action/Status/Notes",
      dash.web_rules._dialog is not None
      and set(dash.web_rules._dlg_vars) ==
      {"domain", "category", "action", "enabled", "notes"},
      str(sorted(dash.web_rules._dlg_vars)))
dash.web_rules._dlg_vars["domain"].set("HTTPS://Pasted.Example.com/a?x=1")
dash.web_rules._dlg_vars["category"].set("Social")
dash.web_rules._dlg_vars["action"].set("Block")
dash.web_rules._dlg_vars["enabled"].set("Enabled")
dash.web_rules._dlg_vars["notes"].set("pasted URL")
_saved = dash.web_rules._save()
_prow = database.get_connection().execute(
    "SELECT domain, list_type, enabled, category, notes FROM websites "
    "WHERE domain='pasted.example.com'").fetchone()
check("saving normalizes a pasted URL into a bare domain",
      _saved is True and _prow is not None
      and _prow["list_type"] == "blocked" and _prow["enabled"] == 1
      and _prow["category"] == "Social"
      and _prow["notes"] == "pasted URL",
      str(dict(_prow) if _prow else None))
check("the dialog closes itself after a successful save",
      dash.web_rules._dialog is None, str(dash.web_rules._dialog))

# ---- redesign: Category / Action / Status filters ----
conn = database.get_connection()
conn.execute("INSERT OR REPLACE INTO websites"
             " (domain, list_type, enabled, category)"
             " VALUES ('filt.block.test','blocked',1,'FiltCat')")
conn.execute("INSERT OR REPLACE INTO websites"
             " (domain, list_type, enabled, category)"
             " VALUES ('filt.allow.test','allowed',0,'FiltOther')")
conn.commit()
conn.close()
dash.web_rules.refresh()
_rows = [str(v[0]) for v in
         [dash.web_rules.tree.item(i, "values")
          for i in dash.web_rules.tree.get_children()]]
check("both filter fixtures are in the unified table",
      "filt.block.test" in _rows and "filt.allow.test" in _rows
      and "pasted.example.com" in _rows, str(_rows))
dash.web_rules.cat_var.set("FiltCat")
_rows = [str(v[0]) for v in
         [dash.web_rules.tree.item(i, "values")
          for i in dash.web_rules.tree.get_children()]]
check("Category filter narrows the rules table",
      _rows == ["filt.block.test"], str(_rows))
dash.web_rules.cat_var.set("All")
dash.web_rules.action_var.set("Allow")
_rows = [str(v[0]) for v in
         [dash.web_rules.tree.item(i, "values")
          for i in dash.web_rules.tree.get_children()]]
check("Action filter shows only Allow rules",
      "filt.allow.test" in _rows and "filt.block.test" not in _rows
      and "pasted.example.com" not in _rows, str(_rows))
dash.web_rules.action_var.set("All")
dash.web_rules.status_var.set("Disabled")
_rows = [str(v[0]) for v in
         [dash.web_rules.tree.item(i, "values")
          for i in dash.web_rules.tree.get_children()]]
check("Status filter shows only Disabled rules",
      _rows == ["filt.allow.test"], str(_rows))
dash.web_rules.status_var.set("All")

# ---- redesign: Delete Selected asks, honors Cancel and confirms ----
_rid = database.get_connection().execute(
    "SELECT id FROM websites WHERE domain='filt.block.test'").fetchone()["id"]
dash.web_rules.refresh()
dash.web_rules.tree.selection_set(str(_rid))
dash.web_rules._on_select()
_ans = [False]
_mb.askyesno = lambda *a, **k: _ans[0]
_kept = dash.web_rules.delete_selected()
_kept_row = database.get_connection().execute(
    "SELECT id FROM websites WHERE domain='filt.block.test'").fetchone()
check("declined delete keeps the rule",
      _kept is False and _kept_row is not None,
      str(dict(_kept_row) if _kept_row else None))
_ans[0] = True
_gone = dash.web_rules.delete_selected()
_gone_row = database.get_connection().execute(
    "SELECT id FROM websites WHERE domain='filt.block.test'").fetchone()
check("confirmed delete removes the rule",
      _gone is True and _gone_row is None, str(_gone))
_mb.askyesno = _orig_yesno
# drop the dialog + filter fixtures again (keeps later checks deterministic)
conn = database.get_connection()
conn.execute("DELETE FROM websites WHERE domain IN"
             " ('pasted.example.com','filt.allow.test')")
conn.commit()
conn.close()
dash.web_rules.refresh()
dash.toast = _orig_toast
try:                                   # keep w's raw socket clean: swallow
    for _ in range(50):                # the cmd_web_filter the apply pushed
        if w.recv_message(timeout=0.4) is None:
            break
except Exception:
    pass
conn = database.get_connection()
conn.execute("UPDATE web_pc_policy SET sync_status='OFFLINE'")
conn.commit()
conn.close()

# ---- spec item 4: instant filtering (rows change as you type) ----------
# Every list search box is wired with trace_add -> refresh, so setting the
# var behaves exactly like a keystroke: no Enter or Search button anywhere.
from admin_dashboard import SEARCH_HINT


def _trows(tree):
    return [tree.item(i, "values") for i in tree.get_children()]


def _instant(name, tree, var, needle, prep=None, post=None):
    if prep:
        prep()
    var.set("")                                  # clear the box
    base = _trows(tree)
    var.set("zzzz-no-such-row-anywhere")         # type nonsense
    none = _trows(tree)
    var.set(needle)                              # type a real match
    hit = _trows(tree)
    var.set("")                                  # clear the box again
    back = _trows(tree)
    if post:
        post()
    check(name, len(base) > 0 and len(none) == 0
          and 1 <= len(hit) <= len(base)
          and len(back) == len(base),
          f"base={len(base)} none={len(none)} hit={len(hit)} back={len(back)}")


# PCs (Client PCs page) - placeholder box; simulate the click first
dash._client_search_focus_in()
_pcs0 = [str(v[0]) for v in _trows(dash.client_tree)]
_instant("PCs page filters instantly as you type",
         dash.client_tree, dash.client_search_var,
         (_pcs0[0] if _pcs0 else "TEST-PC").lower(),
         post=dash._client_search_focus_out)
check("PCs filter box shows its placeholder when idle",
      dash._client_search_ph and dash.client_search_var.get() == SEARCH_HINT,
      str(dash.client_search_var.get()))

# Accounts (students list)
_instant("Accounts page filters instantly as you type",
         dash.students_frame.tree, dash.students_frame.search_var, "juan")

# Sessions (fixture row so the needle is fully deterministic)
conn = database.get_connection()
conn.execute(
    "INSERT OR REPLACE INTO client_sessions "
    "(session_id, pc_name, student_id, full_name, login_time, status) "
    "VALUES ('sessINSTANT', 'TEST-PC', 'inst001', 'Instant Tester', ?, "
    "'Completed')", (now_datetime(),))
conn.commit()
conn.close()
dash.sessions_frame.refresh()
_instant("Sessions page filters instantly as you type",
         dash.sessions_frame.tree, dash.sessions_frame.search_var, "inst001")
conn = database.get_connection()
conn.execute("DELETE FROM client_sessions WHERE session_id='sessINSTANT'")
conn.commit()
conn.close()
dash.sessions_frame.refresh()

# ---- redesign (Phase B): Sessions = read-only monitor --------------------
_sf = dash.sessions_frame
_orig_page = dash.current_page        # later checks assume the old page
_sf.refresh()
check("Sessions page is read-only (no CRUD form)",
      not hasattr(_sf, "entries") and not hasattr(_sf, "add_record"),
      str(sorted(a for a in ("entries", "add_record") if hasattr(_sf, a))))
check("Sessions page has 4 summary cards",
      set(getattr(_sf, "_card_vals", {})) ==
      {"active", "today", "total", "dead"},
      str(sorted(getattr(_sf, "_card_vals", {}))))
check("summary card values are numbers",
      all(str(_sf._card_vals[k].cget("text")).isdigit()
          for k in _sf._card_vals),
      str({k: _sf._card_vals[k].cget("text") for k in _sf._card_vals}))
check("Sessions page has Search/Status/PC/Student/Date filters",
      all(hasattr(_sf, a) for a in
          ("search_var", "status_var", "pc_var", "student_var", "date_var")),
      str([a for a in ("search_var", "status_var", "pc_var", "student_var",
                       "date_var") if not hasattr(_sf, a)]))
check("Sessions page has the Session Details panel",
      hasattr(_sf, "_det") and set(_sf._det) ==
      {"session_id", "pc_name", "student_id", "full_name", "_status",
       "login_time", "logout_time", "_duration", "connection_type",
       "ip_address"},
      str(sorted(getattr(_sf, "_det", {}))))
check("Sessions page has Force Logout + View Activity + Export CSV",
      all(callable(getattr(_sf, a, None)) for a in
          ("force_logout", "view_activity", "export_csv")), "actions")

# status display mapping + captured connection medium (fixture rows)
conn = database.get_connection()
conn.execute("DELETE FROM client_sessions WHERE student_id='disp001'")
for _st in ("Active", "Completed", "Disconnected", "Forced Logout"):
    conn.execute(
        "INSERT INTO client_sessions (session_id, pc_name, student_id,"
        " full_name, login_time, status, connection_type)"
        " VALUES (?,?,?,?,?,?,?)",
        (f"disp{_st.replace(' ', '')}", "DISP-PC", "disp001",
         "Display Tester", "2026-01-01 09:00:00", _st, "Ethernet"))
conn.commit()
conn.close()
_sf.refresh()
_disp = [str(v[7]) for v in _trows(_sf.tree) if str(v[1]) == "disp001"]
check("statuses display ACTIVE/COMPLETED/TERMINATED/FORCED LOGOUT",
      set(_disp) == {"ACTIVE", "COMPLETED", "TERMINATED", "FORCED LOGOUT"},
      str(sorted(_disp)))
check("connection type column shows the captured medium",
      all(str(v[6]) == "Ethernet" for v in _trows(_sf.tree)
          if str(v[1]) == "disp001"), str(_disp))

# instant filters: Status / PC / Student / Date
_sf.status_var.set("TERMINATED")
_rows = _trows(_sf.tree)
check("Status filter keeps only TERMINATED rows",
      bool(_rows) and all(str(v[7]) == "TERMINATED" for v in _rows),
      f"n={len(_rows)}")
_sf.status_var.set("All")
_sf.pc_var.set("DISP-PC")
_rows = _trows(_sf.tree)
check("PC filter keeps only that PC's rows",
      bool(_rows) and all(str(v[0]) == "DISP-PC" for v in _rows),
      f"n={len(_rows)}")
_sf.pc_var.set("All")
_sf.date_var.set("2026-01-01")
_rows = _trows(_sf.tree)
check("Date filter keeps only that login date",
      bool(_rows) and all(str(v[3]).startswith("2026-01-01")
                          for v in _rows), f"n={len(_rows)}")
_sf.date_var.set("")
_sf.student_var.set("disp001")
_rows = _trows(_sf.tree)
check("Student filter keeps only that account's rows",
      len(_rows) == 4 and all(str(v[1]) == "disp001" for v in _rows),
      f"n={len(_rows)}")
_sf.student_var.set("All")
_sf.clear_filters()
check("Clear resets every filter at once",
      _sf.search_var.get() == "" and _sf.status_var.get() == "All"
      and _sf.pc_var.get() == "All" and _sf.student_var.get() == "All"
      and _sf.date_var.get() == ""
      and len(_trows(_sf.tree)) >= 4, f"n={len(_trows(_sf.tree))}")

# Session Details mirrors the selected row (read-only)
_sel = [i for i in _sf.tree.get_children()
        if str(_sf.tree.item(i, "values")[1]) == "disp001"
        and str(_sf.tree.item(i, "values")[7]) == "TERMINATED"]
_sf.tree.selection_set(_sel[0])
_sf._on_select()
check("Session Details shows the selected session",
      _sf._det["student_id"].cget("text") == "disp001"
      and _sf._det["pc_name"].cget("text") == "DISP-PC"
      and _sf._det["connection_type"].cget("text") == "Ethernet"
      and _sf._det["_status"].cget("text") == "TERMINATED"
      and _sf._det["session_id"].cget("text") == "dispDisconnected",
      str({k: _sf._det[k].cget("text") for k in _sf._det}))

# Force Logout: confirm first, honor Cancel, audit + close on OK
conn = database.get_connection()
conn.execute("DELETE FROM client_sessions WHERE session_id='sessFLB'")
conn.execute(
    "INSERT INTO client_sessions (session_id, pc_name, student_id,"
    " full_name, login_time, status) VALUES"
    " ('sessFLB','OFFLINE-FLB','fltest01','Forced Tester',?, 'Active')",
    (now_datetime(),))
conn.commit()
conn.close()
_sf.refresh()
_sel = [i for i in _sf.tree.get_children()
        if str(_sf.tree.item(i, "values")[1]) == "fltest01"]
_sf.tree.selection_set(_sel[0])
_sf._on_select()
import tkinter.messagebox as _mb
_orig_yesno = _mb.askyesno
_orig_toast2 = dash.toast
_orig_notify2 = _sf.notify
_toasts2 = []
dash.toast = lambda msg, kind=None: _toasts2.append((kind, msg))
_sf.notify = lambda msg, kind=None: _toasts2.append((kind, msg))
_audit_fl = database.get_connection().execute(
    "SELECT COUNT(*) FROM admin_activity_log"
    " WHERE action='force_logout' AND target='fltest01'").fetchone()[0]
_mb.askyesno = lambda *a, **k: False
_res = _sf.force_logout()
_row_fl = database.get_connection().execute(
    "SELECT status FROM client_sessions WHERE session_id='sessFLB'"
).fetchone()
check("declined Force Logout leaves the session open",
      _res is None and _row_fl["status"] == "Active"
      and database.get_connection().execute(
          "SELECT COUNT(*) FROM admin_activity_log"
          " WHERE action='force_logout' AND target='fltest01'"
      ).fetchone()[0] == _audit_fl,
      str(_row_fl["status"] if _row_fl else None))
_mb.askyesno = lambda *a, **k: True
_res = _sf.force_logout()
_row_fl = database.get_connection().execute(
    "SELECT status, logout_time FROM client_sessions"
    " WHERE session_id='sessFLB'").fetchone()
check("confirmed Force Logout closes the session",
      isinstance(_res, dict) and _res.get("forced") == 1
      and _row_fl["status"] == "Forced Logout" and _row_fl["logout_time"],
      str((_res, dict(_row_fl) if _row_fl else None)))
check("Force Logout is audited (who / what / how many)",
      database.get_connection().execute(
          "SELECT COUNT(*) FROM admin_activity_log"
          " WHERE action='force_logout' AND target='fltest01'"
      ).fetchone()[0] == _audit_fl + 1, "force_logout row")
check("Force Logout reports back in a toast",
      any("Force logout" in str(m) for _k, m in _toasts2), str(_toasts2))
_mb.askyesno = _orig_yesno
dash.toast = _orig_toast2
_sf.notify = _orig_notify2

# View Activity jumps to the Audit Trail pre-filtered on that account
_sf.tree.selection_set(_sel[0])
_sf._on_select()
_sf.view_activity()
check("View Activity opens the Audit Trail pre-filtered",
      dash.current_page == "activity"
      and dash.audit_search_var.get() == "fltest01",
      f"page={dash.current_page} search={dash.audit_search_var.get()!r}")
dash.show_page("sessions")

# cleanup: the display + force-logout fixtures never outlive this block
conn = database.get_connection()
conn.execute("DELETE FROM client_sessions WHERE student_id IN"
             " ('disp001','fltest01')")
conn.commit()
conn.close()
_sf.refresh()
if _orig_page:
    dash.show_page(_orig_page)        # leave the dashboard where we found it

# ---- redesign (Phase C): Accounts = search -> select -> Account Details ---
import tkinter.messagebox as _mb
_orig_yesno_c = _mb.askyesno
_orig_toast_c = dash.toast
_acf = dash.students_frame
_acf.clear_filters()
_acf.refresh()

# fixtures: one account that is never in a session (add/edit/delete) and one
# that is (cards + Session column + details window)
conn = database.get_connection()
conn.execute("DELETE FROM users WHERE student_id IN"
             " ('accnew1','accblank1','accedit1','accdel1','accsess1')")
for _sid, _name, _st in (("accedit1", "Edit Fixture", "Active"),
                         ("accdel1", "Delete Fixture", "Active"),
                         ("accsess1", "Session Fixture", "Inactive")):
    conn.execute(
        "INSERT INTO users (student_id, password, full_name, role, course,"
        " year_level, email, contact, status, must_change_password)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (_sid, database.hash_password("orig-pass"), _name, "student",
         "BSIT", "2nd Year", f"{_sid}@mail.test", "0999", _st, 1))
conn.execute("DELETE FROM client_sessions WHERE student_id IN"
             " ('accedit1','accsess1')")
conn.execute(
    "INSERT INTO client_sessions (session_id, pc_name, student_id, full_name,"
    " login_time, status, connection_type) VALUES"
    " ('sessACC01','ACC-NOPE-PC','accsess1','Session Fixture', ?,"
    " 'Active','Wi-Fi')", (now_datetime(),))
conn.execute(
    "INSERT INTO client_sessions (session_id, pc_name, student_id, full_name,"
    " login_time, logout_time, duration_seconds, status) VALUES"
    " ('sessACC02','ACC-NOPE-PC','accedit1','Edit Fixture',"
    " '2026-01-02 08:00:00','2026-01-02 09:00:00',3600,'Completed')")
conn.execute(
    "INSERT INTO client_sessions (session_id, pc_name, student_id, full_name,"
    " login_time, logout_time, duration_seconds, status) VALUES"
    " ('sessACC03','ACC-NOPE-2','accedit1','Edit Fixture',"
    " '2026-01-03 08:00:00','2026-01-03 08:30:00',1800,'Completed')")
conn.execute(
    "INSERT INTO admin_activity_log (admin_user, action, target, details,"
    " timestamp) VALUES ('admin','account_update','accedit1',"
    " 'edited from the Accounts page','2026-01-02 03:04:05')")
conn.execute(
    "INSERT INTO client_logs (event_id, pc_name, user_id, severity, category,"
    " message, detail, created_at) VALUES"
    " ('accLOG1','ACC-NOPE-PC','accedit1','INFO','security','logged in','',"
    " '2026-01-02 03:04:06')")
conn.commit()
conn.close()
_acf.refresh()

check("Accounts page has 4 summary cards",
      set(getattr(_acf, "_card_vals", {})) ==
      {"total", "active", "disabled", "insession"},
      str(sorted(getattr(_acf, "_card_vals", {}))))
check("Accounts summary card values are numbers",
      all(str(_acf._card_vals[k].cget("text")).isdigit()
          for k in _acf._card_vals),
      str({k: _acf._card_vals[k].cget("text") for k in _acf._card_vals}))
check("Accounts table never exposes the password",
      "password" not in [str(c) for c in _acf.tree["columns"]]
      and "password" not in [str(_acf.tree.heading(c, "text")).lower()
                             for c in _acf.tree["columns"]],
      str(list(_acf.tree["columns"])))
check("Accounts page has Search/Status/Course filters + Clear",
      all(hasattr(_acf, a) for a in
          ("search_var", "status_var", "course_var", "clear_filters")),
      str(sorted(a for a in ("search_var", "status_var", "course_var",
                             "clear_filters") if hasattr(_acf, a))))
check("Accounts page has the contextual action buttons",
      all(t in _all_button_texts(_acf)
          for t in ("+ Add Account", "Bulk Upload", "View Details",
                    "Edit Selected", "Delete Selected", "Refresh",
                    "Export CSV", "Clear")),
      str(_all_button_texts(_acf)))

_acr = _trows(_acf.tree)
_ids = _acf.tree.get_children()
_sid_iid = {str(_acf.tree.item(i, "values")[0]): i for i in _ids}
check("both account fixtures are listed",
      "accedit1" in _sid_iid and "accsess1" in _sid_iid
      and "accdel1" in _sid_iid, str(sorted(_sid_iid)))
check("Session column marks the account that is in a session",
      str(_acf.tree.item(_sid_iid["accsess1"], "values")[7]) == "ACC-NOPE-PC"
      and str(_acf.tree.item(_sid_iid["accdel1"], "values")[7]) == "—",
      f"{_acf.tree.item(_sid_iid['accsess1'], 'values')[7]!r}"
      f" / {_acf.tree.item(_sid_iid['accdel1'], 'values')[7]!r}")
check("Accounts in Session card counts open sessions",
      int(_acf._card_vals["insession"].cget("text")) ==
      database.get_connection().execute(
          "SELECT COUNT(DISTINCT student_id) FROM client_sessions"
          " WHERE status='Active'").fetchone()[0],
      _acf._card_vals["insession"].cget("text"))
check("Disabled card matches the non-Active accounts",
      int(_acf._card_vals["disabled"].cget("text")) ==
      database.get_connection().execute(
          "SELECT COUNT(*) FROM users WHERE role='student'"
          " AND status != 'Active'").fetchone()[0],
      _acf._card_vals["disabled"].cget("text"))

# ---- Status / Course filters + Clear --------------------------------------
_acf.status_var.set("DISABLED")
_rows = [str(v[0]) for v in _trows(_acf.tree)]
check("Status=DISABLED shows only disabled accounts",
      "accsess1" in _rows and "accedit1" not in _rows, str(_rows))
_acf.status_var.set("ACTIVE")
_rows = [str(v[0]) for v in _trows(_acf.tree)]
check("Status=ACTIVE hides the disabled account",
      "accedit1" in _rows and "accsess1" not in _rows, str(_rows))
_acf.status_var.set("All")
_acf.course_var.set("BSIT")
_rows = [str(v[0]) for v in _trows(_acf.tree)]
check("Course filter narrows the accounts table",
      "accedit1" in _rows, str(_rows))
_acf.search_var.set("no-such-acct-anywhere")
check("search + filters combine (0 rows)", len(_trows(_acf.tree)) == 0,
      str(len(_trows(_acf.tree))))
_base_ac = len(_trows(_acf.tree))          # 0 while the search is nonsense
_acf.clear_filters()
check("Clear resets every filter at once",
      _acf.search_var.get() == "" and _acf.status_var.get() == "All"
      and _acf.course_var.get() == "All"
      and len(_trows(_acf.tree)) > _base_ac,
      f"{_acf.search_var.get()!r} {_acf.status_var.get()!r}"
      f" {_acf.course_var.get()!r} rows={len(_trows(_acf.tree))}")

# ---- Add Account dialog: validation, hashing, flags, live-sync ------------
_toasts_ac, _changes_ac = [], []
_acf.notify = lambda t, k="info": _toasts_ac.append((k, t))
dash.toast = lambda t, k="info": _toasts_ac.append((k, t))
_acf.open_add()
check("Add Account dialog collects the account fields",
      _acf._form is not None and set(_acf._form_vars) ==
      {"student_id", "password", "full_name", "course", "year_level",
       "email", "contact", "status"},
      str(sorted(_acf._form_vars)))
_acf._form_vars["student_id"].set("accnew1")
_ok = _acf._save_form()
check("Add requires a full name",
      _ok is False and _acf._form is not None
      and any("Please fill in" in str(t) for _k, t in _toasts_ac),
      str(_toasts_ac))
_acf._form_vars["full_name"].set("New Account")
_acf._form_vars["password"].set("brand-new")
_acf._form_vars["course"].set("BSIT")
_acf._form_vars["year_level"].set("3rd Year")
_ok = _acf._save_form()
_new_row = database.get_connection().execute(
    "SELECT password, role, must_change_password, full_name, status"
    " FROM users WHERE student_id='accnew1'").fetchone()
check("saving creates the account with UserCRUDFrame semantics",
      _ok is True and _acf._form is None and _new_row is not None
      and _new_row["role"] == "student"
      and _new_row["must_change_password"] == 1
      and "$" in _new_row["password"]
      and _new_row["password"] != "brand-new"
      and _new_row["full_name"] == "New Account"
      and _new_row["status"] == "Active",
      str(dict(_new_row) if _new_row else None))
check("the new account never reports its password back",
      all("brand-new" not in str(t) for _k, t in _toasts_ac),
      str(_toasts_ac))
check("add reports through the account_changed hook (toast)",
      any("Account created" in str(t) for _k, t in _toasts_ac),
      str(_toasts_ac))
check("the new account appears in the table",
      "accnew1" in [str(v[0]) for v in _trows(_acf.tree)],
      str([str(v[0]) for v in _trows(_acf.tree)]))

# ---- P2-10: blank password on ADD falls back to the student default ------
_toasts_ac.clear()
_acf.open_add()
_acf._form_vars["student_id"].set("accblank1")
_acf._form_vars["full_name"].set("Blank Password Account")
_ok = _acf._save_form()
_row_blank = database.get_connection().execute(
    "SELECT password, role, must_change_password FROM users"
    " WHERE student_id='accblank1'").fetchone()
check("a blank password on add uses the student default",
      _ok is True and _row_blank is not None
      and _row_blank["role"] == "student"
      and _row_blank["must_change_password"] == 1
      and database.verify_password(database.DEFAULT_CLIENT_PASSWORD,
                                   _row_blank["password"]),
      str(dict(_row_blank) if _row_blank else None))
check("the default is stored hashed, never in clear text",
      _row_blank is not None
      and _row_blank["password"] != database.DEFAULT_CLIENT_PASSWORD
      and "$" in str(_row_blank["password"]),
      str(_row_blank["password"] if _row_blank else None))
check("the blank add never reports the default password back",
      all(database.DEFAULT_CLIENT_PASSWORD not in str(t)
          for _k, t in _toasts_ac)
      and any("Account created" in str(t) for _k, t in _toasts_ac),
      str(_toasts_ac))

# ---- Edit: blank keeps the password, a new one is hashed -----------------
_toasts_ac.clear()
_acf.search_var.set("accedit1")
_acf.tree.selection_set(_acf.tree.get_children()[0])
_acf._on_select()
_row_e = _acf._selection_row()
_old_hash = database.get_connection().execute(
    "SELECT password FROM users WHERE student_id='accedit1'"
    ).fetchone()["password"]
_acf.open_edit(_row_e)
check("Edit dialog pre-fills the account but never the password",
      _acf._form is not None
      and _acf._form_vars["full_name"].get() == "Edit Fixture"
      and _acf._form_vars["status"].get() == "Active"
      and _acf._form_vars["password"].get() == "",
      f"{_acf._form_vars['full_name'].get()!r}"
      f" {_acf._form_vars['password'].get()!r}")
_acf._form_vars["full_name"].set("Renamed Fixture")
_ok = _acf._save_form()
_row_e2 = database.get_connection().execute(
    "SELECT password, full_name FROM users WHERE student_id='accedit1'"
    ).fetchone()
check("blank password keeps the stored hash on save",
      _ok is True and _row_e2["full_name"] == "Renamed Fixture"
      and _row_e2["password"] == _old_hash,
      str(dict(_row_e2)))
_acf.search_var.set("accedit1")
_acf.tree.selection_set(_acf.tree.get_children()[0])
_acf._on_select()
_acf.open_edit(_acf._selection_row())
_old_hash = _row_e2["password"]
_acf._form_vars["password"].set("brand-new-2")
_ok = _acf._save_form()
_row_e3 = database.get_connection().execute(
    "SELECT password FROM users WHERE student_id='accedit1'").fetchone()
check("a new password is hashed on save",
      _ok is True and _row_e3["password"] != _old_hash
      and _row_e3["password"] != "brand-new-2"
      and "$" in _row_e3["password"], str(_row_e3["password"]))

# ---- Account Details window (Overview/Sessions/Activity Logs/PC Usage) ---
_acf.search_var.set("accedit1")
_acf.tree.selection_set(_acf.tree.get_children()[0])
_acf._on_select()
_dw = _acf.open_details()
_tabtexts = ([str(_acf.details_tabs.tab(t, "text"))
              for t in _acf.details_tabs.tabs()] if _acf.details_tabs
             else [])
check("Account Details opens with 4 tabs",
      _dw is not None and len(_tabtexts) == 4
      and _tabtexts == ["Overview", "Sessions", "Activity Logs", "PC Usage"],
      str(_tabtexts))
_ov = {k: str(v.cget("text")) for k, v in _acf._detail_labels.items()}
check("Overview shows the account (never the password)",
      _ov.get("student_id") == "accedit1"
      and _ov.get("full_name") == "Renamed Fixture"
      and _ov.get("role") == "student"
      and _ov.get("_status") == "ACTIVE"
      and _ov.get("_mcp") == "Yes"
      and not any("$" in v for v in _ov.values()), str(_ov))
check("Sessions tab lists that account's sessions",
      len(_acf._detail_session_tree.get_children()) == 2,
      str(len(_acf._detail_session_tree.get_children())))
check("Activity Logs tab merges audit rows and client events",
      len(_acf._detail_activity_tree.get_children()) >= 2,
      str(len(_acf._detail_activity_tree.get_children())))
check("PC Usage tab groups sessions per PC",
      len(_acf._detail_usage_tree.get_children()) == 2,
      str([_acf._detail_usage_tree.item(i, "values")
           for i in _acf._detail_usage_tree.get_children()]))
_acf.close_details()
check("the details window closes cleanly",
      _acf.details_win is None, str(_acf.details_win))

# ---- Delete: declined keeps the account, confirmed removes it ------------
_acf.search_var.set("accdel1")
_acf.tree.selection_set(_acf.tree.get_children()[0])
_acf._on_select()
_ans_ac = [False]
_mb.askyesno = lambda *a, **k: _ans_ac[0]
_kept = _acf.delete_selected()
_kept_row = database.get_connection().execute(
    "SELECT id FROM users WHERE student_id='accdel1'").fetchone()
check("declined delete keeps the account",
      _kept is False and _kept_row is not None,
      str(dict(_kept_row) if _kept_row else None))
_ans_ac[0] = True
_gone = _acf.delete_selected()
_gone_row = database.get_connection().execute(
    "SELECT id FROM users WHERE student_id='accdel1'").fetchone()
check("confirmed delete removes the account",
      _gone is True and _gone_row is None, str(_gone))
_mb.askyesno = _orig_yesno_c
check("delete reports through the account_changed hook (toast)",
      any("Account delete" in str(t) for _k, t in _toasts_ac),
      str(_toasts_ac))

# ---- Bulk Upload: CSV import - validation, dedupe, default password ------
import csv as _csv
_bulk_dir = tempfile.mkdtemp(prefix="bulk_csv_")
_bulk_path = os.path.join(_bulk_dir, "accounts.csv")
with open(_bulk_path, "w", newline="", encoding="utf-8") as _fh:
    _bw = _csv.writer(_fh)
    _bw.writerow(["Student ID", "Full Name", "Password", "Course",
                  "Year Level", "Email", "Contact", "Status"])
    _bw.writerow(["bulk1", "Bulk One", "", "BSIT", "1st Year", "", "", ""])
    _bw.writerow(["bulk2", "Bulk Two", "bulk-pw-2", "", "", "", "",
                  "inactive"])
    _bw.writerow(["bulk1", "Dup In File", "", "", "", "", "", ""])
    _bw.writerow(["2023-00001", "Already Here", "", "", "", "", "", ""])
    _bw.writerow(["", "Missing ID", "", "", "", "", "", ""])
    _bw.writerow(["bulk3", "", "", "", "", "", "", ""])
    _bw.writerow(["", "", "", "", "", "", "", ""])   # filler: not counted
_toasts_ac.clear()
_bulk = _acf.import_accounts_csv(_bulk_path)
_bulk_rows = {r["student_id"]: dict(r) for r in database.get_connection(
).execute(
    "SELECT student_id, password, role, must_change_password, status,"
    " full_name FROM users WHERE student_id IN ('bulk1','bulk2')")}
check("bulk upload adds valid rows and counts skips + errors",
      isinstance(_bulk, dict)
      and _bulk["added"] == ["bulk1", "bulk2"]
      and len(_bulk["skipped"]) == 2 and len(_bulk["errors"]) == 2
      and any("Account created" in str(t) for _k, t in _toasts_ac),
      str(_bulk))
check("bulk rows keep the add-form write rules (default password, flag)",
      set(_bulk_rows) == {"bulk1", "bulk2"}
      and all(r["role"] == "student" and r["must_change_password"] == 1
              for r in _bulk_rows.values())
      and database.verify_password(database.DEFAULT_CLIENT_PASSWORD,
                                   _bulk_rows["bulk1"]["password"])
      and database.verify_password("bulk-pw-2",
                                   _bulk_rows["bulk2"]["password"])
      and _bulk_rows["bulk1"]["status"] == "Active"
      and _bulk_rows["bulk2"]["status"] == "Inactive"
      and _bulk_rows["bulk2"]["full_name"] == "Bulk Two",
      str(_bulk_rows))
check("bulk summary + toasts never leak a password",
      all(database.DEFAULT_CLIENT_PASSWORD not in s
          and "bulk-pw-2" not in s
          for s in (_bulk["added"] + _bulk["skipped"] + _bulk["errors"]))
      and all(database.DEFAULT_CLIENT_PASSWORD not in str(t)
              and "bulk-pw-2" not in str(t) for _k, t in _toasts_ac),
      str(_bulk))
check("bulk upload leaves the existing account untouched",
      database.get_connection().execute(
          "SELECT full_name FROM users WHERE student_id='2023-00001'"
      ).fetchone()["full_name"] == "Juan Dela Cruz",
      "2023-00001 full name checked")
_bad_path = os.path.join(_bulk_dir, "bad.csv")
with open(_bad_path, "w", newline="", encoding="utf-8") as _fh:
    _bw = _csv.writer(_fh)
    _bw.writerow(["Name", "Marks"])
    _bw.writerow(["bulk9", "x"])
_toasts_ac.clear()
_bad = _acf.import_accounts_csv(_bad_path)
_bad_row = database.get_connection().execute(
    "SELECT 1 FROM users WHERE student_id='bulk9'").fetchone()
check("a CSV without the required columns is refused, nothing imported",
      _bad is None and _bad_row is None
      and any("header" in str(t) for _k, t in _toasts_ac),
      str(_toasts_ac))
# bulk fixtures never outlive this block
conn = database.get_connection()
conn.execute("DELETE FROM users WHERE student_id IN ('bulk1','bulk2')")
conn.commit()
conn.close()

# cleanup: the account fixtures never outlive this block
conn = database.get_connection()
conn.execute("DELETE FROM users WHERE student_id IN"
             " ('accnew1','accblank1','accedit1','accdel1','accsess1')")
conn.execute("DELETE FROM client_sessions WHERE student_id IN"
             " ('accedit1','accsess1')")
conn.execute("DELETE FROM admin_activity_log WHERE target='accedit1'")
conn.execute("DELETE FROM client_logs WHERE event_id='accLOG1'")
conn.commit()
conn.close()
_acf.notify = _orig_toast_c
dash.toast = _orig_toast_c
_acf.clear_filters()
_acf.refresh()

# Audit trail (events view; webfilter_change rows are recorded above)
dash.audit_date_var.set("")
dash.audit_status_var.set("All")
dash.audit_type_var.set("All Types")
dash._set_audit_mode("events")
_instant("Audit page filters instantly as you type",
         dash.audit_tree, dash.audit_search_var, "webfilter")

# Inventory (fixture row so the needle is fully deterministic)
conn = database.get_connection()
conn.execute(
    "INSERT INTO inventory (item_name, category, quantity, available_qty,"
    " assigned_qty, condition_status, status, location, notes,"
    " date_added, last_updated)"
    " VALUES ('Instant Filter Item','Test',1,1,0,'Good','AVAILABLE',"
    " 'Bench 9','','2026-01-01','2026-01-01')")
conn.commit()
conn.close()
inv.refresh()
_instant("Inventory page filters instantly as you type",
         inv.tree, inv.search_var, "instant")
conn = database.get_connection()
conn.execute("DELETE FROM inventory WHERE item_name='Instant Filter Item'")
conn.commit()
conn.close()
inv.refresh()

# Website Access (unified rules table)
conn = database.get_connection()
conn.execute("INSERT OR REPLACE INTO websites"
             " (domain, list_type, enabled) VALUES"
             " ('social.test','blocked',1)")
conn.execute("INSERT OR REPLACE INTO websites"
             " (domain, list_type, enabled) VALUES"
             " ('zzinstant.test','blocked',1)")
conn.commit()
conn.close()
dash.web_rules.refresh()
_instant("Website rules filter instantly as you type",
         dash.web_rules.tree, dash.web_rules.search_var, "social")
conn = database.get_connection()
conn.execute("DELETE FROM websites"
             " WHERE domain IN ('social.test','zzinstant.test')")
conn.commit()
conn.close()
dash.web_rules.refresh()

# Dashboard PC grid (cards): same instant behaviour on the overview page
dash._search_focus_in()                 # clears the "Search PCs..." hint
_cards0 = list(dash._pc_cards.keys())
dash.pc_search_var.set("zzzz-no-pc-here")
_cards_none = list(dash._pc_cards.keys())
dash.pc_search_var.set((_cards0[0] if _cards0 else "TEST-PC").lower())
_cards_hit = list(dash._pc_cards.keys())
dash.pc_search_var.set("")
_cards_back = list(dash._pc_cards.keys())
dash._search_focus_out()                # restore the idle hint
check("Dashboard PC grid filters instantly as you type",
      len(_cards0) > 0 and len(_cards_none) == 0
      and 1 <= len(_cards_hit) <= len(_cards0)
      and len(_cards_back) == len(_cards0),
      f"base={len(_cards0)} none={len(_cards_none)} "
      f"hit={len(_cards_hit)} back={len(_cards_back)}")

# selecting a PC enables the controls
first_item = dash.client_tree.get_children()[0]
dash.client_tree.selection_set(first_item)
dash._on_client_select()
check("selecting a PC enables controls",
      all(str(b.cget("state")) == "normal" for b in dash._ctrl_btns))
# Task 5: [ 🖱 Remote ] lives with the other PC controls but is stricter -
# it needs exactly one selected AND online PC.
_rb = getattr(dash, "_remote_btn", None)
check("remote button sits with the PC controls",
      _rb is not None and "Remote" in str(_rb.cget("text"))
      and _rb in dash._ctrl_btns,
      str([str(b.cget("text")) for b in dash._ctrl_btns]))
check("send file button sits with the PC controls",
      any("Send File" in str(b.cget("text")) for b in dash._ctrl_btns)
      and str([str(b.cget("text")) for b in dash._ctrl_btns
               if "Send File" in str(b.cget("text"))][0]).endswith("Send File"),
      str([str(b.cget("text")) for b in dash._ctrl_btns]))
_sel_pc = (dash._selected_pcs or [None])[0]
check("remote button state follows the selected PC's online state",
      _rb is not None and _sel_pc is not None
      and str(_rb.cget("state")) ==
      ("normal" if dash._pc_online(_sel_pc) else "disabled"),
      f"state={_rb and _rb.cget('state')} "
      f"online={dash._pc_online(_sel_pc) if _sel_pc else None}")
_observe_btn = [b for b in dash._ctrl_btns
                if "\U0001f441" in str(b.cget("text"))]
check("[Observe] stays a separate, view-only control",
      len(_observe_btn) == 1 and _observe_btn[0] is not _rb
      and callable(getattr(dash, "_observe", None)),
      str([str(b.cget("text")) for b in _observe_btn]))

# ---- PC grid, unified selection, details panel (redesign) ----
first_pc = str(dash.client_tree.item(first_item, "values")[0])
check("unified selection model",
      dash._selected_pc() == first_pc and dash._selected_pcs == [first_pc],
      str(dash._selected_pcs))
check("Selected counter updated",
      any("Selected: 1" in str(l.cget("text")) for l in dash._sel_labels),
      str([l.cget("text") for l in dash._sel_labels]))
check("overview PC grid rendered", len(dash._pc_cards) >= 1,
      str(list(dash._pc_cards)))
card_texts = [str(l.cget("text"))
              for l in dash._pc_cards[first_pc]["frame"].winfo_children()
              if _is_label(l)]
check("PC card shows status + IP + user",
      any(t.startswith("IP:") for t in card_texts)
      and any(t.startswith("User:") for t in card_texts)
      and any(t in ("ONLINE", "OFFLINE", "IN USE", "PAUSED", "LOCKED",
                    "AVAILABLE", "VERIFYING", "UNKNOWN") for t in card_texts),
      str(card_texts))
check("[card] PC card shows the person's full name, not the login id",
      dash._pc_user_label({"account_full_name": "Juan Dela Cruz",
                           "logged_in_user": "2023-00001"})
      == "Juan Dela Cruz"
      and dash._pc_user_label({"logged_in_user": "2023-00001"})
      == "Juan Dela Cruz"           # users-row fallback for offline PCs
      and dash._pc_user_label({"logged_in_user": "ghost42"}) == "ghost42"
      and dash._pc_user_label({}) == "None",
      str(dash._pc_user_label({"account_full_name": "Juan Dela Cruz",
                               "logged_in_user": "2023-00001"})))
check("grid card icon keeps its square size",
      dash._pc_cards[first_pc]["icon"] is not None
      and dash._pc_cards[first_pc]["icon"].width() == 44
      and dash._pc_cards[first_pc]["icon"].height() == 44)
check("client tree rows carry a status icon",
      bool(dash.client_tree.item(first_item, "image")),
      str(dash.client_tree.item(first_item, "image")))
root.update()   # pump MapNotify: winfo_ismapped stays 0 after
                # update_idletasks() alone for a freshly packed panel
check("details panel revealed on selection",
      any(p["frame"].winfo_ismapped() for p in dash.detail_panels),
      str([(p["frame"].winfo_ismapped(), p["visible"])
           for p in dash.detail_panels]))
detail_text = " ".join(str(l.cget("text"))
                       for p in dash.detail_panels
                       for l in p["labels"].values()).lower()
check("details show the selected PC", first_pc.lower() in detail_text,
      detail_text[:160])
ov_panel = dash.detail_panels[0]
check("details panel sits on the right side",
      ov_panel["frame"].grid_info().get("column") == 1,
      str(ov_panel["frame"].grid_info()))
check("PC info fields include heartbeat + client version",
      {"hb", "cver"} <= set(ov_panel["labels"]),
      str(sorted(ov_panel["labels"])))
check("current-user fields include first/last name + role",
      {"fname", "lname", "role", "logout", "curpc"} <= set(ov_panel["labels"]),
      str(sorted(ov_panel["labels"])))
check("details never expose passwords (2nd pass)",
      "password" not in detail_text and "123" not in detail_text,
      detail_text[:160])
check("details never expose passwords",
      "password" not in detail_text and "admin123" not in detail_text
      and "student123" not in detail_text, detail_text[:160])
dash._select_all()
check("select all selects the whole grid",
      len(dash._selected_pcs) == len(dash._pc_cards)
      and len(dash._selected_pcs) >= 1, str(dash._selected_pcs))
# Task 5: Remote drives ONE machine - a multi-selection must disable it
# even though every other control stays enabled.
_prev_sel = list(dash._selected_pcs)
dash._selected_pcs = (_prev_sel or ["TEST-PC"]) + ["OTHER-PC-1", "OTHER-PC-2"]
dash._update_selection_ui()
check("remote button disabled when several PCs are selected",
      str(dash._remote_btn.cget("state")) == "disabled"
      and all(str(b.cget("state")) == "normal" for b in dash._ctrl_btns
              if b is not dash._remote_btn),
      str(dash._remote_btn.cget("state")))
dash._selected_pcs = _prev_sel
dash._update_selection_ui()
dash._clear_selection()
check("clear empties the selection and disables controls",
      not dash._selected_pcs
      and all(str(b.cget("state")) == "disabled" for b in dash._ctrl_btns))

# ---- Restart / Shutdown: explicit confirmation, never auto-trigger ------
# _restart() and _shutdown() both funnel through _bulk(), which asks the
# operator first and issues NOTHING when they decline.  This is the UI
# half of the rule; the Client's one-shot _power_token (which makes an
# unsolicited power action impossible even if a command arrived) is
# signed off in the P1.5 block below.  Nothing here can restart a real
# machine: the target is an OFFLINE PC, so an accepted confirmation still
# resolves to OFFLINE rather than to a command on the wire.
def _pwr_rows():
    return database.get_connection().execute(
        "SELECT COUNT(*) AS n FROM admin_activity_log "
        "WHERE action IN ('BULK_RESTART','BULK_SHUTDOWN')").fetchone()["n"]


_pwr_orig_yesno = _mb.askyesno
_pwr_orig_toast = dash.toast
_pwr_asked = []
dash.toast = lambda msg, kind=None: None
try:
    dash._selected_pcs = ["GONE-PC"]              # offline: never sent
    _pwr0 = _pwr_rows()
    # decline: the dialog is asked for both, and neither sends anything
    _mb.askyesno = lambda *a, **k: (_pwr_asked.append(a), False)[1]
    dash._restart()
    dash._shutdown()
    check("Restart and Shutdown each ask for confirmation first",
          len(_pwr_asked) == 2, str(_pwr_asked))
    check("declining Restart or Shutdown sends nothing at all",
          _pwr_rows() == _pwr0, f"{_pwr0} -> {_pwr_rows()}")
    # accept: one ask, and only then does the command path run
    _pwr_asked.clear()
    _mb.askyesno = lambda *a, **k: (_pwr_asked.append(a), True)[1]
    dash._restart()
    _t = time.time() + 5
    while time.time() < _t and _pwr_rows() == _pwr0:
        time.sleep(0.1)
    check("accepting Restart proceeds after exactly one confirmation",
          len(_pwr_asked) == 1 and _pwr_rows() == _pwr0 + 1,
          f"asked={len(_pwr_asked)} rows={_pwr0}->{_pwr_rows()}")
    # the dialog exists because these cannot be undone - not as a
    # general prompt, so every other bulk action stays toast-confirmed
    _pwr_asked.clear()
    _mb.askyesno = lambda *a, **k: (_pwr_asked.append(a), True)[1]
    dash._bulk("lock")
    check("a non-destructive bulk action does not stop to ask",
          len(_pwr_asked) == 0, str(_pwr_asked))
    time.sleep(0.5)                 # let the offline workers finish
finally:
    _mb.askyesno = _pwr_orig_yesno
    dash.toast = _pwr_orig_toast
    dash._selected_pcs = []
    try:
        dash._update_selection_ui()
    except Exception:
        pass

# The crash escalation is the ONE place the Server decides something on
# its own after a timeout.  It may write an event; it must never reach
# for power, so a PC that vanished is logged and left alone.
def _crash_rows():
    return database.get_connection().execute(
        "SELECT COUNT(*) AS n FROM admin_activity_log "
        "WHERE action='client_crash'").fetchone()["n"]


_pwr_a, _crash_a = _pwr_rows(), _crash_rows()
srv._escalate_disconnect("GHOST-PC")
check("an unanswered disconnect is logged, never turned into a power action",
      _pwr_rows() == _pwr_a and _crash_rows() == _crash_a + 1,
      f"power={_pwr_a}->{_pwr_rows()} crash={_crash_a}->{_crash_rows()}")

dash.destroy()

staff = AdminDashboard(root, {"id": 2, "student_id": "staff",
                              "full_name": "Staff", "role": "staff"},
                       lambda: None, server=None, server_events=queue.Queue())
root.update()
check("staff without server has no Client PCs menu entry",
      "clients" not in staff._sidebar_btns)
check("staff has no Staff-accounts page",
      "staff" not in staff._sidebar_btns)
check("staff still gets students/computers pages",
      "students" in staff._sidebar_btns and "computers" in staff._sidebar_btns)
staff.destroy()

mdash = AdminDashboard(root, {"id": 9, "student_id": "maint01",
                              "full_name": "Lab Technician",
                              "role": "maintenance"},
                       lambda: None, server=None, server_events=queue.Queue())
root.update()
check("maintenance dashboard builds as non-admin",
      not mdash.is_admin
      and mdash.title() == "Laboratory System - Maintenance"
      and "staff" not in mdash._sidebar_btns, mdash.title())
check("maintenance keeps staff-side pages",
      "students" in mdash._sidebar_btns
      and "computers" in mdash._sidebar_btns)
mdash.destroy()

stu = StudentDashboard(root, {"student_id": "2023-00001", "full_name": "Juan",
                              "role": "student", "course": "BS CS",
                              "year_level": "3rd"}, lambda: None)
root.update()
check("student dashboard builds", stu.winfo_exists())
check("student Lab Attendance tab removed", not hasattr(stu, "att_tree"))
check("student keeps messages/borrow pages",
      hasattr(stu, "msg_tree") and hasattr(stu, "borrow_tree"))
check("row accessor tolerant",
      row_g({"a": 1}, "a") == 1 and row_g({}, "x", "d") == "d")
stu.destroy()
root.destroy()

# ---------------------------------------------------------------- client mod
import client as client_mod
import client_api
check("client module imports", hasattr(client_mod, "ClientApp"))
check("client role labels cover maintenance",
      client_mod.ROLE_LABELS.get("maintenance") == "MAINTENANCE",
      str(client_mod.ROLE_LABELS))
# spec 14: Ethernet + Wi-Fi reporting - pure classifier rules
check("LAN classifier prefers Wi-Fi",
      client_mod._classify_lan(["wi-fi", "ethernet"]) == "Wi-Fi"
      and client_mod._classify_lan(["ethernet", "wireless"]) == "Wi-Fi")
check("LAN classifier recognises Ethernet",
      client_mod._classify_lan(["ethernet 3",
                                "local area connection*2"]) == "Ethernet")
check("LAN classifier falls back to LAN",
      client_mod._classify_lan(["some tap adapter"]) == "LAN"
      and client_mod._classify_lan([]) == "LAN")
check("get_connection_type returns a known medium",
      client_mod.get_connection_type() in ("Ethernet", "Wi-Fi", "LAN"),
      client_mod.get_connection_type())
check("client has no spec collector", not hasattr(client_mod, "collect_specs"))
check("client_api fallback works", client_api.fetch_announcements().get("ok") is True)
check("client_api attendance API removed", not hasattr(client_api, "fetch_attendance"))
# power safety: _os_power refuses anything that was not armed
class _Fake:
    _power_token = None
try:
    client_mod.ClientApp._os_power(_Fake(), "shutdown")
    power_blocked = True
except Exception:
    power_blocked = True     # raising is fine too - it did not execute
check("unsolicited shutdown is blocked", power_blocked)

# ================================================ P1.5 SAFETY SIGN-OFF ====
# One block that proves every non-negotiable guardrail, so a later change
# that erodes one of them fails the gate instead of quietly shipping.
import io as _gio
import re as _gre

_GROOT = os.path.dirname(os.path.abspath(__file__))
_GFILES = ["client.py", "server.py", "web_access.py", "dns_filter.py",
           "protocol.py", "database.py", "local_store.py",
           "admin_dashboard.py"]
_gsrc = {f: _gio.open(os.path.join(_GROOT, f), encoding="utf-8",
                      errors="replace").read() for f in _GFILES}
_gall = "\n".join(_gsrc.values()).lower()
_gc = _gsrc["client.py"]
_gdn = _gsrc["dns_filter.py"]

# G1 - no hardware / boot / driver / service surgery anywhere in the code
_GFORBID = ["bcdedit", "set-service", "sc.exe", "pnputil", "driverquery",
            "get-pnpdevice", "disable-netadapter", "reg add", "reg.exe",
            "os.system(", "wmic", "powercfg", "win32serviceutil", "netsh",
            "taskkill", "pkill", "killall", "startupapproved"]
_gbad = [b for b in _GFORBID if b in _gall]
check("guardrail G1: no hardware/boot/driver/service surgery in the code",
      _gbad == [], str(_gbad))

# G2 - the hosts file is edited only between OUR markers, and a line the
#      administrator wrote by hand stays byte-for-byte intact
check("guardrail G2: one managed-block writer using both markers",
      _gc.count("def apply_web_filter") == 1
      and _gc.count("HOSTS_BLOCK_BEGIN") >= 2
      and _gc.count("HOSTS_BLOCK_END") >= 2,
      f"{_gc.count('def apply_web_filter')},"
      f"{_gc.count('HOSTS_BLOCK_BEGIN')},"
      f"{_gc.count('HOSTS_BLOCK_END')}")
_gf = os.path.join(tempfile.gettempdir(), "lab_hosts_guard.txt")
with open(_gf, "w", encoding="utf-8") as f:
    f.write("127.0.0.1 localhost\n0.0.0.0 user-set.example\n# a comment\n")
client_mod.apply_web_filter(["user-set.example"], hosts_path=_gf)
_gt = open(_gf, encoding="utf-8").read()
check("guardrail G2: a hand-written hosts entry survives untouched",
      _gt == "127.0.0.1 localhost\n0.0.0.0 user-set.example\n# a comment\n",
      repr(_gt))
os.remove(_gf)

# G3 - the adapter is only written by restore/repair, both of which consult
#      the recorded snapshot and the live-resolver guardrail first
_gown = []
for _m in _gre.finditer(r"_set_dns_servers\(", _gdn):
    _p = _gdn.rfind("\ndef ", 0, _m.start())
    _gown.append(_gdn[_p + 1:_gdn.find("(", _p + 1)].strip())
check("guardrail G3: adapter DNS is written only by restore and repair",
      _gown == ["def _set_dns_servers", "def restore_adapter_dns",
                "def repair_adapter_dns"]
      and _gdn.count("drop_local_dns(") >= 3
      and "probe_local_resolver(" in _gdn, str(_gown))

# G4 - a process is terminated in EXACTLY one place, by PID only: never an
#      image name, never a process tree
_gterm = [f for f in _GFILES
          if _gsrc[f].count(".terminate()") or _gsrc[f].count(".kill()")]
_gtn = sum(_gsrc[f].count(".terminate()") + _gsrc[f].count(".kill()")
           for f in _GFILES)
check("guardrail G4: only web_access.close_browser ever kills a process",
      _gterm == ["web_access.py"] and _gtn == 2, f"{_gterm},{_gtn}")

# G5 - no auto power action: _os_power is the only place that reaches the
#      OS, and only a one-shot token armed by an admin command opens it
_pi = _gc.find("def _os_power")
_si = _gc.find("def _show_popup", (_pi + 10) if _pi != -1 else 0)
_gbody = _gc[_pi:_si] if (_pi != -1 and _si != -1 and _si > _pi) else ""
_gpow = [l for l in _gc.splitlines()
         if "subprocess.Popen(" in l and "shutdown" in l]
check("guardrail G5: OS power is reached only from _os_power",
      _gc.count("def _os_power") == 1 and len(_gpow) == 3
      and all(l in _gbody for l in _gpow) and "power_token" in _gbody,
      f"{len(_gpow)},{_gc.count('def _os_power')}")


class _GPower:
    def __init__(self):
        self._power_token = None


_gcalls = []
_o_popen_g = client_mod.subprocess.Popen
client_mod.subprocess.Popen = lambda argv: _gcalls.append(list(argv))
try:
    _gp = _GPower()
    _gp._power_token = "shutdown"                     # armed by CMD_SHUTDOWN
    client_mod.ClientApp._os_power(_gp, "shutdown")   # allowed, exactly once
    client_mod.ClientApp._os_power(_gp, "shutdown")   # token is consumed
    client_mod.ClientApp._os_power(_gp, "restart")    # never armed at all
finally:
    client_mod.subprocess.Popen = _o_popen_g
check("guardrail G5: an armed power token fires exactly once",
      len(_gcalls) == 1 and _gcalls[0][:2] == ["shutdown", "/s"],
      str(_gcalls))
check("guardrail G5: an unarmed power action never fires",
      len(_gcalls) == 1, str(_gcalls))

# G6 - never claim SYNCED/enforced on a failure: "detect" is reachable only
#      after the detector install AND the live probe both really succeeded
_ppi = _gc.find("det_ok, det_err = _web_probe()")
_rdi = _gc.find('return _done((True, "detect", None))')
_gseg = _gc[_ppi:_rdi] if (_ppi != -1 and _rdi != -1 and _ppi < _rdi) else ""
check("guardrail G6: 'enforced' is only claimed after a successful probe",
      _gc.count('return _done((True, "detect", None))') == 1
      and "if not det_ok:" in _gseg,
      str(_gc.count('return _done((True, "detect", None))')))

# G7 - no persistence beyond the existing watchdog / startup entry
_gcreate = [l for l in _gc.splitlines() if '["/Create"' in l]
check("guardrail G7: persistence is limited to the existing watchdog",
      _gc.count("winreg.SetValueEx") == 1
      and _gc.count("winreg.DeleteValue") == 1
      and _gc.count('["schtasks"]') == 1
      and len(_gcreate) == 2
      and all("WATCHDOG_TASK" in l for l in _gcreate),
      str([_gc.count("winreg.SetValueEx"), len(_gcreate)]))

# G8 - the sign-off itself: every guardrail above held in THIS run
check("guardrail G8: every safety guardrail holds",
      not [f for f in FAIL if f.startswith("guardrail")],
      str([f for f in FAIL if f.startswith("guardrail")]))

# ---- standalone uninstall (dist\uninstall.exe) ---------------------------
# The uninstaller must remove EXACTLY what the Client registers, so both
# sides import the names from startup_ids - and it must never do its work
# at import time.
import startup_ids as _sids
import uninstall as _un
check("[uninstall] shares the exact auto-start ids with the Client",
      _un.WATCHDOG_TASK_LOGON == client_mod.WATCHDOG_TASK_LOGON
      == _sids.WATCHDOG_TASK_LOGON
      and _un.WATCHDOG_TASK_REPEAT == client_mod.WATCHDOG_TASK_REPEAT
      and _un.STARTUP_VALUE_NAME == client_mod.STARTUP_VALUE_NAME
      and _un.STARTUP_KEY_PATH == client_mod.STARTUP_KEY_PATH,
      f"{_un.WATCHDOG_TASK_LOGON!r}/{_un.STARTUP_VALUE_NAME!r}")
check("[uninstall] covers every artifact a Client install owns",
      _un.CLIENT_IMAGE == "client.exe"
      and "client.exe" in _un.CLIENT_FILE_PATTERNS
      and "lab_client.db*" in _un.CLIENT_FILE_PATTERNS
      and "lab_config.json" in _un.CLIENT_FILE_PATTERNS,
      str(_un.CLIENT_FILE_PATTERNS))
check("[uninstall] imports without side effects (all work lives in main)",
      callable(_un.main) and callable(_un.stop_client)
      and callable(_un.export_and_remove_files), "import-only")

# ---- watchdog: registration repairs stale / missing tasks (P1-4)-----------
# _schtasks is swapped for a stateful fake, so NO real task is ever
# created here - a real watchdog tick would open a kiosk on this desktop.
_wd_sch_orig = client_mod._schtasks
_wd_ev_orig = client_mod._watchdog_evidence
_wd_state = {}          # task name -> stored /TR (what /Create remembers)
_wd_calls = []          # every argv the code passes to schtasks
_wd_ev = []             # (severity, message) evidence records
_wd_fail_create = [False]


def _wd_fake_schtasks(args):
    args = list(args)
    _wd_calls.append(args)
    op = args[0]
    name = args[args.index("/TN") + 1] if "/TN" in args else ""
    if op == "/Query":
        if name not in _wd_state:
            return 1, "ERROR: The system cannot find the file specified."
        if "/XML" not in args:
            return 0, f"TaskName: {name}"
        tr = _wd_state[name]
        if tr.startswith('"'):
            i = tr.find('"', 1)
            exe, rest = (tr[:i + 1], tr[i + 1:].strip()) if i > 0 else (tr, "")
        else:
            p = tr.split(None, 1)
            exe, rest = p[0], (p[1] if len(p) > 1 else "")
        # the real shape: namespace + quoting kept (matches schtasks /XML)
        return 0, (
            '<?xml version="1.0" encoding="UTF-16"?>\n\n'
            '<Task version="1.2" xmlns="http://schemas.microsoft.com/'
            'windows/2004/02/mit/task">\n<Actions><Exec>'
            f"<Command>{exe}</Command><Arguments>{rest}</Arguments>"
            "</Exec></Actions>\n</Task>")
    if op == "/Create":
        if _wd_fail_create[0]:
            return 1, "ERROR: Access is denied."
        _wd_state[name] = args[args.index("/TR") + 1]
        return 0, "SUCCESS: created"
    if op == "/Delete":
        _wd_state.pop(name, None)
        return 0, "SUCCESS: deleted"
    return 1, "unexpected schtasks call"


client_mod._schtasks = _wd_fake_schtasks
client_mod._watchdog_evidence = lambda sev, msg: _wd_ev.append((sev, msg))
try:
    _wdcmd = client_mod._watchdog_command()
    # missing -> both tasks created with the right schedules and command
    _wd_calls.clear()
    _wd_state.clear()
    _wd_ev.clear()
    _ok = client_mod.register_watchdog()
    _creates = [c for c in _wd_calls if c[0] == "/Create"]
    check("[watchdog] missing tasks are (re)created with this app's command",
          _ok is True and len(_creates) == 2
          and {c[c.index("/TN") + 1] for c in _creates}
          == {client_mod.WATCHDOG_TASK_LOGON, client_mod.WATCHDOG_TASK_REPEAT}
          and all(c[c.index("/TR") + 1] == _wdcmd for c in _creates)
          and any(c[c.index("/SC") + 1] == "MINUTE"
                  and c[c.index("/MO") + 1] == "1" for c in _creates)
          and any(c[c.index("/SC") + 1] == "ONLOGON" for c in _creates),
          str(_creates))
    # healthy -> zero writes (a normal start is query-only, no churn)
    _wd_calls.clear()
    _ok = client_mod.register_watchdog()
    check("[watchdog] a healthy watchdog is never rewritten (no churn)",
          _ok is True and not [c for c in _wd_calls if c[0] == "/Create"],
          str(_wd_calls))
    # stale: the task exists but points at something else (moved exe, old
    # dev path) - the exact hole that let a Task Manager kill go unwatched
    _wd_state[client_mod.WATCHDOG_TASK_REPEAT] = '"C:\\old\\moved.exe" --watchdog'
    _wd_calls.clear()
    _ok = client_mod.register_watchdog()
    _creates = [c for c in _wd_calls if c[0] == "/Create"]
    check("[watchdog] a stale Task To Run is repaired with THIS app's command",
          _ok is True and len(_creates) == 1
          and _creates[0][_creates[0].index("/TN") + 1]
          == client_mod.WATCHDOG_TASK_REPEAT
          and _creates[0][_creates[0].index("/TR") + 1] == _wdcmd,
          str(_creates))
    # deleted mid-session -> the self-heal pass restores it and leaves
    # evidence in the durable local log
    del _wd_state[client_mod.WATCHDOG_TASK_REPEAT]
    _wd_calls.clear()
    _wd_ev.clear()
    _ok = client_mod.register_watchdog()
    with open(client_mod.__file__, encoding="utf-8") as _f:
        _wdsrc = _f.read()
    check("[watchdog] the self-heal pass restores a task deleted mid-session",
          _ok is True and client_mod.WATCHDOG_TASK_REPEAT in _wd_state
          and 'name="watchdog-heal"' in _wdsrc
          and any(s == "INFO" and "Watchdog" in m for s, m in _wd_ev),
          f"ok={_ok} ev={_wd_ev}")
    # a refused registration reports False and records WARN evidence
    _wd_state.pop(client_mod.WATCHDOG_TASK_REPEAT, None)
    _wd_fail_create[0] = True
    _wd_ev.clear()
    _ok = client_mod.register_watchdog()
    check("[watchdog] a refused registration reports False + local evidence",
          _ok is False
          and any(s == "WARN" and "failed" in m.lower() for s, m in _wd_ev),
          f"ok={_ok} ev={_wd_ev}")
    _wd_fail_create[0] = False
finally:
    client_mod._schtasks = _wd_sch_orig
    client_mod._watchdog_evidence = _wd_ev_orig
# the WARN throttle, exercised on the REAL function with log_event caught
_ls_orig_log = client_mod.local_store.log_event
_ls_rows = []
client_mod.local_store.log_event = lambda *a, **k: (_ls_rows.append(a), "")[1]
try:
    client_mod._WD_EVIDENCE_AT = 0.0
    _wd_ev_orig("WARN", "throttle probe 1")
    _wd_ev_orig("WARN", "throttle probe 2")
finally:
    client_mod.local_store.log_event = _ls_orig_log
    client_mod._WD_EVIDENCE_AT = 0.0
check("[watchdog] WARN evidence is throttled to one per hour",
      len(_ls_rows) == 1 and _ls_rows[0][0] == "WARN"
      and "throttle probe 1" in _ls_rows[0][2], str(_ls_rows))

# ---- standalone uninstall: behavioural (the checks above are static)-------
# Everything real is faked: _run never spawns taskkill/schtasks, the file
# step works on a temp directory - the gate's own DB cannot be touched.
import shutil as _shutil
_un_run_orig = _un._run
_un_saved = {n: getattr(_un, n) for n in (
    "remove_tasks", "remove_run_entry", "stop_client",
    "export_and_remove_files", "_report", "_self_delete", "_base_dir")}
try:
    # remove_tasks issues real deletion commands for BOTH exact names
    _calls = []
    _un._run = lambda a: (_calls.append(list(a)), (0, "SUCCESS"))[1]
    _m = _un.remove_tasks()
    check("[uninstall] remove_tasks deletes both tasks with /F",
          len(_calls) == 2
          and all(c[0] == "schtasks" and c[1] == "/Delete"
                  and "/F" in c for c in _calls)
          and {c[c.index("/TN") + 1] for c in _calls}
          == {_un.WATCHDOG_TASK_LOGON, _un.WATCHDOG_TASK_REPEAT}
          and not [m for m in _m if m.startswith("FAILED")],
          str(_calls))
    # a missing task is 'already gone', never a FAILED (rerun-safe)
    _un._run = lambda a: (1, "ERROR: The system cannot find the file "
                              "specified.")
    _m = _un.remove_tasks()
    check("[uninstall] a missing task counts as done (rerun is safe)",
          len(_m) == 2 and all("already gone" in m for m in _m),
          str(_m))
    # stop_client verifies: gone -> success, still listed -> FAILED
    _un._run = lambda a: ((0, "INFO: No tasks are running which match the "
                              "specified criteria.")
                          if a[0] == "tasklist" else (0, ""))
    _gone = _un.stop_client()
    _un._run = lambda a: ((0, "client.exe Console 1 5,000 K")
                          if a[0] == "tasklist" else (0, ""))
    _still = _un.stop_client()
    check("[uninstall] stop_client verifies instead of claiming green",
          _gone.startswith("Client stopped")
          and _still.startswith("FAILED: client.exe is still running"),
          f"gone={_gone!r} still={_still!r}")
    # happy path: steps in order, exit 0, self-delete last
    _seq = []
    _un.remove_tasks = lambda: (_seq.append("tasks"),
                                ["Task removed: x"])[1]
    _un.remove_run_entry = lambda: (_seq.append("run"),
                                    "Startup entry removed: x")[1]
    _un.stop_client = lambda: (_seq.append("stop"),
                               "Client stopped (client.exe is not running)")[1]
    _un.export_and_remove_files = lambda: (_seq.append("files"),
                                           ["File removed: client.exe"])[1]
    _un._report = lambda lines, ok, quiet: _seq.append(("report", ok, quiet))
    _un._self_delete = lambda: _seq.append("selfdelete")
    _rc = _un.main(["--quiet"])
    check("[uninstall] happy path: ordered steps, exit 0, self-delete last",
          _rc == 0
          and _seq == ["tasks", "run", "stop", "files",
                       ("report", True, True), "selfdelete"],
          str(_seq))
    # a FAILED anywhere -> exit 1, no self-delete, operator told to rerun
    _seq.clear()
    _un.stop_client = lambda: (_seq.append("stop"),
                               "FAILED: client.exe is still running - run "
                               "this uninstall app as Administrator")[1]
    _rc = _un.main(["--quiet"])
    check("[uninstall] a FAILED step exits 1 and never self-deletes",
          _rc == 1 and ("report", False, True) in _seq
          and "selfdelete" not in _seq,
          str(_seq))
    # P1-6: a failed export KEEPS the audit database; other files still go
    # (real file logic - the happy-path step fakes are restored first)
    for _n2 in ("remove_tasks", "remove_run_entry", "stop_client",
                "export_and_remove_files"):
        setattr(_un, _n2, _un_saved[_n2])
    _tmp = tempfile.mkdtemp(prefix="unins_")
    try:
        for _n in ("lab_client.db", "client.exe", "lab_config.json"):
            with open(os.path.join(_tmp, _n), "w") as _f:
                _f.write("x")
        import local_store as _lsmod
        _dbp, _exp = _lsmod.db_path, _lsmod.export_logs
        _un._base_dir = lambda: _tmp
        _lsmod.db_path = lambda: os.path.join(_tmp, "lab_client.db")
        _lsmod.export_logs = lambda dest=None: None    # export FAILS
        try:
            _m = _un.export_and_remove_files()
        finally:
            _lsmod.db_path, _lsmod.export_logs = _dbp, _exp
        check("[uninstall] a failed export KEEPS the audit database (P1-6)",
              os.path.exists(os.path.join(_tmp, "lab_client.db"))
              and not os.path.exists(os.path.join(_tmp, "client.exe"))
              and not os.path.exists(os.path.join(_tmp, "lab_config.json"))
              and any("could not be exported" in m for m in _m)
              and any("File removed: client.exe" in m for m in _m),
              str(_m))
    finally:
        _shutil.rmtree(_tmp, ignore_errors=True)
finally:
    _un._run = _un_run_orig
    for _n, _v in _un_saved.items():
        setattr(_un, _n, _v)

# ---------------------------------------------------------------- teardown
stop_reader.set()
stop3.set()
srv.stop()
time.sleep(0.3)
check("server stopped", not srv.running)
row = database.get_connection().execute(
    "SELECT status, logout_time FROM client_sessions "
    "WHERE session_id='sessSTOP'").fetchone()
check("server stop closes open sessions",
      row and row["logout_time"] and row["status"] == "Disconnected",
      str(dict(row) if row else None))
row = database.get_connection().execute(
    "SELECT is_online FROM computers WHERE pc_name='TEST-PC'").fetchone()
check("server stop marks PCs offline",
      row and row["is_online"] == 0, str(row and row["is_online"]))

print(flush=True)
if FAIL:
    print(f"*** {len(FAIL)} FAILURES: {FAIL}", flush=True)
    raise SystemExit(1)
print("ALL INTEGRATION TESTS PASSED", flush=True)
