import subprocess, time
# Pre-compile the NetTCPIP CIM module so the suite's cold _ps() call
# (timeout=12s) does not pay the ~12s first-use cost. Warm-up only.
cmd = ("(Get-DnsClientServerAddress -InterfaceAlias '__no_such_adapter__' "
       "-AddressFamily IPv4 -ErrorAction SilentlyContinue).ServerAddresses "
       "| ConvertTo-Json -Compress")
t0 = time.time()
r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                    "-Command", cmd],
                   capture_output=True, text=True, timeout=60,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
print(f"warm rc={r.returncode} {time.time()-t0:.1f}s")
