"""
web_access.py - Website Access detection (P1.1 / P1.2)

Detects ACTUAL website access instead of intercepting name resolution:

    connection seen  ->  domain  ->  responsible browser  ->  verdict

Why this replaces the old DNS / hosts enforcement:

  * the old local DNS filter repointed the adapter at 127.0.0.1, and any
    run killed before it could restore (watchdog restart, crash, power
    cut) left the PC with no name resolution at all - see
    dns_filter.repair_adapter_dns, shipped as P1.0
  * browsers default to DNS-over-HTTPS, so a local resolver never sees
    most lookups in the first place
  * a socket is proof that access happened; a blocked domain sitting in
    the rule list is not - "do not close browsers merely because a
    blocked domain exists"

Safety rules baked in here (the P1.1 / P1.2 guardrails):

  * ALLOW ALL never scans anything: no connections read, no events
  * an IP shared by policy domains with DIFFERENT verdicts (a CDN) is
    AMBIGUOUS -> recorded UNRESOLVED, nothing is ever killed
  * when the owning process cannot be identified -> UNRESOLVED, never a guess
  * only chrome / edge / firefox are ever "the responsible browser"
  * this module NEVER touches the Windows adapter DNS; upstream_dns is
    only ever a forwarding target for our own lookups
  * every entry point never raises - a dead detector must not disturb
    the kiosk

A browser is only ever closed through the five-condition gate in
can_close(): one re-validated PID, never an image name, never a process
tree, never during a cooldown, and only for a DETECTED event with an
unambiguous domain (P1.2).
"""

import os
import socket
import struct
import subprocess
import threading
import time

import psutil

from dns_filter import normalize, decide
from startup_ids import FIREWALL_QUIC_RULE

MODES = ("allow_all", "block_list", "allow_only")

# the browsers we are allowed to consider responsible.  Originally
# chrome/edge/firefox only (Bug 3); Brave, Opera and Vivaldi are Chrome
# builds that run the same enforcement, and a lab that blocks on them
# must not fall through to UNRESOLVED (never closed).
BROWSERS = ("chrome.exe", "msedge.exe", "firefox.exe",
            "brave.exe", "opera.exe", "vivaldi.exe")

# connection states that count as evidence of an attempt or a visit
# (TIME_WAIT/FIN_* catch a page that opened and closed between polls)
LIVE_STATES = ("ESTABLISHED", "SYN_SENT", "SYN_RECV", "FIN_WAIT1",
               "FIN_WAIT2", "CLOSE_WAIT", "LAST_ACK", "TIME_WAIT",
               "CLOSING", "CLOSED", "CONNECTING", "LISTEN")

# emitted once per continuous connection episode; a key is forgotten this
# many seconds after the connection stops being sighted
EPISODE_TTL = 5.0

# background re-resolution cadence for the policy domain -> IP cache
REFRESH_INTERVAL = 300.0

# minimum gap between two closes of the SAME browser on the SAME domain:
# one violation closes the browser, hammering re-opens it, and we must
# not turn that into a kill loop
KILL_COOLDOWN = 60.0


def set_udp443_block(enabled):
    """Create/remove the machine-wide outbound UDP 443 (QUIC) rule (Bug 3).

    Chrome/Brave/Edge and friends prefer HTTP/3 over UDP 443, and a UDP
    socket on Windows has no remote address - the scanner in
    _connections() can neither see it nor attribute it to a domain, so a
    blocked site in Chrome/Brave never triggered.  Forcing QUIC off
    machine-wide makes every browser fall back to TLS on TCP 443, where
    the scanner already sees Edge today.

    Idempotent (enable deletes before adding - netsh allows duplicate
    names, and every kiosk start must not stack them), never raises, and
    never blocks a start: without elevation netsh refuses and the caller
    logs the refusal and continues.  Returns (ok, detail)."""
    if os.name != "nt":
        return False, "not Windows - no firewall rule"

    def _netsh(args):
        try:
            p = subprocess.run(
                list(args), capture_output=True, text=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            text = " ".join(x for x in ((p.stdout or "").strip(),
                                        (p.stderr or "").strip()) if x)
            return int(p.returncode), text
        except Exception as e:
            return 1, str(e)

    if not enabled:
        rc, text = _netsh(["netsh", "advfirewall", "firewall", "delete",
                           "rule", f"name={FIREWALL_QUIC_RULE}"])
        low = text.lower()
        if "no rules match" in low or "not found" in low:
            return True, ""              # already gone - rerun-safe
        return (True, "") if rc == 0 else (False, text or "netsh refused")
    # delete first: a restart must never stack a second copy of the rule
    _netsh(["netsh", "advfirewall", "firewall", "delete",
            "rule", f"name={FIREWALL_QUIC_RULE}"])
    rc, text = _netsh(["netsh", "advfirewall", "firewall", "add", "rule",
                       f"name={FIREWALL_QUIC_RULE}", "dir=out",
                       "action=block", "protocol=UDP", "remoteport=443"])
    if rc == 0:
        return True, ""
    low = text.lower()
    if "elevation" in low or "administrator" in low:
        return False, ("not applied - run the Client as Administrator "
                       f"(netsh: {text})")
    return False, text or "netsh refused the rule"

def _clean_ip(raw):
    """Normalize a peer address: drop an IPv6 zone id and brackets."""
    ip = str(raw or "").strip()
    if "%" in ip:
        ip = ip.split("%", 1)[0]
    if ip.startswith("[") and ip.endswith("]"):
        ip = ip[1:-1]
    return ip.strip("[]")


def is_public_ip(ip):
    """True for an address that can legitimately be a website.

    Loopback, RFC1918, link-local, multicast and reserved space are
    local resources - not "a website", and never safely attributable to
    a domain, so they are never events."""
    ip = _clean_ip(ip)
    if not ip:
        return False
    if ":" in ip:                                  # IPv6
        low = ip.lower()
        if low == "::" or low.startswith(("fe80:", "fe9", "fea", "feb",
                                          "fc", "fd", "::1")):
            return False
        return True
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    if not 1 <= a <= 223 or a == 127:
        return False                               # 0/8, 127/8, >=240
    if a == 10:
        return False
    if a == 192 and b == 168:
        return False
    if a == 172 and 16 <= b <= 31:
        return False
    if a == 169 and b == 254:
        return False                               # link-local
    if 224 <= a <= 239:
        return False                               # multicast
    return True


# ==========================================================================
# Our own lookups - never the adapter's DNS
# ==========================================================================
def _skip_name(data, i):
    """Advance past a (possibly compressed) DNS name."""
    while i < len(data):
        ln = data[i]
        if ln == 0:
            return i + 1
        if ln & 0xC0:
            return i + 2                        # compression pointer
        i += 1 + ln
    return len(data)


def parse_answers(data):
    """[(type, address), ...] for the A/AAAA records in a DNS response."""
    out = []
    try:
        if not data or len(data) < 12:
            return out
        qd, an = struct.unpack("!HH", data[4:8])
        i = 12
        for _ in range(qd):
            i = _skip_name(data, i) + 4
        for _ in range(an):
            i = _skip_name(data, i)
            if i + 10 > len(data):
                break
            qtype, _qcls, _ttl, rdlen = struct.unpack("!HHIH", data[i:i + 10])
            i += 10
            rdata = data[i:i + rdlen]
            i += rdlen
            if qtype == 1 and len(rdata) == 4:
                out.append(("A", socket.inet_ntoa(rdata)))
            elif qtype == 28 and len(rdata) == 16:
                out.append(("AAAA",
                            socket.inet_ntop(socket.AF_INET6, rdata)))
    except Exception:
        return out
    return out


def _raw_query(server, name, qtype, timeout=2.0):
    """One UDP DNS query at `server:53`.  Returns the response or None.

    `server` is only ever a FORWARDING TARGET for our own lookup - it is
    never written into the Windows adapter configuration."""
    from dns_filter import _query_packet
    sock = None
    try:
        pkt = _query_packet(name)
        # rebuild with the wanted qtype (the helper always asks for A)
        pkt = pkt[:-4] + struct.pack("!HH", qtype, 1)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        sock.sendto(pkt, (str(server), 53))
        data, _ = sock.recvfrom(4096)
        return data
    except Exception:
        return None
    finally:
        try:
            if sock is not None:
                sock.close()
        except Exception:
            pass


def resolve_domain(name, upstream=None, timeout=2.0):
    """All A/AAAA addresses for a hostname: the UNION of both views.

    A browser resolves through the SYSTEM resolver (or DoH, whose view
    matches a public upstream), while this Client asks `upstream_dns`
    directly (per the P1 decision: upstream_dns is the Client's lookup
    resolver and NOTHING else).  The two views genuinely differ for
    common sites - x.com, tiktok.com and accounts.google.com each
    resolved to DISJOINT address sets per view - so mapping only the
    upstream answer let the browser connect to an address the detector
    never learned: no match, no event, and blocking silently did
    nothing.  Both views are therefore always collected; returns a set
    - never raises."""
    name = normalize(name)
    found = set()
    if upstream and name:
        for qtype in (1, 28):
            for _t, addr in parse_answers(
                    _raw_query(upstream, name, qtype, timeout)):
                found.add(addr)
    # the system view is ALWAYS included - upstream answering must no
    # longer short-circuit it (that skip was the missed connection)
    try:
        for _family, _type, _proto, _canon, sa in socket.getaddrinfo(
                name or "localhost", None):
            ip = _clean_ip(sa[0])
            if ip:
                found.add(ip)
    except Exception:
        pass
    return found


# ==========================================================================
# Capability probe - honest reporting when we cannot see other processes
# ==========================================================================
def probe_access():
    """(ok, error): can this process list foreign sockets AND read the
    owning process?

    Without both, detection cannot work.  The caller must report FAILED
    instead of pretending the policy is enforced - the same honesty the
    old DNS path owed the server and did not give it."""
    try:
        conns = psutil.net_connections(kind="inet")
    except Exception as e:
        return False, f"cannot list network connections: {e}"
    for c in conns:
        if c.pid and c.pid != os.getpid():
            try:
                psutil.Process(c.pid).name()
                return True, ""
            except psutil.AccessDenied:
                return False, ("cannot identify which process owns "
                               "another application's connection - run the "
                               "Client as Administrator")
            except Exception:
                continue
    # nothing foreign open right now; the listing itself worked
    return True, ""


def process_name(pid):
    """Best-effort process image name ('' when it cannot be read)."""
    try:
        if not pid or pid == os.getpid():
            return ""
        return str(psutil.Process(int(pid)).name() or "").lower()
    except Exception:
        return ""


def _create_time(pid):
    """Process creation time (0.0 when unreadable).

    Recorded with the event so a PID that has been recycled into some
    other process between poll() and close() is detected instead of
    terminated."""
    try:
        return float(psutil.Process(int(pid)).create_time())
    except Exception:
        return 0.0


def browser_root(pid):
    """The top-most browser process (see BROWSERS) for a connection
    owner.

    Chrome/Edge/Brave are multi-process: the socket usually belongs to a
    network-service child, and killing that child leaves the browser
    running.  Returns (root_pid, image_name) or (0, "") when the chain
    does not end in a browser we are allowed to touch."""
    try:
        proc = psutil.Process(int(pid))
    except Exception:
        return 0, ""
    name = str(proc.name() or "").lower()
    if name not in BROWSERS:
        return 0, ""
    root, root_name = proc, name
    for _ in range(32):                          # bounded: no cycle chasing
        try:
            parent = proc.parent()
        except Exception:
            break
        if parent is None:
            break
        pname = str(parent.name() or "").lower()
        if pname == name:
            root, root_name = parent, name
        proc = parent
    try:
        return int(root.pid), root_name
    except Exception:
        return 0, ""


def _lookup_candidates(dom):
    """Hosts to resolve for ONE rule domain.

    The address bar does not use the rule's exact host: typing
    youtube.com lands the browser on www.youtube.com, and the two hosts
    serve DISJOINT address pools (measured for youtube.com,
    facebook.com and tiktok.com).  Resolving only the rule's own host
    therefore let the very connection that visits the blocked site go
    unseen - no match, no event, the browser stayed open and blocking
    looked broken.  The rule and its www/apex counterpart are both
    resolved, and every address learned is attributed to the RULE
    domain, so the verdict is still decide()'s on the rule the
    administrator wrote (suffix semantics unchanged)."""
    dom = normalize(dom)
    if not dom:
        return []
    out = [dom]
    variant = dom[4:] if dom.startswith("www.") else "www." + dom
    if variant and "." in variant and variant not in out:
        out.append(variant)
    return out


# ==========================================================================
# The detector
# ==========================================================================
class WebAccessDetector:
    """Turns the policy snapshot into detected-access events.

    The class owns three pieces of state:
      * the policy snapshot (version + mode + rule lists)
      * a domain -> {IP} cache, refreshed in the background
      * the episode set that stops one open connection from producing the
        same event over and over
    """

    def __init__(self, resolver=None, connections=None,
                 process_factory=None):
        self._lock = threading.RLock()
        self._resolver = resolver or resolve_domain
        self._connections_fn = connections
        self._proc_factory = process_factory or psutil.Process
        self._policy = {"version": 0, "mode": "allow_all",
                        "blocked": [], "allowed": [],
                        "upstream_dns": ""}
        self._ip_map = {}                 # ip -> {domain, ...}
        self._watch = []                  # domains we have to resolve
        self._reverse_cache = {}          # ip -> reverse name (bounded)
        self._dirty = True
        self._next_refresh = 0.0
        self._episodes = {}               # (pid, ip, domain) -> sighting
        self._cooldown = {}               # (domain, browser) -> last close
        self.kills = []                   # audit trail for this process
        self._running = False
        self._thread = None
        self.last_error = ""
        self.last_refresh = 0.0

    # ------------------------------------------------------------ policy
    def set_policy(self, policy):
        """Install a snapshot (same shape client.normalize_web_policy
        produces) and schedule a re-resolve.  Never raises."""
        pol = policy if isinstance(policy, dict) else {}
        mode = pol.get("mode") or "allow_all"
        if mode not in MODES:
            mode = "allow_all"
        try:
            version = int(pol.get("version") or 0)
        except (TypeError, ValueError):
            version = 0
        with self._lock:
            self._policy = {
                "version": version,
                "mode": mode,
                "blocked": [normalize(d) for d in pol.get("blocked") or []
                            if normalize(d)],
                "allowed": [normalize(d) for d in pol.get("allowed") or []
                            if normalize(d)],
                "upstream_dns": str(pol.get("upstream_dns") or ""),
            }
            self._watch = self._watched_domains()
            self._dirty = True
            self._next_refresh = 0.0
            if mode == "allow_all":
                # no scanning, no resolving, no events (spec)
                self._ip_map = {}
                self._episodes = {}
        self.ensure_thread()

    def get_policy(self):
        with self._lock:
            return dict(self._policy)

    def _watched_domains(self):
        """Which domains have to be resolved for the current mode.

        The snapshot ALWAYS carries both rule lists (server.get_web_policy
        returns them whatever the mode is), and both are watched here:

          block_list  primary = blocked,  context = allowed
          allow_only  primary = allowed,  context = blocked

        The context list is what makes an ambiguous shared address
        detectable.  A CDN that serves a blocked domain and an allowed
        one from the SAME address must never turn into a kill signal -
        we cannot tell which of the two the browser actually asked for.
        allow_all watches nothing at all (no work, no events)."""
        pol = self._policy
        mode = pol["mode"]
        if mode == "allow_all":
            return []
        if mode == "block_list":
            primary, other = pol["blocked"], pol["allowed"]
        else:
            primary, other = pol["allowed"], pol["blocked"]
        if not primary:
            return []                      # nothing is being enforced
        out, seen = [], set()
        for dom in list(primary) + list(other):
            if dom and dom not in seen:
                seen.add(dom)
                out.append(dom)
        return out

    def violations(self, host):
        """(is_violation, reason) for a domain under the current policy.

        The verdict itself comes from dns_filter.decide() - the single
        implementation of the policy - so a detection event and the
        rules the server versioned can never disagree.  Its suffix
        semantics are unchanged: example.com matches www.example.com
        and never notexample.com."""
        pol = self._policy
        host = normalize(host)
        if not host:
            return False, ""
        if decide(host, pol):
            return False, ""
        return True, ("not_allowed" if pol["mode"] == "allow_only"
                      else "blocked")

    # --------------------------------------------------------- resolution
    def refresh(self, force=True):
        """Re-resolve every watched rule domain AND its entry-point
        variant (the _lookup_candidates www/apex pair).  Returns how
        many addresses were learned.  Never raises."""
        with self._lock:
            watch = list(self._watch)
            upstream = self._policy.get("upstream_dns") or None
            mode = self._policy["mode"]
        if not watch or mode == "allow_all":
            with self._lock:
                self._ip_map = {}
                self._dirty = False
                self.last_refresh = time.time()
            return 0
        ip_map, learned = {}, 0
        for dom in watch:
            for host in _lookup_candidates(dom):
                addrs = self._resolver(host, upstream) \
                    if _takes_upstream(self._resolver) \
                    else self._resolver(host)
                for ip in {_clean_ip(a) for a in (addrs or set())}:
                    if not ip:
                        continue
                    # attributed to the RULE domain: whatever host the
                    # browser really opened, the verdict + reported
                    # domain are the rule the administrator wrote
                    ip_map.setdefault(ip, set()).add(dom)
                    learned += 1
        with self._lock:
            self._ip_map = ip_map
            self._dirty = False
            self.last_refresh = time.time()
            self._next_refresh = time.time() + REFRESH_INTERVAL
            self.last_error = "" if ip_map else \
                "no policy domain resolved - detection has nothing to match"
        return learned

    def ensure_thread(self):
        """Start the background refresh loop (idempotent)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._running = True
            self._thread = threading.Thread(target=self._loop,
                                             name="web-access",
                                             daemon=True)
            self._thread.start()

    def _loop(self):
        while True:
            with self._lock:
                if not self._running:
                    return
                due = self._dirty or time.time() >= self._next_refresh
                mode = self._policy["mode"]
            if due and mode != "allow_all":
                try:
                    self.refresh()
                except Exception as e:          # a dead detector never
                    with self._lock:            # disturbs the kiosk
                        self.last_error = str(e)
            time.sleep(1.0)

    def stop(self):
        with self._lock:
            self._running = False

    # ----------------------------------------------------------- scanning
    def _connections(self):
        """[(ip, pid, status)] for every socket on the machine.

        This is the ONLY psutil call in the scan path; tests override it
        so they never depend on psutil's platform-specific structures."""
        if self._connections_fn is not None:
            return list(self._connections_fn())
        out = []
        for c in psutil.net_connections(kind="inet"):
            if not c.raddr:
                continue
            out.append((_clean_ip(c.raddr.ip), int(c.pid or 0),
                        str(c.status or "")))
        return out

    def poll(self, now=None):
        """One scan.  Returns a list of events, never raises.

        Each event:
          domain, ip, pid, process, browser, browser_pid, status, reason,
          policy_version

        status is DETECTED (domain + responsible process both known) or
        UNRESOLVED (something we deliberately do not act on)."""
        now = time.time() if now is None else now
        with self._lock:
            mode = self._policy["mode"]
            if mode == "allow_all":
                return []                     # zero events, no scanning
            if mode == "allow_only" and not self._policy["allowed"]:
                # guardrail: an empty allow-list is a misconfiguration,
                # never a reason to flag every connection on the machine
                self.last_error = ("ALLOW ONLY has no allowed domains - "
                                   "nothing is being detected")
                return []
            ip_map = {k: set(v) for k, v in self._ip_map.items()}
        if not ip_map:
            return []                         # nothing resolvable yet
        try:
            conns = self._connections()
        except Exception as e:
            self.last_error = str(e)
            return []
        events, seen = [], set()
        for ip, pid, status in conns:
            ip = _clean_ip(ip)
            if not ip or not is_public_ip(ip):
                continue
            if status and status not in LIVE_STATES:
                continue
            if not pid or pid == os.getpid():
                continue                      # never our own lookups
            domains = ip_map.get(ip)
            if domains:
                verdicts = {self.violations(d)[0] for d in domains}
                if len(verdicts) > 1:
                    # one address serving policy domains with mixed
                    # verdicts (shared CDN): never guess which one
                    ev = self._event(sorted(domains)[0], ip, pid, status,
                                     "UNRESOLVED", "ambiguous-domain")
                    if self._episode(ev, now, seen):
                        events.append(ev)
                    continue
                domain = sorted(domains)[0]
                bad, reason = self.violations(domain)
                if not bad:
                    continue                  # no event for allowed traffic
                ev = self._event(domain, ip, pid, status, "DETECTED", reason)
                if self._episode(ev, now, seen):
                    events.append(ev)
                continue
            if mode != "allow_only":
                continue                      # block_list: only its own
            # ALLOW ONLY: this address is not in the allowed set, so it
            # has to be named before anyone may act on it
            name = self._reverse(ip)
            if not name:
                ev = self._event("", ip, pid, status, "UNRESOLVED",
                                 "unnameable")
                if self._episode(ev, now, seen):
                    events.append(ev)
                continue
            bad, reason = self.violations(name)
            if not bad:
                continue                      # reverse name is allowed
            ev = self._event(name, ip, pid, status, "DETECTED",
                             reason or "not_allowed")
            if self._episode(ev, now, seen):
                events.append(ev)
        self._prune(now)
        return events

    def _reverse(self, ip):
        """Reverse name for an address we cannot map, cached briefly."""
        with self._lock:
            cached = self._reverse_cache.get(ip)
        if cached is not None:
            return cached
        name = ""
        try:
            name = normalize(socket.gethostbyaddr(ip)[0])
        except Exception:
            name = ""
        with self._lock:
            self._reverse_cache[ip] = name
        return name

    def _event(self, domain, ip, pid, status, verdict, reason):
        proc = process_name(pid)
        root, root_name = browser_root(pid)
        return {
            "domain": domain,
            "ip": ip,
            "pid": int(pid),
            "process": proc,
            "browser": root_name,
            "browser_pid": int(root or 0),
            "browser_created": _create_time(root) if root else 0.0,
            "status": verdict,
            "reason": reason,
            "policy_version": self.get_policy()["version"],
        }

    def _episode(self, event, now, seen):
        """Emit at most once per continuous connection episode.

        The key is refreshed on every sighting and forgotten a few
        seconds after the connection disappears, so an open page produces
        one event while it stays open and a fresh visit produces a new one."""
        key = (event["pid"], event["ip"], event["domain"])
        if key in seen:
            return False
        with self._lock:
            prev = self._episodes.get(key)
            self._episodes[key] = now
        if prev is not None and now - prev < EPISODE_TTL:
            return False                       # same episode, already sent
        seen.add(key)
        return True

    def _prune(self, now):
        with self._lock:
            dead = [k for k, ts in self._episodes.items()
                    if now - ts > EPISODE_TTL]
            for k in dead:
                self._episodes.pop(k, None)
            # the reverse cache is best effort and bounded on purpose:
            # a long-running kiosk must not grow it without limit
            if len(self._reverse_cache) > 512:
                self._reverse_cache.clear()

    # ------------------------------------------------------------ status
    def status(self):
        """(state, detail) for the server: OK / FAILED with a reason.

        Only HARD failures count here, because only they say the
        mechanism itself cannot work:

          * ALLOW ALL enforces nothing by definition           -> OK
          * ALLOW ONLY with an empty list deliberately flags
            nothing (the misconfiguration guardrail)          -> FAILED
          * everything else needs this process able to list foreign
            sockets AND name their owner; without both no
            connection can ever be attributed, so nothing is
            enforced                                        -> FAILED

        A resolution gap is NOT one of those: refresh() re-resolves
        every REFRESH_INTERVAL and poll() simply produces nothing
        until then, so failing the ack over a transient lookup would
        tell the administrator that a working policy is broken.  It
        stays visible in last_error for diagnostics.  Never raises."""
        with self._lock:
            mode = self._policy["mode"]
            allowed = list(self._policy.get("allowed") or [])
        if mode == "allow_all":
            return "OK", ""
        if mode == "allow_only" and not allowed:
            return "FAILED", ("ALLOW ONLY has no allowed domains - "
                              "nothing can be enforced")
        try:
            ok, why = probe_access()
        except Exception as e:
            return "FAILED", str(e) or "Website Access detection is unavailable"
        return ("OK", "") if ok else ("FAILED", why)

    # -------------------------------------------------- guarded close (P1.2)
    def can_close(self, event):
        """(allowed, reason) - the five conditions, checked in order.

          1. the mode is actually enforcing (ALLOW ALL closes nothing)
          2. the event is DETECTED - UNRESOLVED is never acted on
          3. a domain is attributed AND unambiguous
          4. the responsible process is a supported browser
             (chrome/edge/firefox/brave/opera/vivaldi)
          5. its PID was positively identified and is not cooling down

        Nothing is terminated here: this is the gate the caller reports
        on, so a refusal is always explainable instead of silent."""
        ev = event if isinstance(event, dict) else {}
        mode = self.get_policy()["mode"]
        if mode == "allow_all":
            return False, "ALLOW ALL is active - nothing is enforced"
        if ev.get("status") != "DETECTED":
            return False, f"event is {ev.get('status') or 'unknown'}, not DETECTED"
        if not ev.get("domain"):
            return False, "no domain attribution"
        if ev.get("reason") == "ambiguous-domain":
            return False, "domain attribution is ambiguous"
        browser = str(ev.get("browser") or "").lower()
        if browser not in BROWSERS:
            return False, (f"responsible process "
                           f"({ev.get('process') or 'unknown'}) is not "
                           f"chrome/edge/firefox/brave/opera/vivaldi")
        if int(ev.get("browser_pid") or 0) <= 0:
            return False, "no positively identified browser process"
        key = (ev.get("domain"), browser)
        with self._lock:
            last = self._cooldown.get(key)
        left = KILL_COOLDOWN - (time.time() - (last or 0))
        if last and left > 0:
            return False, f"cooldown active ({int(left)}s left)"
        return True, ""

    def close_browser(self, event, pid=None):
        """Close the ONE browser process responsible for this event.

        Returns (ok, detail).  Exactly one PID is re-validated and then
        terminated - never an image name (which would take out every
        window of that browser in every session) and never a process
        tree.  Never raises: a refused or failed close is reported, not
        swallowed, so the server never sees SYNCED over a failed action.
        """
        allowed, why = self.can_close(event)
        if not allowed:
            return False, why
        ev = event if isinstance(event, dict) else {}
        target = int(pid or ev.get("browser_pid") or 0)
        if target <= 0:
            return False, "no PID to close"
        if target == os.getpid():
            return False, "refusing to terminate the Client itself"
        try:
            proc = self._proc_factory(target)
            name = str(proc.name() or "").lower()
        except psutil.NoSuchProcess:
            return False, "the browser process is already gone"
        except psutil.AccessDenied:
            return False, ("cannot access the browser process - run the "
                           "Client as Administrator")
        except Exception as e:
            return False, str(e) or "cannot inspect the browser process"
        if name not in BROWSERS:
            return False, (f"pid {target} is {name or 'unknown'}, not a "
                           f"browser we may touch")
        # the PID may have been recycled into a different process since
        # the event was recorded - never terminate something else
        want = float(ev.get("browser_created") or 0.0)
        if want:
            try:
                have = float(proc.create_time())
            except Exception:
                have = 0.0
            if have and abs(have - want) > 0.5:
                return False, "that PID is no longer the browser detected"
        try:
            proc.terminate()
        except psutil.AccessDenied:
            return False, ("the browser refused to close - run the Client "
                           "as Administrator")
        except Exception as e:
            return False, str(e) or "terminate failed"
        if not self._wait_gone(proc, 5.0):
            try:
                proc.kill()                       # same single PID
            except Exception:
                pass
            if not self._wait_gone(proc, 2.0):
                self._record(ev, target, name, False, "still running")
                return False, "the browser did not exit"
        self._record(ev, target, name, True, "")
        with self._lock:
            self._cooldown[(ev.get("domain"), name)] = time.time()
        return True, f"closed {name} (pid {target})"

    @staticmethod
    def _wait_gone(proc, timeout):
        """True once the process is really gone (or it vanished itself)."""
        deadline = time.time() + max(0.0, float(timeout))
        while True:
            try:
                if not proc.is_running():
                    return True
            except Exception:
                return True                       # NoSuchProcess == gone
            if time.time() >= deadline:
                return False
            time.sleep(0.1)

    def _record(self, event, pid, name, ok, detail):
        """Audit this attempt in-process; P1.3 ships it to the server."""
        try:
            entry = {"at": time.time(), "pid": int(pid), "browser": name,
                     "domain": event.get("domain") or "",
                     "ip": event.get("ip") or "",
                     "policy_version": event.get("policy_version") or 0,
                     "ok": bool(ok), "detail": detail}
        except Exception:
            return
        with self._lock:
            self.kills.append(entry)
            if len(self.kills) > 256:             # bounded, like the rest
                del self.kills[:len(self.kills) - 256]


def _takes_upstream(fn):
    """True when the injected resolver accepts the upstream argument."""
    try:
        import inspect
        return len(inspect.signature(fn).parameters) >= 2
    except Exception:
        return False
