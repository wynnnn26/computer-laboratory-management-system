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
