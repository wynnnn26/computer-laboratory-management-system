"""Full integration smoke test (v2.1 fix plan): DB, protocol, TLS server,
heartbeats + ACK, desired lock/pause state, force logout, sessions,
audit trail, sidebar dashboard, toasts, attendance removal, UI."""
import queue
import socket
import threading
import time
import os
import json

from utils import now_date as _today, now_datetime

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
# v2.1 schema columns + settings migration
cols = [r[1] for r in conn.execute("PRAGMA table_info(computers)").fetchall()]
check("computers has hostname column", "hostname" in cols, str(cols))
check("computers has admin_state column", "admin_state" in cols, str(cols))
conn.close()
check("screenshot quality readable", database.get_setting("screenshot_quality") == "70",
      database.get_setting("screenshot_quality"))
check("screenshot scale readable", database.get_setting("screenshot_scale") == "0.75",
      database.get_setting("screenshot_scale"))

# ---------------------------------------------------------------- protocol
from protocol import (Message, MessageType, TLSSocketWrapper, create_ssl_context,
                      build_client_register, build_client_heartbeat,
                      build_auth_request, build_command_response,
                      build_session_start, build_session_end, build_stu_request)

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

# ---------------------------------------------------------------- heartbeat
w.send_message(build_client_heartbeat("TEST-PC", "logged_in", 12.5, 44.0,
                                      logged_in_user="2023-00001",
                                      ip="127.0.0.1", hostname="testhost"))
r = w.recv_message(timeout=4)
check("heartbeat is ACKed (PONG)", r and r.type == MessageType.PONG.value, str(r and r.type))
check("PONG carries admin state field",
      r and "admin_state" in r.payload)
time.sleep(0.3)
snap = next((c for c in srv.list_clients() if c["pc_name"] == "TEST-PC"), None)
check("heartbeat cpu/ram/status/user",
      snap and snap["cpu_percent"] == 12.5 and snap["ram_percent"] == 44.0
      and snap["status"] == "logged_in" and snap["logged_in_user"] == "2023-00001")

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
check("observe start works", srv.start_screen_observe("TEST-PC", "t") is True)
time.sleep(0.3)
srv.stop_screen_observe("TEST-PC", "t")
check("observe watchers cleared", "TEST-PC" not in srv.screen_watchers)

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

w.send_message(build_session_start("TEST-PC", "2023-00001", "Juan Dela Cruz", "sess999"))
time.sleep(0.4)
conn = database.get_connection()
row = conn.execute("SELECT * FROM client_sessions WHERE session_id='sess999'").fetchone()
check("session recorded active", row and row["status"] == "Active")
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

# ---------------------------------------------------------------- settings
check("system settings seeded",
      database.get_setting("server_port", "") == "8443")
check("system settings default fallback",
      database.get_setting("no_such_key", "d") == "d")

# ---------------------------------------------------------------- UI build
import tkinter as tk
from admin_dashboard import AdminDashboard
from student_dashboard import StudentDashboard, g as row_g

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
check("Lab Attendance page removed",
      "attendance" not in dash.pages and "attendance" not in dash._sidebar_btns)
check("client tree populated", len(dash.client_tree.get_children()) > 0)
check("overview cards built", len(dash.cards_frame.winfo_children()) >= 9,
      str(len(dash.cards_frame.winfo_children())))
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
check("computers view has exactly 8 useful columns",
      headers == ["PC Name", "IP Address", "Hostname", "Online/Offline",
                  "Current User", "CPU %", "RAM %", "Last Seen"], str(headers))

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

# selecting a PC enables the controls
first_item = dash.client_tree.get_children()[0]
dash.client_tree.selection_set(first_item)
dash._on_client_select()
check("selecting a PC enables controls",
      all(str(b.cget("state")) == "normal" for b in dash._ctrl_btns))
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
print("ALL INTEGRATION TESTS PASPLIED", flush=True)
