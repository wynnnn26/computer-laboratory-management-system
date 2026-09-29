"""
dns_filter.py - Website Access policy helpers + adapter-DNS repair
(spec items 6/7/8).

Two parts, both free of sockets at import time:

1. PURE decision logic (module level, unit-testable):
   normalize() / domain_matches() / decide() turn a policy snapshot
   {version, mode, blocked, allowed} into a per-hostname verdict.
   Matching is suffix based:
       example.com    -> matches example.com, www.example.com,
                         subdomain.example.com
       notexample.com -> never matches example.com
   web_access keeps these semantics exactly, so the verdict on a
   detection event is the same one the policy always meant.

2. Windows adapter-DNS REPAIR (P1.0).  The DNS proxy that used to live
   here answered on 127.0.0.1:53 and repointed the adapter at itself -
   and any run killed before its restore (watchdog restart, crash,
   power cut) left the PC with NO name resolution at all.  That
   enforcement is GONE: Website Access is enforced by connection
   detection (web_access.py), and nothing here repoints an adapter any
   more.  What remains is the safety net that heals such a leftover -
   get_adapter_dns(), probe_local_resolver(), repair_adapter_dns() -
   and it never touches an adapter that is not stuck, never wipes a
   local resolver that still answers, and only reports success after
   re-reading the adapter.
"""

import os
import socket
import struct
import subprocess
import time

MODES = ("allow_all", "block_list", "allow_only")


# Loopback DNS entries - the ONLY values this module ever treats as a
# STUCK adapter, and the one thing a "previous"/"restore" value must
# never be.  Reading the previous servers *after* a repoint (or after a
# restart that never restored) recorded 127.0.0.1 as its own ancestor,
# so ALLOW ALL restored 127.0.0.1 and the PC lost name resolution for
# good.
LOCAL_DNS = ("127.0.0.1", "::1", "localhost")


def is_local_dns(servers):
    """True when any entry points at a loopback resolver."""
    return any(str(s or "").strip().lower() in LOCAL_DNS
               for s in servers or [])


def drop_local_dns(servers):
    """The entries that are NOT loopback - what a restore must write."""
    return [str(s).strip() for s in (servers or [])
            if str(s or "").strip().lower() not in LOCAL_DNS]


# ==========================================================================
# Pure decision logic
# ==========================================================================
def normalize(name):
    """Lowercase, trim, drop a trailing dot, punycode IDN labels."""
    n = str(name or "").strip().lower().rstrip(".")
    if not n:
        return ""
    try:
        n = n.encode("idna").decode("ascii")
    except Exception:
        pass
    return n


def domain_matches(host, rule):
    """True when host is rule or a subdomain of rule (never notexample.com
    for rule example.com)."""
    host, rule = normalize(host), normalize(rule)
    if not host or not rule:
        return False
    return host == rule or host.endswith("." + rule)


def decide(host, policy):
    """Evaluate a hostname against a policy snapshot.

    Returns True to ALLOW the hostname, False to DENY it - the same
    verdict web_access.WebAccessDetector.violations() reports, because
    it calls this function.  Unknown modes behave like allow_all (fail
    open; the server only ever sends the three known modes)."""
    pol = policy or {}
    mode = pol.get("mode") or "allow_all"
    host = normalize(host)
    if not host or mode == "allow_all":
        return True
    if mode == "block_list":
        return not any(domain_matches(host, r) for r in pol.get("blocked") or [])
    if mode == "allow_only":
        return any(domain_matches(host, r) for r in pol.get("allowed") or [])
    return True


# ==========================================================================
# Windows helpers (admin check + adapter DNS read/restore/repair)
# ==========================================================================
def is_admin():
    if os.name == "nt":
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    try:
        return os.geteuid() == 0
    except Exception:
        return False


def active_adapter():
    """Name of the active non-loopback IPv4 adapter, or None."""
    try:
        import psutil
        stats = psutil.net_if_stats()
        addrs = psutil.net_if_addrs()
        for name, st in stats.items():
            if not st.isup:
                continue
            low = name.lower()
            if low.startswith(("lo", "loopback", "vethernet", "isatap",
                               "teredo", "tap-windows", "wg")):
                continue
            if any(a.family == socket.AF_INET
                   and not a.address.startswith("127.")
                   for a in addrs.get(name, [])):
                return name
    except Exception:
        pass
    return None


def _ps(script):
    """Run a PowerShell snippet hidden; returns (rc, stdout)."""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=12,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return proc.returncode, (proc.stdout or "").strip()
    except Exception as e:
        return 1, str(e)


def get_adapter_dns(adapter):
    """Current IPv4 DNS servers of the adapter ([] = DHCP or unreadable).

    NOTE: -ErrorAction has to sit INSIDE the cmdlet call.  Hanging it off
    the .ServerAddresses property (as the original code did) is a parse
    error, so the query silently returned nothing - which is why the old
    "previous" list was always empty and a repair could never see that
    the adapter was still pointed at 127.0.0.1."""
    if not adapter:
        return []
    rc, out = _ps(
        "(Get-DnsClientServerAddress -InterfaceAlias "
        f"'{adapter.replace(chr(39), chr(39) * 2)}' -AddressFamily IPv4"
        " -ErrorAction SilentlyContinue).ServerAddresses "
        "| ConvertTo-Json -Compress")
    if rc != 0 or not out:
        return []
    try:
        import json
        servers = json.loads(out)
    except Exception:
        return []
    if isinstance(servers, str):
        servers = [servers]
    return [str(s) for s in servers or []]


def _set_dns_servers(adapter, servers):
    esc = adapter.replace("'", "''")
    addrs = ",".join(f"'{s}'" for s in servers)
    return _ps("Set-DnsClientServerAddress -InterfaceAlias "
               f"'{esc}' -ServerAddresses @({addrs}) | Out-Null")


def _reset_dns_servers(adapter):
    esc = adapter.replace("'", "''")
    return _ps("Set-DnsClientServerAddress -InterfaceAlias "
               f"'{esc}' -ResetServerAddresses | Out-Null")


def restore_adapter_dns(adapter, previous):
    """Write a known-good server list back to an adapter, or reset it to
    DHCP/default when that list is empty.  Loopback entries are dropped
    first - they are the very thing being healed, never a value to write.

    Returns (ok, error) - the caller must be able to report a restore
    that did NOT happen instead of silently claiming the policy is off."""
    if not adapter:
        return False, "no adapter to restore"
    servers = drop_local_dns(previous)
    if servers:
        rc, err = _set_dns_servers(adapter, servers)
    else:
        rc, err = _reset_dns_servers(adapter)
    if rc != 0:
        return False, err or "Set-DnsClientServerAddress failed"
    return True, ""


# ------------------------------------------------- repair (stuck DNS)
def adapter_guid(alias):
    """InterfaceGuid of the adapter (best effort, '' when unknown).

    Windows can rename a display alias, so a snapshot stores the GUID
    alongside the name and repair prefers whichever still resolves."""
    if not alias:
        return ""
    esc = alias.replace("'", "''")
    rc, out = _ps(f"(Get-NetAdapter -Name '{esc}' "
                  "-ErrorAction SilentlyContinue).InterfaceGuid")
    return str(out).strip().strip("{}") if rc == 0 and out else ""


def resolve_adapter(guid="", alias=""):
    """Current display alias for a stored adapter identity, or '' when
    neither resolves - in which case repair must touch nothing."""
    if guid:
        g = str(guid).strip().strip("{}")
        if g:
            rc, out = _ps(
                "Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object "
                f"{{$_.InterfaceGuid.ToString() -eq '{g}'}} | "
                "Select-Object -First 1 -ExpandProperty Name")
            if rc == 0 and out:
                return str(out).strip()
    if alias:
        esc = alias.replace("'", "''")
        rc, out = _ps(f"(Get-NetAdapter -Name '{esc}' "
                      "-ErrorAction SilentlyContinue).Name")
        if rc == 0 and out:
            return str(out).strip()
    return ""


def _query_packet(name="example.com"):
    """Minimal DNS query packet (A, recursion desired) for the probe."""
    qid = int.from_bytes(os.urandom(2), "big")
    labels = [l for l in str(name or "").strip(".").split(".") if l]
    q = b"".join(bytes([len(l)]) + l.encode("ascii", "ignore")
                 for l in labels)
    return (struct.pack("!HHHHHH", qid, 0x0100, 1, 0, 0, 0)
            + q + b"\x00" + struct.pack("!HH", 1, 1))


def probe_local_resolver(host="127.0.0.1", port=53, timeout=0.6,
                         name="example.com"):
    """True when *something* answers a DNS query at host:port.

    This is the guardrail that tells a leftover repoint from our Client
    (dead: nothing is listening on 127.0.0.1:53) apart from a local
    resolver an administrator deliberately installed (it answers).
    repair_adapter_dns() must never wipe the latter."""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.sendto(_query_packet(name), (host, port))
        data, _addr = sock.recvfrom(512)
        return bool(data)
    except Exception:
        return False
    finally:
        try:
            if sock is not None:
                sock.close()
        except Exception:
            pass


def repair_adapter_dns(snapshot=None, adapter=None, probe_timeout=0.6):
    """Heal a leftover loopback repoint (the PC has no name resolution).

    Returns (acted, ok, error):
      acted=False, ok=True   -> nothing to do (not repointed, unreadable,
                                or a live local resolver answers)
      acted=True,  ok=True/False -> the adapter was changed; ok says the
                                write was verified by re-reading it

    Guardrails:
      * only an adapter whose DNS IS loopback is ever touched
      * a local resolver that ANSWERS is left alone (an administrator's
        own resolver, or our filter still running and enforcing)
      * the exact pre-repoint servers come from the persisted snapshot;
        otherwise only the loopback entry is dropped, and a DHCP reset
        is the last resort - never a blind wipe
      * adapter identity resolves by GUID when the stored alias is gone
      * success is only reported after re-reading the adapter and seeing
        that the loopback entry is really gone"""
    snap = snapshot if isinstance(snapshot, dict) else {}
    alias = str(snap.get("alias") or adapter or active_adapter() or "")
    cur = get_adapter_dns(alias)
    if not cur and snap.get("guid"):
        # the stored alias may have been renamed - resolve it by GUID
        alt = resolve_adapter(snap.get("guid") or "", "")
        if alt and alt != alias:
            alias, cur = alt, get_adapter_dns(alt)
    if not is_local_dns(cur):
        return False, True, ""            # never repointed - touch nothing
    if probe_local_resolver(timeout=probe_timeout):
        return False, True, ""            # a live resolver - hands off
    servers = drop_local_dns(snap.get("servers") or []) or drop_local_dns(cur)
    if servers:
        rc, err = _set_dns_servers(alias, servers)
    else:
        rc, err = _reset_dns_servers(alias)
    if rc != 0:
        return True, False, err or "Set-DnsClientServerAddress failed"
    after = get_adapter_dns(alias)
    if is_local_dns(after):
        time.sleep(0.3)
        after = get_adapter_dns(alias)
    if is_local_dns(after):
        return True, False, "adapter DNS still points at a local resolver"
    return True, True, ""
