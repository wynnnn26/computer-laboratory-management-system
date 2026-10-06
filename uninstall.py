"""
uninstall.py - stop and remove the Laboratory Client from this PC.

Built to ``dist\\uninstall.exe`` (PyInstaller, requests elevation).
Removes, in this order:

  1. both watchdog tasks        - nothing can restart the client
  2. the HKCU Run entry         - it cannot start at sign-in
  3. the HKCU CAD policies       - Ctrl+Alt+Del is fully normal again
  4. every running client.exe   - the kiosk really stops
  5. the local audit log        - exported to CSV first; kept, not
                                  deleted, when that export fails
  6. the client files           - client.exe, lab_client.db*, lab_config.json

Every step reports its own outcome (failures start with "FAILED:", never
a polite lie), the result is shown in a message box, and the app then
deletes itself.  `--quiet` skips the dialog for scripted use.

Importing this module NEVER runs anything - all work lives in main().
The task and Run-entry names come from startup_ids, the same module the
Client registers them from, so this can never drift from what it removes.
server.crt/server.key (Server files) and the exported CSV (the operator's
data) are never touched.
"""

import glob
import os
import subprocess
import sys

from startup_ids import (WATCHDOG_TASK_LOGON, WATCHDOG_TASK_REPEAT,
                         STARTUP_KEY_PATH, STARTUP_VALUE_NAME,
                         CAD_POLICY_VALUES)

CLIENT_IMAGE = "client.exe"
# the files a Client install owns in THIS folder
CLIENT_FILE_PATTERNS = ("client.exe", "lab_client.db*", "lab_config.json")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(args):
    """One command with no console window.  (rc, output); never raises."""
    try:
        p = subprocess.run(list(args), capture_output=True, text=True,
                           timeout=30, creationflags=_NO_WINDOW)
        out = " ".join(x for x in ((p.stdout or "").strip(),
                                   (p.stderr or "").strip()) if x)
        return int(p.returncode), out
    except Exception as e:
        return 1, str(e)


def remove_tasks():
    """Delete both watchdog tasks; a missing task counts as done."""
    out = []
    for name in (WATCHDOG_TASK_LOGON, WATCHDOG_TASK_REPEAT):
        rc, text = _run(["schtasks", "/Delete", "/TN", name, "/F"])
        low = text.lower()
        if rc == 0:
            out.append(f"Task removed: {name}")
        elif "cannot find" in low or "not found" in low:
            out.append(f"Task already gone: {name}")
        else:
            out.append(f"FAILED: task not removed: {name} ({text})")
    return out


def remove_run_entry():
    """Delete the current user's Run entry; missing counts as done."""
    try:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY_PATH,
                                0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, STARTUP_VALUE_NAME)
            return f"Startup entry removed: {STARTUP_VALUE_NAME}"
        except FileNotFoundError:
            return f"Startup entry already gone: {STARTUP_VALUE_NAME}"
    except Exception as e:
        return f"FAILED: startup entry not removed: {e}"


def remove_cad_policy():
    """Clear the Ctrl+Alt+Del restrictions the Client applies while a
    kiosk screen is up (Bug 2).  The registry survives file deletion, so
    the uninstaller must free it explicitly; missing values count as
    done.  Names come from startup_ids - the same source the Client
    writes them from, so this can never drift."""
    try:
        import winreg
    except Exception as e:
        return [f"FAILED: Ctrl+Alt+Del restrictions not cleared: {e}"]
    out = []
    for path, name in CAD_POLICY_VALUES:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, name)
            out.append(f"Ctrl+Alt+Del restriction cleared: {name}")
        except FileNotFoundError:
            out.append(f"Ctrl+Alt+Del restriction already clear: {name}")
        except Exception as e:
            out.append(f"FAILED: Ctrl+Alt+Del restriction not cleared: "
                       f"{name} ({e})")
    return out


def stop_client():
    """Kill every client.exe, then verify it is really gone.

    The verify-after is the honest half: taskkill on an elevated process
    from a non-elevated run fails silently, and the caller must be told
    instead of being shown a green result over a running kiosk."""
    _run(["taskkill", "/F", "/IM", CLIENT_IMAGE])
    _rc, text = _run(["tasklist", "/FI", f"IMAGENAME eq {CLIENT_IMAGE}"])
    if CLIENT_IMAGE.lower() in text.lower():
        return ("FAILED: client.exe is still running - run this "
                "uninstall app as Administrator")
    return "Client stopped (client.exe is not running)"


def _base_dir():
    """The install folder (frozen: next to uninstall.exe)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def export_and_remove_files():
    """Export the local audit log to CSV, then delete the client files.

    The export runs BEFORE the database goes (P1-6: an uninstall must
    never silently throw the audit trail away).  If the export fails the
    database is KEPT - a reported FAILED line beats lost audit data."""
    msgs = []
    db_kept = False
    try:
        import local_store
        if os.path.exists(local_store.db_path()):
            path = local_store.export_logs()
            if path:
                msgs.append(f"Local log exported: {path}")
            else:
                db_kept = True
                msgs.append("FAILED: local log could not be exported - "
                            "the database is kept")
        else:
            msgs.append("No local log to export")
    except Exception as e:
        db_kept = True
        msgs.append(f"FAILED: local log export error: {e} - "
                    "the database is kept")
    base = _base_dir()
    for pattern in CLIENT_FILE_PATTERNS:
        if db_kept and pattern.startswith("lab_client.db"):
            continue                      # unexported audit data survives
        for path in glob.glob(os.path.join(base, pattern)):
            if os.path.abspath(path) == os.path.abspath(sys.executable):
                continue                      # self-delete is the last step
            try:
                os.remove(path)
                msgs.append(f"File removed: {os.path.basename(path)}")
            except OSError as e:
                msgs.append(f"FAILED: could not remove "
                            f"{os.path.basename(path)}: {e}")
    return msgs


def _self_delete():
    """uninstall.exe removes itself a moment after this process exits."""
    if not getattr(sys, "frozen", False):
        return
    try:
        subprocess.Popen(
            f'ping -n 3 127.0.0.1 >nul & del /f /q "{sys.executable}"',
            shell=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except Exception:
        pass


def _report(lines, ok, quiet):
    """Result dialog (TOPMOST, like the kiosk's own message boxes)."""
    if quiet:
        print("\n".join(lines))
        return
    try:
        import ctypes
        MB_ICONINFORMATION, MB_ICONWARNING, MB_TOPMOST = 0x40, 0x30, 0x00040000
        ctypes.windll.user32.MessageBoxW(
            None, "\n".join(lines),
            "Uninstall Laboratory Client",
            (MB_ICONINFORMATION if ok else MB_ICONWARNING) | MB_TOPMOST)
    except Exception:
        pass


def main(argv=None):
    """Run every removal step once.  Returns 0 when nothing FAILED."""
    argv = list(sys.argv[1:] if argv is None else argv)
    lines = []
    lines += remove_tasks()
    lines.append(remove_run_entry())
    lines += remove_cad_policy()
    lines.append(stop_client())
    lines += export_and_remove_files()
    ok = not any(l.startswith("FAILED") for l in lines)
    lines.append("This uninstall app deletes itself now." if ok
                 else "Fix the FAILED line(s) above and run it again.")
    _report(lines, ok, "--quiet" in argv)
    if ok:
        _self_delete()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
