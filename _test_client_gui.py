"""Client kiosk GUI state-machine test - deterministic (config pre-written,
class-level stubs so no real dialogs can appear).

Covers the v2.1 fix plan: verifying-timeout, admin-lock login block,
lock preserved across disconnect, force-login unlock, pause that never
auto-expires, logout clearing overlays, no-config first-run screen.
"""
import os
import json
import sys
import client

# status glyphs (● ○ • …) must never crash the console printer
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# --- safety: no keyboard hook, no server-IP dialog ever -------------------
client.HotkeyBlocker.start = lambda self: None
client.HotkeyBlocker.stop = lambda self: None
client.HotkeyBlocker.shutdown = lambda self: None
# ...but keep a handle on the REAL dialog first so the P0-2 block below can
# open it deliberately (every other call in this suite stays a no-op).
_real_ask_server_config = client.ClientApp.__dict__["_ask_server_config"]
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

# --- login card (spec): password toggle, server line, PC footer -----------
check("[login] password show/hide widgets present",
      hasattr(app, "pw_entry") and hasattr(app, "pw_show_btn"))
check("[login] password starts hidden",
      str(app.pw_entry.cget("show")) == "\u25cf")
_eye0 = app.pw_show_btn.cget("image")
app._toggle_pw_visibility(); app.update()
check("[login] show reveals the password",
      str(app.pw_entry.cget("show")) == ""
      and getattr(app.pw_show_btn, "_eye_state", "") == "hide"
      and app.pw_show_btn.cget("image") is not _eye0,
      getattr(app.pw_show_btn, "_eye_state", ""))
app._toggle_pw_visibility(); app.update()
check("[login] hide re-masks the password",
      str(app.pw_entry.cget("show")) == "\u25cf"
      and getattr(app.pw_show_btn, "_eye_state", "") == "show")
check("[login] server status line present",
      "Server" in str(app.srv_state_lbl.cget("text"))
      and ":" in str(app.srv_addr_lbl.cget("text")),
      f"{app.srv_state_lbl.cget('text')} {app.srv_addr_lbl.cget('text')}")
check("[login] PC footer + LAN Connection",
      "LAN Connection" in str(app.foot_conn_lbl.cget("text"))
      and "\u2022" in str(app.foot_pc_lbl.cget("text")),
      f"{app.foot_pc_lbl.cget('text')} | {app.foot_conn_lbl.cget('text')}")
check("[login] button label is LOGIN", str(app.login_btn.cget("text")) == "LOGIN",
      str(app.login_btn.cget("text")))

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
      str(app.login_btn.cget("text")) == "LOGIN")
check("[verify] timeout shows a message",
      "did not respond" in app.msg_lbl.cget("text"))
app.net.connected = False

# --- forced first-login password change (fresh client accounts) -----------
app._handle_auth_response({"success": True, "user_data": {
    "student_id": "2099-00001", "full_name": "Fresh Account",
    "role": "student", "must_change_password": True}})
app.update()
check("[pwchange] default-password login opens the change screen",
      app.state == "login" and getattr(app, "_pw_change_active", False)
      and hasattr(app, "pwc_new_var"))
check("[pwchange] no session before the change", app.session_id is None)
check("[pwchange] kiosk stays visible (still locked)", app.winfo_viewable())
check("[pwchange] pending identity kept",
      (app._pending_pw_user or {}).get("student_id") == "2099-00001")
check("[pwchange] tick-safe status widgets recreated",
      app.srv_state_lbl.winfo_exists() and app.foot_pc_lbl.winfo_exists())

# --- P1 hotkey cannot bypass the forced first-login card -------------------
app.event_generate("<Control-Shift-Alt-M>")
app.update()
check("[hotkey] no bypass of the forced password card",
      app.state == "login" and getattr(app, "_pw_change_active", False)
      and app.winfo_viewable() and app.session_id is None)

# --- client-side validation: short / mismatch / default -------------------
app.pwc_new_var.set("abc"); app.pwc_conf_var.set("abc")
app._submit_password_change(); app.update()
check("[pwchange] short password rejected",
      "at least 6" in str(app.pwc_err_lbl.cget("text")),
      str(app.pwc_err_lbl.cget("text")))
app.pwc_new_var.set("newpass123"); app.pwc_conf_var.set("newpass987")
app._submit_password_change(); app.update()
check("[pwchange] mismatch rejected",
      "do not match" in str(app.pwc_err_lbl.cget("text")),
      str(app.pwc_err_lbl.cget("text")))
app.pwc_new_var.set("password123"); app.pwc_conf_var.set("password123")
app._submit_password_change(); app.update()
check("[pwchange] default password rejected",
      "default password" in str(app.pwc_err_lbl.cget("text")),
      str(app.pwc_err_lbl.cget("text")))

# --- a valid change request goes out with ONLY the new password -----------
app.net.connected = True
sent_pw = []
orig_send3 = app.net.send
app.net.send = lambda m: (sent_pw.append(m), True)[1]
app.pwc_new_var.set("labpass99"); app.pwc_conf_var.set("labpass99")
app._submit_password_change()
app.net.send = orig_send3
app.update()
check("[pwchange] change request sent once",
      len(sent_pw) == 1
      and sent_pw[0].type == MessageType.PASSWORD_CHANGE_REQUEST.value,
      str([m.type for m in sent_pw]))
check("[pwchange] request carries only the new password",
      sent_pw and sent_pw[0].payload.get("new_password") == "labpass99"
      and len(sent_pw[0].payload) == 1, str(sent_pw and sent_pw[0].payload))
check("[pwchange] button shows Saving while waiting",
      str(app.pwc_btn.cget("text")) == "Saving…",
      str(app.pwc_btn.cget("text")))

# --- server rejection re-enables the card ---------------------------------
app._handle_password_change_response(
    {"success": False,
     "error": "Choose a password different from your current one."})
app.update()
check("[pwchange] server rejection shown",
      "different from your current" in str(app.pwc_err_lbl.cget("text")))
check("[pwchange] button re-enabled after rejection",
      str(app.pwc_btn.cget("text")) == "CHANGE PASSWORD"
      and str(app.pwc_btn.cget("state")) != "disabled")

# --- Back returns to the login card without unlocking anything ------------
app._cancel_password_change()
app.update()
check("[pwchange] back returns to the login card",
      app.state == "login" and str(app.login_btn.cget("text")) == "LOGIN")
check("[pwchange] back clears the pending identity",
      app._pending_pw_user is None and app.session_id is None)

# --- a successful change proceeds to the normal login flow ----------------
app._handle_auth_response({"success": True, "user_data": {
    "student_id": "2099-00001", "full_name": "Fresh Account",
    "role": "student", "must_change_password": True}})
app.update()
app._handle_password_change_response({"success": True})
app.update()
check("[pwchange] success unlocks with the pending identity",
      app.state == "unlocked"
      and app.user.get("student_id") == "2099-00001")
check("[pwchange] default flag cleared before unlock",
      not app.user.get("must_change_password"))
check("[pwchange] session created after the change", bool(app.session_id))

# --- restore the environment for the checks that follow -------------------
app.user_logout("pwchange cleanup")
app.net.connected = False
app.update()
check("[pwchange] cleanup back to the login screen",
      app.state == "login" and app.session_id is None
      and app.user is None)

# --- successful login unlocks the PC --------------------------------------
app._on_auth_ok({"student_id": "2023-00001", "full_name": "Juan Dela Cruz",
                 "role": "student", "course": "BS CS", "year_level": "3rd"})
app.update()
check("[config] login unlocks PC",
      app.state == "unlocked" and not app.winfo_viewable())
check("[config] session created", bool(app.session_id))
check("[config] session bar visible", app.bar.winfo_viewable() and
      "Juan" in app.bar_user.cget("text"))
check("[config] student sees Current User label",
      str(app.bar_user.cget("text")).startswith("Current User:"))
check("[config] dashboard button shown for students",
      app.dash_btn.winfo_ismapped())
# TASK-1: the session bar used to be an always-on-top, frameless overlay
# pinned over the user's other applications.  It must now be a normal
# desktop window that never stays above other apps.
check("[task-1] session bar is NOT always-on-top",
      not app.bar.attributes("-topmost"))
check("[task-1] session bar is a normal window (no frameless override)",
      not app.bar.overrideredirect())
check("[task-1] session bar has a real title bar / taskbar presence",
      str(app.bar.title()) != "")
# TASK-2: the ⚙ Server Settings gear is gone from the main Client
# display; the feature itself is still reachable (login-screen gear +
# role-gated Maintenance entry in the Dashboard header).
check("[task-2] no settings gear on the session bar",
      not hasattr(app, "bar_cfg_btn"))
check("[p0-2] settings still reachable from the LOGIN screen",
      hasattr(app, "config_btn") and app.config_btn is not None
      and app.config_btn.cget("command") != "",
      str(getattr(app, "config_btn", None)))
# spec 2: the bar button is "Log Out" (was "Re-lock")
check("[logout] bar has a Log Out button (spec 2)",
      hasattr(app, "logout_btn") and app.logout_btn.winfo_ismapped()
      and str(app.logout_btn.cget("text")).strip() == "Log Out",
      str(getattr(app, "logout_btn", None)
          and app.logout_btn.cget("text")))
# spec 14: connection medium (Ethernet/Wi-Fi) shown on the session bar
check("[conn] session bar shows Connection type",
      hasattr(app, "bar_conn") and app.bar_conn.winfo_exists()
      and str(app.bar_conn.cget("text")).startswith("Connection: ")
      and str(app.bar_conn.cget("text")).split("Connection: ", 1)[1]
          in ("Ethernet", "Wi-Fi", "LAN"),
      str(getattr(app, "bar_conn", None)
          and app.bar_conn.cget("text")))

# --- P3: dashboard X closes the window WITHOUT logging out ---------------
app.open_dashboard()
app.update()
check("[p3] dashboard opens", getattr(app, "_dash", None) is not None
      and app._dash.winfo_exists())
# TASK-2: a student account must never see a maintenance/settings entry
# in the Dashboard header (the ⚙ gear was removed from the main display).
check("[task-2] student dashboard has no Maintenance/settings entry",
      not hasattr(app._dash, "maint_btn"))
# TASK-1: opening the Dashboard must never make the Client sit on top of
# other applications - it is an ordinary, non-topmost window.
check("[task-1] open Dashboard is NOT always-on-top",
      not app._dash.attributes("-topmost"))
check("[task-1] open Dashboard is a normal (non-overrideredirect) window",
      not app._dash.overrideredirect())
check("[task-1] the locked kiosk root stays withdrawn behind the session",
      not app.winfo_viewable())
dash_p3 = app._dash
sent_ev = []
orig_send4 = app.net.send
app.net.send = lambda m: (sent_ev.append(m), True)[1]
dash_p3._close()                      # exactly what the title-bar X runs
app.net.send = orig_send4
app.update()
check("[p3] X on the dashboard does NOT log out",
      app.state == "unlocked" and bool(app.session_id), str(app.state))
check("[p3] session bar stays visible after X",
      app.bar.winfo_viewable() and "Juan" in app.bar_user.cget("text"))
check("[p3] dashboard window really closed", not dash_p3.winfo_exists())
check("[p3] 'client_closed' sent, no session_end",
      any(m.type == MessageType.ACTIVITY_LOG.value
          and m.payload.get("action") == "client_closed" for m in sent_ev)
      and not any(m.type == MessageType.SESSION_END.value for m in sent_ev),
      str([(m.type, m.payload.get("action")) for m in sent_ev]))

# --- P3: minimizing the dashboard must not log out either -----------------
app.open_dashboard()
app.update()
dash_p3b = app._dash
dash_p3b.iconify()
app.update()
check("[p3] minimize keeps the session",
      app.state == "unlocked" and bool(app.session_id)
      and app.bar.winfo_viewable(), str(app.state))
dash_p3b.destroy()                    # plain close: no event, no logout
app._dash = None
app.update()

# --- admin lock returns to login screen, session survives -----------------
app.handle_command(Message.create(MessageType.CMD_LOCK,
                                  {"params": {"message": "Locked!"},
                                   "command_id": "c1"}))
app.update()
check("[config] admin lock -> login screen",
      app.state == "admin_lock" and app.winfo_viewable())
check("[config] session survives lock", bool(app.session_id))
check("[config] bar hidden on lock", not app.bar.winfo_viewable())
# TASK-2: on the (now visible) login screen the Server IP/Port gear is
# present and reachable - that is the secondary configuration area.
check("[task-2] LOGIN-screen settings gear visible while locked",
      app.config_btn.winfo_ismapped()
      and app.config_btn.cget("command") != "")

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

# --- P1 emergency hotkey: Ctrl+Shift+Alt+M force-unlocks the kiosk --------
# (a) no-op while already unlocked
app.event_generate("<Control-Shift-Alt-M>")
app.update()
check("[hotkey] combo is a no-op while unlocked",
      app.state == "unlocked" and not app.winfo_viewable(), str(app.state))
# (b) admin lock + session -> the combo restores the session UI
app.handle_command(Message.create(MessageType.CMD_LOCK,
                                  {"params": {"message": "hotkey test"},
                                   "command_id": "h1"}))
app.update()
locked_session = app.session_id
check("[hotkey] locked again with the session intact",
      app.state == "admin_lock" and bool(locked_session), str(app.state))
sent_hk = []
orig_send5 = app.net.send
app.net.send = lambda m: (sent_hk.append(m), True)[1]
app.event_generate("<Control-Shift-Alt-M>")
app.net.send = orig_send5
app.update()
check("[hotkey] combo force-unlocks the locked kiosk",
      app.state == "unlocked" and not app.winfo_viewable(), str(app.state))
check("[hotkey] session survives the emergency unlock",
      app.session_id == locked_session and app.bar.winfo_viewable())
check("[hotkey] unlock is audited as hotkey_force_unlock",
      any(m.type == MessageType.ACTIVITY_LOG.value
          and m.payload.get("action") == "hotkey_force_unlock"
          for m in sent_hk),
      str([(m.type, m.payload.get("action")) for m in sent_hk]))
# (c) the combo must NEVER release a pause (only admin Resume does)
app.handle_command(Message.create(MessageType.CMD_PAUSE,
                                  {"params": {"message": "hk"},
                                   "command_id": "h2"}))
app.update()
app.event_generate("<Control-Shift-Alt-M>")
app.update()
check("[hotkey] pause is NOT released by the hotkey",
      app.state == "paused" and app.pause_win is not None, str(app.state))
app.handle_command(Message.create(MessageType.CMD_RESUME,
                                  {"command_id": "h3"}))
app.update()
check("[hotkey] admin resume still works afterwards",
      app.state == "unlocked" and app.pause_win is None, str(app.state))

# --- Website Access: the legacy cmd_web_filter shape clears the block ------
# P1.4: enforcement moved to connection detection, so the plain domain
# list the server used to push can no longer write a hosts block - the
# only honest action is to clear anything an older build left behind, and
# the only honest answer is the detection state we are really in.
applied_wf, sent_wf = [], []
orig_awf = client.apply_web_filter
orig_send_wf = app.net.send
client.apply_web_filter = lambda domains, hosts_path=None: (
    applied_wf.append(list(domains)), True)[1]
app.net.send = lambda m: (sent_wf.append(m), True)[1]
app.handle_command(Message.create(MessageType.CMD_WEB_FILTER,
                                  {"command_id": "wf1",
                                   "params": {"domains": ["bad.example"]}}))
app.net.send = orig_send_wf
client.apply_web_filter = orig_awf
check("[webfilter] the legacy list only clears the managed block",
      applied_wf == [[]], str(applied_wf))
check("[webfilter] no detector running is reported, never a fake success",
      any(m.type == MessageType.CMD_RESPONSE.value
          and m.payload.get("command_id") == "wf1"
          and m.payload.get("success") is False
          and m.payload.get("error")
          for m in sent_wf),
      str([(m.payload.get("success"), m.payload.get("error"))
           for m in sent_wf]))
# (c) with detection actually running the same command is acknowledged
class _RunningWeb:
    def status(self):
        return "OK", ""


_o_det_wf = client._web_detector
client._web_detector = _RunningWeb()
sent_wf2 = []
client.apply_web_filter = lambda domains, hosts_path=None: (
    applied_wf.append(list(domains)), True)[1]
app.net.send = lambda m: (sent_wf2.append(m), True)[1]
app.handle_command(Message.create(MessageType.CMD_WEB_FILTER,
                                  {"command_id": "wf2",
                                   "params": {"domains": ["x.test"]}}))
app.net.send = orig_send_wf
client.apply_web_filter = orig_awf
client._web_detector = _o_det_wf
check("[webfilter] detection running -> the legacy push is acknowledged",
      applied_wf[-1] == []
      and any(m.type == MessageType.CMD_RESPONSE.value
              and m.payload.get("command_id") == "wf2"
              and m.payload.get("success") is True
              and (m.payload.get("result") or {}).get("enforced") == "detect"
              for m in sent_wf2),
      str([(m.payload.get("success"), m.payload.get("result"))
           for m in sent_wf2]))

# --- Website Access: versioned policy snapshot -> apply + ack (spec 8) ---
applied_pol, sent_pol = [], []
orig_awp = client.apply_web_policy
client.apply_web_policy = lambda pol, hosts_path=None, manage_dns=True: (
    applied_pol.append(dict(pol)), (True, "detect", None))[1]
app.net.send = lambda m: (sent_pol.append(m), True)[1]
app.handle_command(Message.create(MessageType.CMD_WEB_FILTER,
                                  {"command_id": "wp1",
                                   "params": {"policy": {
                                       "version": 7, "mode": "block_list",
                                       "blocked": ["bad.example"],
                                       "allowed": [],
                                       "upstream_dns": "9.9.9.9"}}}))
app.net.send = orig_send_wf
client.apply_web_policy = orig_awp
check("[webpolicy] policy snapshot reaches the applier",
      len(applied_pol) == 1
      and applied_pol[0].get("version") == 7
      and applied_pol[0].get("mode") == "block_list"
      and applied_pol[0].get("blocked") == ["bad.example"]
      and applied_pol[0].get("upstream_dns") == "9.9.9.9",
      str(applied_pol))
check("[webpolicy] command acknowledged with version + enforcement",
      any(m.type == MessageType.CMD_RESPONSE.value
          and m.payload.get("command_id") == "wp1"
          and m.payload.get("success") is True
          and (m.payload.get("result") or {}).get("version") == 7
          and (m.payload.get("result") or {}).get("enforced") == "detect"
          for m in sent_pol),
      str([(m.type, m.payload.get("command_id")) for m in sent_pol]))
check("[webpolicy] WEB_POLICY_ACK carries version + success",
      any(m.type == MessageType.WEB_POLICY_ACK.value
          and m.payload.get("version") == 7
          and m.payload.get("success") is True
          and m.payload.get("enforced") == "detect"
          for m in sent_pol),
      str([(m.type, m.payload.get("version")) for m in sent_pol]))
# (b) an apply failure is acked as failed - the server must never show
# SYNCED for a policy the client could not enforce
sent_pol2 = []
client.apply_web_policy = lambda pol, hosts_path=None, manage_dns=True: (
    False, "none", "ALLOW ONLY has no allowed domains - "
                   "nothing can be enforced")
app.net.send = lambda m: (sent_pol2.append(m), True)[1]
app.handle_command(Message.create(MessageType.CMD_WEB_FILTER,
                                  {"command_id": "wp2",
                                   "params": {"policy": {
                                       "version": 8, "mode": "allow_only",
                                       "blocked": [],
                                       "allowed": ["s.test"]}}}))
app.net.send = orig_send_wf
client.apply_web_policy = orig_awp
check("[webpolicy] failed apply reports the reason",
      any(m.type == MessageType.CMD_RESPONSE.value
          and m.payload.get("command_id") == "wp2"
          and m.payload.get("success") is False
          and "ALLOW ONLY" in (m.payload.get("error") or "")
          for m in sent_pol2),
      str([(m.payload.get("success"), m.payload.get("error"))
           for m in sent_pol2]))
check("[webpolicy] failed apply acks success=False",
      any(m.type == MessageType.WEB_POLICY_ACK.value
          and m.payload.get("version") == 8
          and m.payload.get("success") is False
          and "ALLOW ONLY" in (m.payload.get("error") or "")
          for m in sent_pol2),
      str([(m.payload.get("version"), m.payload.get("success"))
           for m in sent_pol2]))

# --- P1.0: startup repairs the stuck DNS BEFORE the policy re-applies ----
# A run killed by the watchdog never restored the adapter, so re-applying
# the saved policy first would repoint it and record 127.0.0.1 as the
# "previous" value all over again - the exact bug that left a PC with no
# name resolution after clicking ALLOW ALL.
_order = []
_o_repair = client.repair_local_dns
_o_apply_sp = client.apply_web_policy
client.repair_local_dns = lambda *a, **k: (_order.append("repair"),
                                           (True, ""))[1]
client.apply_web_policy = lambda *a, **k: (_order.append("apply"),
                                           (True, "none", None))[1]
client._startup_web_policy({"version": 1, "mode": "block_list",
                            "blocked": ["bad.example"], "allowed": []})
client._startup_web_policy(None)
check("[dns] startup repairs BEFORE re-applying the saved policy",
      _order == ["repair", "apply", "repair"], str(_order))
check("[dns] startup re-applies only when a policy was saved",
      _order.count("apply") == 1, str(_order))
client.repair_local_dns = _o_repair
client.apply_web_policy = _o_apply_sp

# --- P1.3: detection -> durable queue + server audit + guarded close -----
# The detector is faked, so these prove the ROUTING (queue first, audit
# second, guarded close third, outcome through the same two sinks) and
# can never scan a socket table or terminate a process.
class _FakeWebDetector:
    def __init__(self, events=(), close=(True, "closed chrome.exe (pid 4242)"),
                 state=("OK", "")):
        self.events = list(events)
        self.close_result = close
        self.closed = []
        self.policy = None
        self.state = state
        self.last_error = ""

    def status(self):
        return self.state

    def set_policy(self, pol):
        self.policy = dict(pol or {})

    def get_policy(self):
        return dict(self.policy or {})

    def poll(self):
        out, self.events = list(self.events), []
        return out

    def close_browser(self, ev):
        self.closed.append(ev)
        return self.close_result

    def stop(self):
        return True


_wsh = os.path.join(os.environ.get("TEMP", os.getcwd()),
                    "lab_webscan_hosts.txt")
# start from what an OLDER build would have left behind: a managed block
with open(_wsh, "w", encoding="utf-8") as _fh:
    _fh.write("127.0.0.1 localhost\n"
              + client.HOSTS_BLOCK_BEGIN + "\n"
              "0.0.0.0 bad.example\n"
              + client.HOSTS_BLOCK_END + "\n")

_fw_orig = client._web_detector
client._web_detector = _fw = _FakeWebDetector()
_ok, _enf, _err = client.apply_web_policy(
    {"version": 21, "mode": "block_list", "blocked": ["bad.example"],
     "allowed": [], "upstream_dns": ""}, hosts_path=_wsh, manage_dns=False)
_txt = open(_wsh, encoding="utf-8").read()
check("[webscan] an enforcing apply installs the versioned snapshot",
      _ok is True and _enf == "detect" and _fw.policy == {
          "version": 21, "mode": "block_list", "blocked": ["bad.example"],
          "allowed": [], "upstream_dns": ""},
      f"{_ok},{_enf},{_err},{_fw.policy}")
check("[webscan] an enforcing apply clears a legacy hosts block",
      client.HOSTS_BLOCK_BEGIN not in _txt
      and "0.0.0.0 bad.example" not in _txt
      and "127.0.0.1 localhost" in _txt, repr(_txt))

# the detector keeps mirroring the snapshot even when we must answer
# FAILED - the server pushes again, and detection must not go stale
client._web_detector = _fw = _FakeWebDetector(
    state=("FAILED", "cannot identify which process owns another "
                     "application's connection - run the Client as "
                     "Administrator"))
_ok, _enf, _err = client.apply_web_policy(
    {"version": 22, "mode": "allow_only", "allowed": ["ok.example"],
     "blocked": []}, hosts_path=_wsh, manage_dns=False)
check("[webscan] a refused detector probe is reported, rules still kept",
      _ok is False and _enf == "none"
      and "cannot identify" in (_err or "")
      and _fw.policy.get("version") == 22
      and _fw.policy.get("mode") == "allow_only",
      f"{_ok},{_enf},{_err},{_fw.policy}")

_ev_web = {"domain": "bad.example", "ip": "203.0.113.5", "pid": 4242,
           "process": "chrome.exe", "browser": "chrome.exe",
           "browser_pid": 4242, "browser_created": 0.0,
           "status": "DETECTED", "reason": "blocked", "policy_version": 21}


def _run_scan(detector):
    """Run one scan with both sinks captured; restores them on the way out."""
    sent, logged = [], []
    client._web_detector = detector
    o_send, o_logf = app.net.send, client.local_store.log_event
    app.net.send = lambda m: (sent.append(m), True)[1]
    client.local_store.log_event = \
        lambda *a, **k: (logged.append((a, k)), "evt")[1]
    try:
        n = app._web_access_scan()
    finally:
        app.net.send = o_send
        client.local_store.log_event = o_logf
    acts = [m.payload.get("action") for m in sent
            if m.type == MessageType.ACTIVITY_LOG.value]
    msgs = [a[0][2] for a in logged if len(a[0]) > 2]
    return n, acts, msgs, detector


_fw = _FakeWebDetector(events=[_ev_web])
n, acts, msgs, _ = _run_scan(_fw)
check("[webscan] DETECTED is queued, notified and acted on",
      n == 1 and len(_fw.closed) == 1
      and "web_access_detected" in acts and "web_browser_closed" in acts
      and any("bad.example" in m for m in msgs)
      and any("Browser closed" in m for m in msgs),
      f"{n},{acts},{msgs}")

_fw = _FakeWebDetector(events=[dict(_ev_web, status="UNRESOLVED",
                                    reason="ambiguous-domain")])
n, acts, msgs, _ = _run_scan(_fw)
check("[webscan] UNRESOLVED is recorded but never acted on",
      n == 1 and _fw.closed == []
      and "web_access_unresolved" in acts and "web_browser_closed" not in acts,
      f"{n},{acts},{_fw.closed}")

_fw = _FakeWebDetector(events=[_ev_web],
                       close=(False, "responsible process (opencode-cli.exe) "
                                    "is not chrome/edge/firefox"))
n, acts, msgs, _ = _run_scan(_fw)
check("[webscan] a refused close is reported, not swallowed",
      n == 1 and "web_close_failed" in acts
      and any("close not performed" in m for m in msgs),
      f"{n},{acts},{msgs}")

_fw = _FakeWebDetector(events=[_ev_web], close=(False, "cooldown active (42s left)"))
n, acts, msgs, _ = _run_scan(_fw)
check("[webscan] a cooldown is logged as routine, not as a failure",
      n == 1 and "web_close_skipped" in acts and "web_close_failed" not in acts,
      f"{n},{acts}")

client._web_detector = None
check("[webscan] a scan with no detector is a harmless no-op",
      app._web_access_scan() == 0, "should be 0")

# --- P1.3 live seam: a REAL detector -> the REAL durable queue + audit ----
# Everything upstream is hermetic (injected resolver + socket table), so
# no real DNS or socket table is read - but the detector, the scan, the
# local_store row and the audit message are the shipping code.  The pid is
# our own parent: a real process that is certainly not a browser, so the
# guarded close MUST refuse and nothing is ever terminated.
_det_live = client.web_access.WebAccessDetector(
    resolver=lambda d, u=None: {"blocked.test": ["203.0.113.77"]}.get(d, set()),
    connections=lambda: [("203.0.113.77", os.getppid() or 999999,
                          "ESTABLISHED")])
_det_live.set_policy({"version": 30, "mode": "block_list",
                      "blocked": ["blocked.test"], "allowed": [],
                      "upstream_dns": ""})
_det_live.stop()
_det_live.refresh()
client._web_detector = _det_live
_sent_live = []
_o_send_live = app.net.send
app.net.send = lambda m: (_sent_live.append(m), True)[1]
try:
    _n_live = app._web_access_scan()
finally:
    app.net.send = _o_send_live
_live_rows = [r for r in client.local_store.pending_logs(500)
              if r.get("category") == "web"]
_live_acts = [m.payload.get("action") for m in _sent_live
              if m.type == MessageType.ACTIVITY_LOG.value]
check("[webscan] a real detector event lands in the real queue + audit",
      _n_live == 1
      and any("blocked.test" in str(r.get("message", "")) for r in _live_rows)
      and "web_access_detected" in _live_acts
      and "web_close_failed" in _live_acts
      and _det_live.kills == [],
      f"{_n_live},{_live_acts},{[r.get('message') for r in _live_rows]},"
      f"{_det_live.kills}")
_det_live.stop()
for _r in _live_rows:
    try:
        with client.local_store._lock:
            _c = client.local_store.get_local_connection()
            try:
                _c.execute("DELETE FROM local_logs WHERE event_id=?",
                           (_r.get("event_id"),))
                _c.commit()
            finally:
                _c.close()
    except Exception:
        pass

client._web_detector = _fw_orig
if os.path.exists(_wsh):
    os.remove(_wsh)

# --- UDP LAN discovery (fallback path) -------------------------------------
check("[udp] discovery finds no server in tests",
      client.discover_server_ip(18443, timeout=0.4) is None)
check("[udp] kiosk fell back to the saved server_ip",
      app.net.server_ip == "127.0.0.1", str(app.net.server_ip))

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

# --- spec 2: the "Log Out" button (was "Re-lock") actually logs out --------
# re-unlock first: the remote logout above landed on the login screen.
app._handle_auth_response({"success": True, "user_data": {
    "student_id": "2023-00001", "full_name": "Juan", "role": "student"}})
app.update()
check("[logout] re-login before button test", app.state == "unlocked")
check("[logout] no 'Re-lock' control left in the bar",
      not any("re-lock" in str(c.cget("text")).lower()
              for c in app.bar.winfo_children()
              if c.winfo_class() == "Button"))
app.logout_btn.invoke()
app.update()
check("[logout] Log Out button -> login screen",
      app.state == "login" and app.winfo_viewable(), str(app.state))
check("[logout] Log Out clears session + user",
      app.session_id is None and app.user is None)
check("[logout] Log Out hides bar", not app.bar.winfo_viewable())

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

# --- client-admin login: server-validated role, kiosk UI stays put -------
app._on_auth_ok({"student_id": "admin", "full_name": "System Administrator",
                 "role": "admin"})
app.update()
check("[admin] admin login unlocks the kiosk", app.state == "unlocked")
check("[admin] bar shows Current User",
      "Current User:" in str(app.bar_user.cget("text"))
      and "System Administrator" in str(app.bar_user.cget("text")),
      str(app.bar_user.cget("text")))
check("[admin] bar shows Role: ADMINISTRATOR",
      str(app.bar_role.cget("text")) == "Role: ADMINISTRATOR",
      str(app.bar_role.cget("text")))
check("[admin] client never opens the Server/Admin Dashboard",
      getattr(app, "_dash", None) is None)
check("[admin] dashboard button hidden for admins",
      not app.dash_btn.winfo_ismapped())

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

# --- Send File: dispatch writes to a patched Desktop and acks success ----
import tempfile as _tf
_desk = _tf.mkdtemp(prefix="cdesk_")
_orig_dd = client.desktop_dir
client.desktop_dir = lambda: _desk
_acks = []
_orig_s = app.net.send
def _cap_ack2(msg):
    if msg.type == MessageType.CMD_RESPONSE.value:
        _acks.append(msg)
    return True
app.net.send = _cap_ack2
app.handle_command(Message.create(MessageType.CMD_SEND_FILE,
                                  {"params": {"filename": "notes.txt",
                                              "data": "aGVsbG8="},
                                   "command_id": "sf1"}))
app.net.send = _orig_s
client.desktop_dir = _orig_dd
check("[sendfile] dispatch saves the file and acks success",
      os.path.isfile(os.path.join(_desk, "notes.txt"))
      and open(os.path.join(_desk, "notes.txt"), "rb").read() == b"hello"
      and _acks and _acks[0].payload.get("success") is True
      and _acks[0].payload.get("command_id") == "sf1",
      str([(a.payload.get("command_id"), a.payload.get("success"))
           for a in _acks]))

# --- command_id de-duplication: a re-delivered command runs ONCE ---------
acks = []
orig_send2 = app.net.send
def _cap_ack(msg):
    if msg.type == MessageType.CMD_RESPONSE.value:
        acks.append(msg)
    return True
app.net.send = _cap_ack
app.handle_command(Message.create(MessageType.CMD_PAUSE,
                                  {"params": {"message": "dup", "seconds": 0},
                                   "command_id": "dup1"}))
app.handle_command(Message.create(MessageType.CMD_PAUSE,
                                  {"params": {"message": "dup", "seconds": 0},
                                   "command_id": "dup1"}))
app.net.send = orig_send2
app.update()
check("[dedupe] first delivery acked", len(acks) == 1, str(len(acks)))
check("[dedupe] duplicate delivery ignored",
      app.state == "paused" and app.pause_win is not None, str(app.state))
app.handle_command(Message.create(MessageType.CMD_RESUME,
                                  {"command_id": "dupres"}))
app.update()
check("[dedupe] resume still honored after dedupe",
      app.state == "login" and app.pause_win is None, str(app.state))

# --- offline login + Local Mode (spec items 10/12) ------------------------
import local_store as _ls
import database as _db
app.net.connected = False              # force deterministic offline state
# (a) no roster synced yet -> fail closed, kiosk stays locked
app.id_var.set("2023-00001"); app.pw_var.set("student123")
app.attempt_login()
app.update()
check("[offline] empty roster stays locked",
      app.state == "login"
      and "Connect to the server" in app.msg_lbl.cget("text"),
      str(app.msg_lbl.cget("text")))
# (b) roster synced -> the right password unlocks in Local Mode
_ls.init_local_db()
_ls.cache_auth_roster([
    {"student_id": "2023-00001", "full_name": "Juan Dela Cruz",
     "role": "student",
     "password_hash": _db.hash_password("student123"),
     "status": "Active", "must_change_password": 0}], 7)
app.attempt_login()
app.update()
check("[offline] roster account signs in (Local Mode)",
      app.state == "unlocked" and getattr(app, "offline_session", False),
      str(app.state))
check("[offline] offline session carries a session id",
      bool(app.session_id), str(app.session_id))
# (c) wrong password is rejected with the usual message
app.user_logout("offline test reset")
app.update()
app.id_var.set("2023-00001"); app.pw_var.set("wrong")
app.attempt_login()
app.update()
check("[offline] wrong password rejected",
      app.state == "login"
      and "Invalid ID or password" in app.msg_lbl.cget("text"),
      str(app.msg_lbl.cget("text")))
# (d) accounts that were never synced have no offline access
app.id_var.set("9999-99999"); app.pw_var.set("student123")
app.attempt_login()
app.update()
check("[offline] non-roster account rejected",
      app.state == "login"
      and "No offline access" in app.msg_lbl.cget("text"),
      str(app.msg_lbl.cget("text")))
# (e) a roster older than max_offline_days expires (item 12)
_ls.cache_auth_roster([
    {"student_id": "2023-00001", "full_name": "Juan Dela Cruz",
     "role": "student",
     "password_hash": _db.hash_password("student123"),
     "status": "Active", "must_change_password": 0}], 7)
import time as _time
_stale = _time.strftime("%Y-%m-%d %H:%M:%S",
                        _time.localtime(_time.time() - 9 * 86400))
_c = _ls.get_local_connection()
_c.execute("UPDATE auth_cache SET synced_at=? WHERE student_id='2023-00001'",
           (_stale,))
_c.commit(); _c.close()
app.id_var.set("2023-00001"); app.pw_var.set("student123")
app.attempt_login()
app.update()
check("[offline] stale roster expires after max_offline_days",
      app.state == "login" and "expired" in app.msg_lbl.cget("text"),
      str(app.msg_lbl.cget("text")))
# (f) accounts flagged must_change_password are online-only
_ls.cache_auth_roster([
    {"student_id": "2023-00001", "full_name": "Juan Dela Cruz",
     "role": "student",
     "password_hash": _db.hash_password("student123"),
     "status": "Active", "must_change_password": 1}], 7)
app.attempt_login()
app.update()
check("[offline] password-change accounts are online-only",
      app.state == "login"
      and "while connected" in app.msg_lbl.cget("text"),
      str(app.msg_lbl.cget("text")))
# (g) every attempt is durably logged locally (item 9) and nothing has
# been assumed delivered - all rows stay PENDING until the server acks
_pend, _sync, _tot = _ls.log_counts()
check("[offline] attempts land in the durable local log",
      _tot >= 4 and _pend >= 4, f"pending={_pend} synced={_sync} total={_tot}")
_unacked = _ls.log_event("WARN", "test", "row waiting for its ack")
_ls.mark_synced(["not-a-real-event-id"])
check("[flush] rows stay PENDING until their own id is acknowledged",
      any(e["event_id"] == _unacked for e in _ls.pending_logs(500)),
      _unacked)
# (h) spec 10 connection indicator, exact strings
app.net.connected = True
app._refresh_login_status()
check("[indicator] online text is spec-exact",
      str(app.srv_state_lbl.cget("text")) == "\u25cf Server Connected",
      str(app.srv_state_lbl.cget("text")))
app.net.connected = False
app._refresh_login_status()
check("[indicator] offline text is spec-exact",
      str(app.srv_state_lbl.cget("text"))
      == "\u25cb Server Offline \u2014 Local Mode",
      str(app.srv_state_lbl.cget("text")))

# --- P0-3: durable sync survives a restart + roster refresh timer --------
_rid_live = _ls.log_event("INFO", "test", "row written before a restart")
import sqlite3 as _sq
_rc = _sq.connect(_ls.db_path())          # fresh connection: no module state
_rows_before = _rc.execute(
    "SELECT sync_status FROM local_logs WHERE event_id=?",
    (_rid_live,)).fetchall()
_rc.close()
check("[p0-3] pending rows survive a client restart",
      len(_rows_before) == 1 and _rows_before[0][0] == "PENDING",
      str(_rows_before))
# the roster refresh timer must fire on schedule while connected ...
app._roster_due = client.ROSTER_REFRESH_EVERY - 1
app.net.connected = True
check("[p0-3] roster refresh fires on schedule while connected",
      app._roster_refresh_due() is True and app._roster_due == 0,
      str(app._roster_due))
# ... and must never run while the link is down (nothing to pull against)
app._roster_due = client.ROSTER_REFRESH_EVERY - 1
app.net.connected = False
check("[p0-3] roster refresh skipped while offline",
      app._roster_refresh_due() is False)
app._roster_due = 0
app.net.connected = False            # the state this block inherited

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

# --- P0-1: system tray (one icon per process, safe to start/stop twice) ---
check("[p0-1] tray not started by construction",
      app2._tray is None and app2._tray_ready is None
      and app2._tray_ok is False)
check("[p0-1] tray artwork comes from assets/images/logo.png",
      client._tray_logo(64) is not None)
_tray_started = app2.start_tray()
app2.update()
check("[p0-1] tray icon starts",
      _tray_started is True and app2._tray is not None
      and app2._tray_ok is True, f"started={_tray_started}")
check("[p0-1] start_tray is idempotent", app2.start_tray() is True)
app2.stop_tray()
check("[p0-1] tray icon stops",
      app2._tray is None and app2._tray_ready is None
      and app2._tray_ok is False)
app2.stop_tray()                       # must be a harmless no-op
check("[p0-1] stop_tray is idempotent", app2._tray is None)

# --- P0-1: exactly one Client per PC (named mutex, across processes) ------
check("[p0-1] first Client launch acquires the mutex",
      client.acquire_single_instance() is True)
# A second client.exe creates its OWN handle to the same named mutex; the
# module-level shortcut only hides a repeat call from THIS process, so drop
# it here to reproduce exactly what a second launch does.
_owner = client._SINGLE_INSTANCE_HANDLE
client._SINGLE_INSTANCE_HANDLE = None
check("[p0-1] second Client launch is rejected",
      client.acquire_single_instance() is False)
client._SINGLE_INSTANCE_HANDLE = _owner
check("[p0-1] release drops the handle",
      client.release_single_instance() is True
      and client._SINGLE_INSTANCE_HANDLE is None)
check("[p0-1] acquires again after release",
      client.acquire_single_instance() is True)
client.release_single_instance()
check("[p0-1] nothing left held", client._SINGLE_INSTANCE_HANDLE is None)

# --- P1-4: Task Scheduler watchdog (relaunches the kiosk if it ever dies) --
# The task command must be THIS app plus the magic flag, so a tick can only
# ever relaunch the same kiosk.
_wcmd = client._watchdog_command()
check("[p1-4] watchdog command relaunches this very app",
      _wcmd.endswith("--watchdog")
      and os.path.basename(sys.executable).lower() in _wcmd.lower(),
      _wcmd)
# idle PC: the tick may start the kiosk (this is the recovery path)
check("[p1-4] a tick on an idle PC is allowed to start",
      client._watchdog_conflict(["client.exe", "--watchdog"]) == "start")
# hold the mutex the way ANOTHER running process would, then re-tick
_owner = client._SINGLE_INSTANCE_HANDLE
client._SINGLE_INSTANCE_HANDLE = None
check("[p1-4] a tick while running exits silently (no second kiosk)",
      client._watchdog_conflict(["client.exe", "--watchdog"]) == "quiet")
check("[p1-4] a human double-launch is reported, not silent",
      client._watchdog_conflict(["client.exe"]) == "conflict")
client._SINGLE_INSTANCE_HANDLE = _owner
client.release_single_instance()
check("[p1-4] nothing left held after the watchdog gate",
      client._SINGLE_INSTANCE_HANDLE is None)
# the schtasks wrapper must never raise, even for a command that fails
_rc_wd, _out_wd = client._schtasks(["/Query", "/TN", "_no_such_task_"])
check("[p1-4] schtasks failures come back as data, not exceptions",
      isinstance(_rc_wd, int) and _rc_wd != 0 and isinstance(_out_wd, str),
      f"rc={_rc_wd} out={_out_wd[:80]}")
try:
    _wd_removed = client.remove_watchdog()
    _wd_err = ""
except Exception as _e:
    _wd_removed, _wd_err = None, str(_e)
check("[p1-4] removing the watchdog tasks is safe and idempotent",
      not _wd_err and isinstance(_wd_removed, bool),
      f"removed={_wd_removed} err={_wd_err}")

# --- P0-1: the Dashboard is an ordinary window parked in the tray ---------
# The dashboard reads the server database (announcements / messages /
# equipment) while building its tabs; this suite never initialises it,
# so create the schema first - the run is cleaned up afterwards anyway.
_db.init_db()
from student_dashboard import StudentDashboard
tray_dash = StudentDashboard(app2, {"student_id": "2023-00001",
                                    "full_name": "Juan Dela Cruz"},
                             on_logout=lambda: None,
                             minimize_to_tray=True)
app2.update()
check("[p0-1] Dashboard is never always-on-top",
      not tray_dash.attributes("-topmost"))
tray_dash.iconify()
app2.update()
check("[p0-1] minimizing parks the Dashboard in the tray",
      tray_dash.state() == "withdrawn", tray_dash.state())
check("[p0-1] parking never touches the kiosk/session state",
      app2.state == "login" and app2.session_id is None, str(app2.state))
tray_dash.deiconify(); tray_dash.lift(); app2.update()
check("[p0-1] restore brings the Dashboard back",
      tray_dash.state() == "normal" and tray_dash.winfo_viewable(),
      f"state={tray_dash.state()} viewable={tray_dash.winfo_viewable()}")
tray_dash.destroy()
app2.update()
check("[p0-1] closing the Dashboard is still not a logout",
      app2.state == "login")

# --- P0-2: Server Settings dialog (opened deliberately, not via the stub) --
check("[p0-2] lock-screen settings gear stays available",
      getattr(app2, "config_btn", None) is not None
      and app2.config_btn.winfo_ismapped())
_probe = client.ClientApp._probe_server("127.0.0.1", 1)
check("[p0-2] unreachable server probe fails safely (nothing raised)",
      isinstance(_probe, tuple) and _probe[0] is False and _probe[1],
      str(_probe))

client.ClientApp._ask_server_config = _real_ask_server_config
app2._ask_server_config()
app2.update()
check("[p0-2] settings dialog opens",
      getattr(app2, "_settings_win", None) is not None
      and app2._settings_win.winfo_exists())
check("[p0-2] dialog is topmost (never hidden behind the lock screen)",
      app2._settings_win.attributes("-topmost"))
check("[p0-2] dialog prefills the saved values",
      app2._cfg_ip_var.get() == "" and app2._cfg_port_var.get() == "8443",
      f"ip={app2._cfg_ip_var.get()!r} port={app2._cfg_port_var.get()!r}")

# every bad input is rejected WITHOUT writing lab_config.json
for _ip, _port, _why in (("127.0.0.1", "99999", "port out of range"),
                         ("127.0.0.1", "abc",   "port not a number"),
                         ("",          "8443", "empty address")):
    app2._cfg_ip_var.set(_ip)
    app2._cfg_port_var.set(_port)
    app2._cfg_save()
    app2.update()
    check(f"[p0-2] rejects {_why}",
          getattr(app2, "_settings_win", None) is not None
          and app2._settings_win.winfo_exists()
          and not os.path.exists(cfg_file),
          str(getattr(app2, "_cfg_status_lbl", None)
              and app2._cfg_status_lbl.cget("text")))
check("[p0-2] rejection reason is shown to the user",
      bool(getattr(app2, "_cfg_status_lbl", None))
      and any(_t in str(app2._cfg_status_lbl.cget("text"))
              for _t in ("1 and 65535", "number", "required")),
      str(app2._cfg_status_lbl.cget("text")))

# a valid save writes ONLY lab_config.json and re-points the live link
app2._cfg_ip_var.set("127.0.0.1")
app2._cfg_port_var.set("18443")
app2._cfg_save()
app2.update()
_saved = (json.load(open(cfg_file, encoding="utf-8"))
          if os.path.exists(cfg_file) else {})
check("[p0-2] valid save persists server_ip/port",
      _saved.get("server_ip") == "127.0.0.1"
      and int(_saved.get("server_port", 0)) == 18443, str(_saved))
check("[p0-2] dialog closes after a successful save",
      getattr(app2, "_settings_win", None) is None)
check("[p0-2] link re-pointed in place, no restart needed",
      app2.net.server_ip == "127.0.0.1" and app2.net.server_port == 18443,
      f"{app2.net.server_ip}:{app2.net.server_port}")
check("[p0-2] kiosk still locked after configuring",
      app2.state == "login" and app2.winfo_viewable())

# --- P0-3: IP change then flush -------------------------------------------
# Stop the connect thread first so it cannot race the manual state below,
# then prove the backlog queued across the address change is pushed to the
# NEW server - and that only what it acks ever leaves PENDING.
app2.net.stop()
# wait for the connect loop to actually leave its current attempt (it can
# sit in create_connection for up to 5 s + a 2.5 s back-off) - otherwise
# it could observe the hand-set `connected` flag and act on it.
if app2.net._conn_thread is not None:
    app2.net._conn_thread.join(timeout=10)
_rid_flush = _ls.log_event("INFO", "test", "queued across an address change")
_orig_req = app2.net.request
_got_batch = {"n": 0}
def _fake_log_sync(kind, payload=None, timeout=None):
    if kind != "log_sync":
        return {"ok": False}
    evs = (payload or {}).get("events") or []
    _got_batch["n"] = len(evs)
    # acknowledge everything EXCEPT the newest row, which must stay PENDING
    return {"ok": True,
            "data": {"accepted": [e["event_id"] for e in evs[:-1]]}}
app2.net.request = _fake_log_sync
app2.net.connected = True
_flush_ok = app2._flush_local_logs()
check("[p0-3] IP change then flush: backlog pushed to the new server",
      _flush_ok is True and _got_batch["n"] > 0,
      f"n={_got_batch['n']} ok={_flush_ok}")
check("[p0-3] only the ACKed rows leave PENDING",
      any(e["event_id"] == _rid_flush for e in _ls.pending_logs(500)),
      _rid_flush)
check("[p0-3] unacknowledged rows are never deleted",
      any(e["event_id"] == _rid_flush for e in _ls.pending_logs(500))
      and _ls.log_counts()[0] >= 1, str(_ls.log_counts()))
# single-flight: a second trigger while one is running must be refused,
# otherwise a reconnect + timer could double-send the same batch.
with app2._log_flush_lock:
    _second = app2._flush_local_logs()
check("[p0-3] flush is single-flight (second trigger refused)",
      _second is False, str(_second))
app2.net.request = _orig_req
app2.net.connected = False

# restore the safety stub for anything that follows
client.ClientApp._ask_server_config = lambda self: None

# --- P1-5 panic stop: Ctrl+Shift+Alt+K ends the session and locks ---------
check("[panic] Tk binding registered for Ctrl+Shift+Alt+K",
      bool(app2.bind_all("<Control-Shift-Alt-K>")))
check("[panic] keyboard hook raises the panic stop too",
      app2.hotkeys.on_panic == app2._panic_from_hook)
# drop anything earlier steps queued so it cannot interleave with this
while True:
    try:
        app2.events.get_nowait()
    except Exception:
        break
app2.user = {"student_id": "2023-00001", "full_name": "Juan Dela Cruz",
             "role": "student"}
app2.session_id = "SESS-PANIC"
app2.session_start = _time.time()
app2.net.connected = False            # Server offline: must not matter
app2._unlock_ui()
app2.update()
check("[panic] setup: a session is running and unlocked",
      app2.state == "unlocked" and app2.session_id == "SESS-PANIC",
      str(app2.state))
# The Tk binding's target is _panic_stop itself.  Driving it through
# `event_generate` is NOT reliable here: while a session runs the kiosk is
# WITHDRAWN and Tk silently drops the synthetic key - which is exactly why
# the global keyboard hook (exercised further down) is the real path.
app2._panic_stop()
app2.update()
check("[panic] unlocked session is stopped and the PC locks",
      app2.state == "login" and app2.winfo_viewable(), str(app2.state))
check("[panic] panic worked with the Server offline",
      app2.net.connected is False and app2.session_id is None
      and app2.user is None,
      f"conn={app2.net.connected} sess={app2.session_id}")
check("[panic] lock screen names the panic",
      "Panic stop" in str(app2.msg_lbl.cget("text")),
      str(app2.msg_lbl.cget("text")))
check("[panic] audited in the durable local store",
      len([e for e in _ls.pending_logs(500)
           if e.get("message") == "Panic stop activated"]) == 1)
# double delivery (Tk binding AND hook, both allowed to fire) must neither
# double-log nor raise - this is what makes the two paths safe together.
app2._panic_stop()
app2._panic_from_hook()
app2._pump()
check("[panic] repeat delivery while locked is a safe no-op",
      app2.state == "login"
      and len([e for e in _ls.pending_logs(500)
               if e.get("message") == "Panic stop activated"]) == 1)
# ...and the hook path must be able to perform the panic on its own, since
# the kiosk has no focus at all while it is unlocked (hook is global).
app2.user = {"student_id": "2023-00001", "full_name": "Juan Dela Cruz",
             "role": "student"}
app2.session_id = "SESS-PANIC-HOOK"
app2.session_start = _time.time()
app2._unlock_ui()
app2.update()
check("[panic] setup 2: session running again",
      app2.state == "unlocked", str(app2.state))
app2._panic_from_hook()
app2._pump()
check("[panic] the keyboard-hook path stops the session too",
      app2.state == "login" and app2.session_id is None
      and app2.user is None, str(app2.state))
check("[panic] second panic is audited as well",
      len([e for e in _ls.pending_logs(500)
           if e.get("message") == "Panic stop activated"]) == 2)
# an admin pause is admin-owned: only Resume releases it, never a hotkey
app2.user = {"student_id": "2023-00001", "full_name": "Juan Dela Cruz",
             "role": "student"}
app2.session_id = "SESS-PANIC-2"
app2.session_start = _time.time()
app2._unlock_ui()
app2.update()
app2.handle_command(Message.create(MessageType.CMD_PAUSE,
                                   {"params": {"message": "panic test"},
                                    "command_id": "pz1"}))
app2.update()
app2._panic_from_hook()
app2._pump()
check("[panic] does NOT release an admin pause",
      app2.state == "paused" and app2.pause_win is not None,
      str(app2.state))
app2.handle_command(Message.create(MessageType.CMD_RESUME,
                                   {"command_id": "pz2"}))
app2.update()
check("[panic] admin resume still works afterwards",
      app2.state == "unlocked" and app2.pause_win is None, str(app2.state))
# Ctrl+Shift+Alt+M is the opposite emergency and must be untouched
check("[panic] the M rescue hotkey is still bound too",
      bool(app2.bind_all("<Control-Shift-Alt-M>")))
# leave it locked, exactly as the rest of this suite expects
app2.user_logout("panic test cleanup")
app2.update()
check("[panic] cleanup returns to the lock screen",
      app2.state == "login", str(app2.state))

# --- P1-6 [Uninstall Client] (admin/maintenance + explicit confirmation) ---
_real_uninstall_work = client.__dict__["_uninstall_client_work"]
client.ClientApp._ask_server_config = _real_ask_server_config
# (a) a student never sees the maintenance section at all
app2.user = {"student_id": "2023-00001", "full_name": "Juan Dela Cruz",
             "role": "student"}
app2._ask_server_config()
app2.update()
check("[p1-6] student sees no Uninstall button",
      getattr(app2, "_settings_win", None) is not None
      and getattr(app2, "_cfg_uninstall_btn", None) is None)
app2._cfg_close()
app2.update()
# (b) an admin does, and closing drops the handle with the dialog
app2.user["role"] = "admin"
app2._ask_server_config()
app2.update()
check("[p1-6] admin sees the Uninstall button",
      getattr(app2, "_cfg_uninstall_btn", None) is not None)
app2._cfg_close()
app2.update()
check("[p1-6] closing the dialog drops the button handle",
      getattr(app2, "_cfg_uninstall_btn", None) is None)
# (c) the role gate is re-checked INSIDE the handler, so a stale widget
#     (or a direct call) can never reach the uninstall
_ran = []
client._uninstall_client_work = lambda *a, **k: (_ran.append(1), ["ok"])[1]
_orig_yesno = client.messagebox.askyesno
for _role in ("student", "staff", "", None):
    app2.user = {"student_id": "2023-00001", "role": _role}
    app2._uninstall_client()
check("[p1-6] only admin/maintenance may trigger it",
      _ran == [], f"ran={_ran}")
# (d) an admin who DECLINES must change nothing
app2.user = {"student_id": "2023-00001", "role": "admin"}
client.messagebox.askyesno = lambda *a, **k: False
app2._uninstall_client()
check("[p1-6] declining the confirmation does nothing", _ran == [],
      str(_ran))
# (e) an admin who CONFIRMS gets the real action
client.messagebox.askyesno = lambda *a, **k: True
app2._uninstall_client()
client.messagebox.askyesno = _orig_yesno
check("[p1-6] confirming runs the uninstall", _ran == [1], str(_ran))
client._uninstall_client_work = _real_uninstall_work
# (f) the real work: export the log, drop auto-start, keep everything else
_p16_dest = os.path.join(os.environ.get("TEMP", "."),
                         "p16_uninstall_export.csv")
if os.path.exists(_p16_dest):
    os.remove(_p16_dest)
_lines16 = client._uninstall_client_work(_p16_dest)
check("[p1-6] real uninstall reports every step",
      len(_lines16) == 3 and all(isinstance(x, str) and x for x in _lines16),
      str(_lines16))
check("[p1-6] the local log is exported",
      os.path.exists(_p16_dest) and os.path.getsize(_p16_dest) > 0)
try:
    _txt16 = open(_p16_dest, encoding="utf-8-sig").read()
except Exception as _e:
    _txt16 = str(_e)
_hdr16 = (_txt16.splitlines() or [""])[0]
check("[p1-6] export carries a proper CSV header",
      "event_id" in _hdr16, _hdr16)
check("[p1-6] export records that the client was uninstalled",
      "Client uninstalled" in _txt16, _txt16[:160])
try:
    os.remove(_p16_dest)
except Exception:
    pass
check("[p1-6] the kiosk database is left in place",
      os.path.exists(_ls.db_path()), str(_ls.db_path()))
# restore the safety stub for anything that follows
client.ClientApp._ask_server_config = lambda self: None

# --- P1-7 Observe: a stream that is visible, audited and really stops -----
import threading as _thr
app2.net.connected = False            # offline worker must idle, not capture
check("[observe] nothing observed before the command",
      app2.observe_stop is None
      and getattr(app2, "_observe_banner", None) is None)
# (a) the command path: parameters are clamped BEFORE they reach the loop,
#     so a malformed command can never peg the CPU or flood the wire.
app2.handle_command(Message.create(MessageType.CMD_SCREEN_OBSERVE_START,
                                   {"command_id": "ob1",
                                    "params": {"interval": 0.0,
                                               "quality": 999,
                                               "scale": 42}}))
app2.update()
check("[observe] out-of-range parameters are clamped",
      app2.observe_cfg == {"interval": 0.3, "quality": 95, "scale": 1.0},
      str(app2.observe_cfg))
check("[observe] streaming raises NO client-side banner",
      getattr(app2, "_observe_banner", None) is None
      and getattr(app2, "_show_observing_banner", None) is None,
      "the red OBSERVING overlay source was removed")
check("[observe] the frame stream is tied to the command that asked",
      isinstance(app2.observe_ref, str) and len(app2.observe_ref) > 0,
      str(app2.observe_ref))
check("[observe] start is audited in the durable local store",
      any(e.get("message") == "Screen observation started"
          for e in _ls.pending_logs(500)))
# (b) a restart must retire the OLD stream: the worker keeps its own
#     private event, so it can never be swapped out from under it and
#     leave two capture threads running for ever.
_first_ev = app2.observe_stop
_first_badge = getattr(app2, "_observe_banner", None)
app2.start_observation("ref2", 5, 30, 0.2)

def _alive(w):
    try:
        return bool(w is not None and w.winfo_exists())
    except Exception:
        return False                       # already destroyed
check("[observe] restart retires the previous stream",
      _first_ev.is_set() and app2.observe_stop is not _first_ev,
      f"set={_first_ev.is_set()}")
# the client-side OBSERVING overlay source was deleted outright, so a
# restart can never (re)create it - nothing is ever on screen.
check("[observe] a restart raises no client-side banner at all",
      getattr(app2, "_observe_banner", None) is None
      and _first_badge is None
      and getattr(app2, "_show_observing_banner", None) is None)
# (c) no thread may ever blow up when the shared attribute is cleared
_thr_err = []
_orig_hook = _thr.excepthook
_thr.excepthook = lambda args: _thr_err.append(args)
app2.stop_observation()              # clears observe_stop while running
_time.sleep(0.4)
_thr.excepthook = _orig_hook
check("[observe] the capture thread never dies on a cleared attribute",
      _thr_err == [], str([getattr(e, "exc_value", e) for e in _thr_err]))
# (d) stop clears the stream and audits it exactly once
check("[observe] stop drops the stream (and never a banner)",
      app2.observe_stop is None and app2.observe_ref is None
      and getattr(app2, "_observe_banner", None) is None)
_stops = len([e for e in _ls.pending_logs(500)
              if e.get("message") == "Screen observation stopped"])
check("[observe] stop is audited in the durable local store",
      _stops >= 1, str(_stops))
app2.stop_observation()
app2.stop_observation()
check("[observe] stopping again is idempotent (no extra audit row)",
      len([e for e in _ls.pending_logs(500)
           if e.get("message") == "Screen observation stopped"]) == _stops,
      f"was={_stops}")
# (e) CMD_SCREEN_OBSERVE_STOP must produce the same result as a direct call
app2.handle_command(Message.create(MessageType.CMD_SCREEN_OBSERVE_STOP,
                                   {"command_id": "ob2"}))
app2.update()
check("[observe] the stop command reaches the same clean state",
      app2.observe_stop is None
      and getattr(app2, "_observe_banner", None) is None)
# (f) the overlay source is gone: no method, no window, and no widget
#     anywhere in the client still shows the old observing warning.
check("[observe] the OBSERVING overlay source is really deleted",
      not hasattr(app2, "_show_observing_banner")
      and not hasattr(app2, "_observe_banner"))
def _widget_text(w, out=None):
    out = [] if out is None else out
    for c in w.winfo_children():
        try:
            out.append(str(c.cget("text")))
        except Exception:
            pass
        _widget_text(c, out)
    return out
_texts = " ".join(_widget_text(app2))
check("[observe] no visible client widget warns about being observed",
      "OBSERVING" not in _texts and "being viewed" not in _texts,
      _texts[:200])

# --- P1-8 Remote control: a durable, honest record on the PC itself -------
app2.net.connected = False            # worst case: the link is already down
app2.handle_command(Message.create(MessageType.CMD_SCREEN_OBSERVE_STOP,
                                   {"command_id": "rc1"}))
app2.update()
_rc = [e for e in _ls.pending_logs(500)
       if e.get("message") == "Remote command received"]
check("[p1-8] the client records every remote command it receives",
      len(_rc) >= 1, str(len(_rc)))
check("[p1-8] the record names the command and its id",
      any("cmd_screen_observe_stop" in str(e.get("detail"))
          and "rc1" in str(e.get("detail")) for e in _rc),
      str(_rc[-1:]))
check("[p1-8] the record says whether the link could carry the ack",
      any("rc1" in str(e.get("detail"))
          and "link=down" in str(e.get("detail")) for e in _rc),
      str(_rc[-1:]))
# a repeated command_id is dropped before it is ever recorded or re-run
app2.handle_command(Message.create(MessageType.CMD_SCREEN_OBSERVE_STOP,
                                   {"command_id": "rc1"}))
check("[p1-8] a repeated command_id is never recorded twice",
      len([e for e in _ls.pending_logs(500)
           if e.get("message") == "Remote command received"
           and "rc1" in str(e.get("detail"))]) == 1)
check("[p1-8] the kiosk database survives a command received offline",
      os.path.exists(_ls.db_path()), str(_ls.db_path()))

# --- Task 5: remote mouse/keyboard control on the Client ------------------
_sent5 = []
_orig_send5 = app2.net.send
app2.net.send = lambda m: (_sent5.append(m), True)[1]
_applied5 = []
# never move this machine's real cursor/keyboard during the test run
app2._inject_mouse = lambda u32, ev: (_applied5.append(("mouse", dict(ev))),
                                      True)[1]
app2._inject_key = lambda u32, ev: (_applied5.append(("key", dict(ev))),
                                    True)[1]
app2.net.connected = True               # link up: input may be armed
check("[task-5] nothing is armed and no indicator before any command",
      app2._remote_active is False and app2._remote_session is None
      and getattr(app2, "_remote_banner", None) is None)
app2.handle_command(Message.create(MessageType.CMD_REMOTE_START,
                                   {"command_id": "rs1",
                                    "params": {"admin_user": "tester"}}))
app2.update()
check("[task-5] an authorised CMD_REMOTE_START arms the input gate",
      app2._remote_active is True and app2._remote_session == "rs1",
      str((app2._remote_active, app2._remote_session)))
check("[task-5] the session start is acknowledged back to the Server",
      any(getattr(m, "payload", {}).get("command_id") == "rs1"
          and m.payload.get("success") for m in _sent5),
      str([getattr(m, "payload", {}) for m in _sent5][-3:]))
_t5 = " ".join(_widget_text(app2))
check("[task-5] a visible REMOTE CONTROL ACTIVE indicator is raised",
      getattr(app2, "_remote_banner", None) is not None
      and app2._remote_banner.winfo_exists()
      and "REMOTE CONTROL ACTIVE" in _t5, _t5[:200])
check("[task-5] the indicator is NOT the removed observing overlay",
      "OBSERVING" not in _t5 and "being viewed" not in _t5
      and not hasattr(app2, "_show_observing_banner"), _t5[:200])

# the whitelist is enforced again on the CLIENT, before anything is applied
check("[task-5] a whitelisted batch reaches the injector",
      app2._apply_remote_input({"session_id": "rs1", "events": [
          {"kind": "mouse", "action": "move", "x": 0.5, "y": 0.5},
          {"kind": "key", "action": "down", "keysym": "a", "char": "a"}]})
      == 2 and len(_applied5) == 2, str(_applied5))
_applied5.clear()
check("[task-5] a wrong session id applies NOTHING",
      app2._apply_remote_input({"session_id": "OTHER", "events": [
          {"kind": "mouse", "action": "move", "x": 0.5, "y": 0.5}]}) == 0
      and _applied5 == [], str(_applied5))
check("[task-5] no shell/exec primitive can ever be forwarded",
      app2._apply_remote_input({"session_id": "rs1", "events": [
          {"kind": "exec", "action": "run", "keysym": "rm -rf /"}]}) == 0
      and _applied5 == [], str(_applied5))
check("[task-5] a flood is capped by the whitelist",
      len(client.sanitize_remote_events(
          [{"kind": "key", "action": "down", "keysym": "a"}] * 900)) == 64)

# a lost link closes the gate before another batch can be applied
app2.net.connected = False
check("[task-5] a lost link disarms the session immediately",
      app2._apply_remote_input({"session_id": "rs1", "events": [
          {"kind": "mouse", "action": "move", "x": 0.5, "y": 0.5}]}) == 0
      and app2._remote_active is False
      and getattr(app2, "_remote_banner", None) is None
      and _applied5 == [], str((app2._remote_active, _applied5)))

# re-arm, then prove a stale stop can never disarm a live session
app2.net.connected = True
app2.handle_command(Message.create(MessageType.CMD_REMOTE_START,
                                   {"command_id": "rs2"}))
app2.update()
check("[task-5] a session can be armed again after a disconnect",
      app2._remote_active is True and app2._remote_session == "rs2")
app2.handle_command(Message.create(MessageType.CMD_REMOTE_STOP,
                                   {"command_id": "st1",
                                    "session_id": "rs1"}))
check("[task-5] a stale stop never disarms a live session",
      app2._remote_active is True and app2._remote_session == "rs2",
      str((app2._remote_active, app2._remote_session)))
app2.handle_command(Message.create(MessageType.CMD_REMOTE_STOP,
                                   {"command_id": "st2",
                                    "session_id": "rs2"}))
app2.update()
check("[task-5] the matching stop ends the session and the indicator",
      app2._remote_active is False and app2._remote_session is None
      and getattr(app2, "_remote_banner", None) is None)
check("[task-5] input after the stop applies nothing",
      app2._apply_remote_input({"session_id": "rs2", "events": [
          {"kind": "mouse", "action": "move", "x": 0.5, "y": 0.5}]}) == 0
      and _applied5 == [], str(_applied5))
_p5 = _ls.pending_logs(500)
check("[task-5] the local store records the session start and stop",
      any(e.get("message") == "Remote control started" for e in _p5)
      and any(e.get("message") == "Remote control stopped" for e in _p5),
      str([e.get("message") for e in _p5][-6:]))
check("[task-5] the local records name their session ids",
      any("rs1" in str(e.get("detail")) for e in _p5
          if e.get("message") == "Remote control started")
      and any("rs2" in str(e.get("detail")) for e in _p5
              if e.get("message") == "Remote control stopped"))
app2.net.send = _orig_send5
app2.net.connected = False              # restore the state the tests assume

app2.stop_observation()
app2.net.stop()
app2.destroy()

# --- restore whatever config existed before the test -----------------------
if had_config and backup is not None:
    with open(cfg_file, "w", encoding="utf-8") as f:
        f.write(backup)
elif os.path.exists(cfg_file):
    os.remove(cfg_file)

# --- P2: Windows startup registration (HKCU Run, idempotent) --------------
import subprocess
import winreg
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

def _read_run_value():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as _k:
            return winreg.QueryValueEx(
                _k, "Computer Laboratory Client")[0]
    except OSError:
        return None

check("[startup] no Run entry before the test", _read_run_value() is None,
      str(_read_run_value()))
ok1 = client.register_startup()
val1 = _read_run_value()
check("[startup] register writes the Run entry",
      ok1 is True and isinstance(val1, str) and "client.py" in val1,
      f"ok={ok1} val={val1}")
check("[startup] second register is idempotent",
      client.register_startup() is True and _read_run_value() == val1,
      str(_read_run_value()))
# CLI contract: `client.py --uninstall-startup` removes the entry and
# never opens the kiosk (second process - no hooks, no window).
_here = os.path.dirname(os.path.abspath(__file__))
proc = subprocess.run([sys.executable, os.path.join(_here, "client.py"),
                       "--uninstall-startup"],
                      capture_output=True, timeout=90, cwd=_here)
check("[startup] --uninstall-startup exits cleanly (no kiosk)",
      proc.returncode == 0,
      f"rc={proc.returncode} err={(proc.stderr or b'')[-300:]}")
check("[startup] Run entry removed by --uninstall-startup",
      _read_run_value() is None, str(_read_run_value()))
# the same uninstall also drops both P1-4 watchdog tasks (no client is
# ever restarted by Windows once the kiosk has been uninstalled)
_wd_l = client._task_exists(client.WATCHDOG_TASK_LOGON)
_wd_r = client._task_exists(client.WATCHDOG_TASK_REPEAT)
check("[startup] --uninstall-startup clears the watchdog tasks",
      not _wd_l and not _wd_r, f"logon={_wd_l} repeat={_wd_r}")

print(flush=True)
if FAIL:
    print(f"*** {len(FAIL)} FAILURES: {FAIL}", flush=True)
    raise SystemExit(1)
print("ALL CLIENT GUI TESTS PASSED", flush=True)
