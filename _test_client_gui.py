"""Client kiosk GUI state-machine test - deterministic (config pre-written,
class-level stubs so no real dialogs can appear).

Covers the v2.1 fix plan: verifying-timeout, admin-lock login block,
lock preserved across disconnect, force-login unlock, pause that never
auto-expires, logout clearing overlays, no-config first-run screen.
"""
import os
import json
import client

# --- safety: no keyboard hook, no server-IP dialog ever -------------------
client.HotkeyBlocker.start = lambda self: None
client.HotkeyBlocker.stop = lambda self: None
client.HotkeyBlocker.shutdown = lambda self: None
client.ClientApp._ask_server_config = lambda self: None

FAIL = []
def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {extra}" if extra else ""), flush=True)
    if not cond:
        FAIL.append(name)

# --- deterministic config: present before construction --------------------
cfg_file = client.config_path()
had_config = os.path.exists(cfg_file)
backup = open(cfg_file, encoding="utf-8").read() if had_config else None
with open(cfg_file, "w", encoding="utf-8") as f:
    json.dump({"server_ip": "127.0.0.1", "server_port": 18443}, f)

from protocol import Message, MessageType

# ============================ PATH A: config present ======================
app = client.ClientApp()
app.update()
check("[config] kiosk visible at start", app.winfo_viewable())
check("[config] login state", app.state == "login")
check("[config] session bar hidden", not app.bar.winfo_viewable())

# --- "Verifying..." can never get stuck (Phase A/#5) ----------------------
app.net.connected = True              # pretend the link is up, no answer
app.id_var.set("2023-00001"); app.pw_var.set("student123")
app.attempt_login()
app.update()
check("[verify] button shows Verifying",
      str(app.login_btn.cget("text")) == "Verifying…")
app._verify_timeout()
app.update()
check("[verify] timeout resets the button",
      str(app.login_btn.cget("text")) == "Log In")
check("[verify] timeout shows a message",
      "did not respond" in app.msg_lbl.cget("text"))
app.net.connected = False

# --- successful login unlocks the PC --------------------------------------
app._on_auth_ok({"student_id": "2023-00001", "full_name": "Juan Dela Cruz",
                 "role": "student", "course": "BS CS", "year_level": "3rd"})
app.update()
check("[config] login unlocks PC",
      app.state == "unlocked" and not app.winfo_viewable())
check("[config] session created", bool(app.session_id))
check("[config] session bar visible", app.bar.winfo_viewable() and
      "Juan" in app.bar_user.cget("text"))

# --- admin lock returns to login screen, session survives -----------------
app.handle_command(Message.create(MessageType.CMD_LOCK,
                                  {"params": {"message": "Locked!"},
                                   "command_id": "c1"}))
app.update()
check("[config] admin lock -> login screen",
      app.state == "admin_lock" and app.winfo_viewable())
check("[config] session survives lock", bool(app.session_id))
check("[config] bar hidden on lock", not app.bar.winfo_viewable())

# --- admin lock blocks credentials (Phase C/#2) ---------------------------
app.id_var.set("2023-00001"); app.pw_var.set("student123")
app.attempt_login()
app.update()
check("[lock] login attempt blocked by admin lock",
      app.state == "admin_lock" and
      "locked by the administrator" in app.msg_lbl.cget("text"))
check("[lock] button not stuck on Verifying",
      str(app.login_btn.cget("text")) != "Verifying…")

# --- admin lock survives a server disconnect (no bypass, Phase C) ---------
app._handle_event({"kind": "net_disconnected"})
app.update()
check("[lock] admin lock survives disconnect",
      app.state == "admin_lock" and app.winfo_viewable())
check("[lock] session still survives", bool(app.session_id))
check("[disconnect] reconnect message shown",
      "Reconnecting" in app.msg_lbl.cget("text"))

# --- Admin Force Login: unlock restores the session without credentials ---
app.handle_command(Message.create(MessageType.CMD_UNLOCK, {"command_id": "c5b"}))
app.update()
check("[unlock] force login restores session",
      app.state == "unlocked" and bool(app.session_id))
check("[unlock] kiosk hidden again", not app.winfo_viewable())

# --- re-login resumes the same session ------------------------------------
old = app.session_id
app._on_auth_ok({"student_id": "2023-00001", "full_name": "Juan Dela Cruz",
                 "role": "student"})
app.update()
check("[config] relogin resumes session",
      app.session_id == old and app.state == "unlocked")

# --- pause / resume (pause NEVER auto-expires, Phase B/#6) ----------------
app.handle_command(Message.create(MessageType.CMD_PAUSE,
                                  {"params": {"message": "brb", "seconds": 30},
                                   "command_id": "c2"}))
app.update()
check("[config] pause overlay appears",
      app.state == "paused" and app.pause_win is not None)
check("[config] pause hides kiosk/bar",
      not app.winfo_viewable() and not app.bar.winfo_viewable())
check("[pause] no auto-expiry scheduled", app.pause_until == 0,
      str(app.pause_until))
app._tick()                          # several UI ticks must not resume it
app._tick()
app.update()
check("[pause] still paused after ticks", app.state == "paused" and
      app.pause_win is not None)
app.handle_command(Message.create(MessageType.CMD_RESUME, {"command_id": "c3"}))
app.update()
check("[config] resume restores session",
      app.state == "unlocked" and app.pause_win is None and app.bar.winfo_viewable())

# --- repeated pause must not leak a second overlay ------------------------
app.handle_command(Message.create(MessageType.CMD_PAUSE,
                                  {"params": {"message": "again", "seconds": 5},
                                   "command_id": "c2b"}))
app.handle_command(Message.create(MessageType.CMD_PAUSE,
                                  {"params": {"message": "twice", "seconds": 5},
                                   "command_id": "c2c"}))
app.update()
overlays = [c for c in app.winfo_children()
            if c.winfo_class() == "Toplevel" and c is not app.bar]
check("[pause] single overlay after repeated pause", len(overlays) == 1,
      str(len(overlays)))
app.handle_command(Message.create(MessageType.CMD_RESUME, {"command_id": "c3b"}))
app.update()

# --- remote logout -> login screen (clears any pause overlay) -------------
app.handle_command(Message.create(MessageType.CMD_LOGOUT, {"command_id": "c4"}))
app.update()
check("[config] logout -> login screen",
      app.state == "login" and app.winfo_viewable())
check("[config] logout clears session",
      app.session_id is None and app.user is None)
check("[config] logout hides bar", not app.bar.winfo_viewable())
check("[logout] no pause overlay left behind", app.pause_win is None)

# --- invalid credentials stay locked --------------------------------------
app._handle_auth_response({"success": False, "error": "Invalid ID or password."})
app.update()
check("[config] invalid creds stay locked", app.state == "login" and
      "Invalid" in app.msg_lbl.cget("text"))
check("[config] login button re-enabled",
      str(app.login_btn.cget("state")) != "disabled")

# --- valid credentials unlock ---------------------------------------------
app._handle_auth_response({"success": True, "user_data": {
    "student_id": "2023-00001", "full_name": "Juan", "role": "student"}})
app.update()
check("[config] valid creds unlock", app.state == "unlocked")

# --- unlock with no session requires login --------------------------------
app.user = None
app.session_id = None
app._show_lock("")
app.state = "admin_lock"
app.handle_command(Message.create(MessageType.CMD_UNLOCK, {"command_id": "c5"}))
app.update()
check("[config] unlock w/o session -> login screen", app.state == "login")
check("[unlock] login allowed after admin unlock (no creds asked)",
      app.state != "admin_lock")

# --- remote message popup does not break state ---------------------------
app.handle_command(Message.create(MessageType.CMD_SEND_MESSAGE,
                                  {"params": {"message": "Hello",
                                              "msg_type": "warning"},
                                   "command_id": "c6"}))
app.update(); app.update()
check("[config] message popup handled", app.state == "login")

# --- attempt login while disconnected stays locked ------------------------
app.net.connected = False              # force deterministic offline state
app.id_var.set("2023-00001"); app.pw_var.set("student123")
app.attempt_login()
app.update()
check("[config] offline login attempt stays locked",
      app.state == "login" and "Not connected" in app.msg_lbl.cget("text"))

# --- heartbeat carries the display fields (Phase A/#1) --------------------
sent = None
class _Net: pass
orig_send = app.net.send
def _capture(msg):
    global sent
    sent = msg
    return True
app.net.send = _capture
app._send_heartbeat(1.0, 2.0)
app.net.send = orig_send
check("[heartbeat] includes ip + hostname",
      sent is not None and "ip" in sent.payload and "hostname" in sent.payload,
      str(sent and list(sent.payload.keys())))
check("[heartbeat] includes cpu/ram/user/status",
      sent and sent.payload.get("cpu_percent") == 1.0 and
      sent.payload.get("ram_percent") == 2.0 and "status" in sent.payload)

app.stop_observation()
app.net.stop()
app.destroy()

# ============================ PATH B: no config ===========================
os.remove(cfg_file)
app2 = client.ClientApp()
app2.update()
check("[no-config] kiosk visible + locked (never hides)",
      app2.winfo_viewable() and app2.state == "login")
check("[no-config] server-settings button offered",
      hasattr(app2, "config_btn") and app2.config_btn.winfo_ismapped())
app2._ask_server_config()          # stub - must be a no-op, no dialog
app2.update()
check("[no-config] no crash from config flow", app2.state == "login")
check("[no-config] still visible after config cancel",
      app2.winfo_viewable())
app2.stop_observation()
app2.net.stop()
app2.destroy()

# --- restore whatever config existed before the test -----------------------
if had_config and backup is not None:
    with open(cfg_file, "w", encoding="utf-8") as f:
        f.write(backup)
elif os.path.exists(cfg_file):
    os.remove(cfg_file)

print(flush=True)
if FAIL:
    print(f"*** {len(FAIL)} FAILURES: {FAIL}", flush=True)
    raise SystemExit(1)
print("ALL CLIENT GUI TESTS PASSED", flush=True)
