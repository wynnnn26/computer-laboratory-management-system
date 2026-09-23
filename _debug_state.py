import os, json
# Reproduce test conditions: NO config file
cfg_path = None
import client
cfg_path = client.config_path()
had_config = os.path.exists(cfg_path)
backup = None
if had_config:
    backup = open(cfg_path, encoding="utf-8").read()
    os.remove(cfg_path)
print("config exists now:", os.path.exists(cfg_path))

# Class-level stub so the constructor's after(150, ...) schedules the stub
client.HotkeyBlocker.start = lambda self: None
client.HotkeyBlocker.stop = lambda self: None
client.HotkeyBlocker.shutdown = lambda self: None
client.ClientApp._ask_server_config = lambda self: None

app = client.ClientApp()
app.update()
print("1) init      -> state=%r viewable=%s" % (app.state, app.winfo_viewable()))
print("   root children mapped:", app.winfo_ismapped())

app._on_auth_ok({"student_id": "2023-00001", "full_name": "Juan",
                 "role": "student"})
app.update()
print("2) auth      -> state=%r viewable=%s session=%s bar=%s" % (
    app.state, app.winfo_viewable(), bool(app.session_id), app.bar.winfo_viewable()))

app.destroy()

# restore original config
if had_config and backup is not None:
    with open(cfg_path, "w", encoding="utf-8") as f:
        f.write(backup)
    print("config restored")
