"""
startup_ids.py - the Windows auto-start names, in ONE place.

The Client registers these on every start; ``dist\\uninstall.exe``
removes them.  Both import them from here, so the uninstaller can
never drift from what it is supposed to uninstall.
"""

# Task Scheduler tasks (created by client.register_watchdog)
WATCHDOG_TASK_LOGON = "Computer Laboratory Client Logon"
WATCHDOG_TASK_REPEAT = "Computer Laboratory Client Watchdog"

# HKCU Run entry (created by client.register_startup)
STARTUP_VALUE_NAME = "Computer Laboratory Client"
STARTUP_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"

# HKCU policy values the Client hardens while a kiosk screen is up
# (client.cad_policy_apply) and clears again on the bare desktop
# (client.cad_policy_clear).  uninstall.exe clears exactly these too.
# (registry path, value name); every one is a REG_DWORD 1.
CAD_POLICY_VALUES = (
    (r"Software\Microsoft\Windows\CurrentVersion\Policies\System",
     "DisableTaskMgr"),
    (r"Software\Microsoft\Windows\CurrentVersion\Policies\System",
     "DisableLockWorkstation"),
    (r"Software\Microsoft\Windows\CurrentVersion\Policies\System",
     "DisableChangePassword"),
    (r"Software\Microsoft\Windows\CurrentVersion\Policies\System",
     "HideFastUserSwitching"),
    (r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer",
     "NoLogoff"),
)

