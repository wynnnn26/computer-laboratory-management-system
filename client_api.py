"""
client_api.py
Data-access bridge used by client-side UI code.

If a live server link is available (Client Mode), every call is forwarded
to the central Server over the TLS socket using purpose-built parameterized
queries.  Otherwise the calls fall back to the original direct-SQLite path
so the standalone/server-PC behaviour of the existing app is unchanged.
"""

import threading

_LINK = None          # object exposing .request(kind, payload, timeout) -> dict
_LOCK = threading.Lock()


def set_link(link):
    """Register the live ClientNetwork (or None to clear)."""
    global _LINK
    with _LOCK:
        _LINK = link


def get_link():
    with _LOCK:
        return _LINK


def _call(kind, payload=None, fallback=None, timeout=8.0):
    """Forward to the server when linked; else run the local fallback."""
    link = get_link()
    if link is not None:
        try:
            resp = link.request(kind, payload or {}, timeout=timeout)
            return resp
        except Exception as e:
            return {"ok": False, "error": str(e), "data": [] if fallback else None}
    if fallback is not None:
        return fallback()
    return {"ok": False, "error": "No server link", "data": []}


# ----------------------------------------------------------------- fallback
def _local_announcements():
    from database import get_connection
    conn = get_connection()
    rows = conn.execute("SELECT * FROM announcements ORDER BY id DESC").fetchall()
    conn.close()
    return {"ok": True, "data": [dict(r) for r in rows]}


def _local_messages(sid):
    from database import get_connection
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM messages WHERE sender=? OR receiver=? ORDER BY id DESC", (sid, sid)
    ).fetchall()
    conn.close()
    return {"ok": True, "data": [dict(r) for r in rows]}


def _local_borrow(sid):
    from database import get_connection
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM borrow_records WHERE student_id=? ORDER BY id DESC", (sid,)
    ).fetchall()
    conn.close()
    return {"ok": True, "data": [dict(r) for r in rows]}


def _local_attendance(sid):
    from database import get_connection
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM attendance WHERE student_id=? ORDER BY id DESC", (sid,)
    ).fetchall()
    conn.close()
    return {"ok": True, "data": [dict(r) for r in rows]}


def _local_pcs():
    from database import get_connection
    conn = get_connection()
    rows = conn.execute(
        "SELECT pc_name FROM computers WHERE status='Available' ORDER BY pc_name"
    ).fetchall()
    conn.close()
    return {"ok": True, "data": [r["pc_name"] for r in rows]}


def _local_send_message(sid, text):
    from database import get_connection
    from utils import now_datetime
    conn = get_connection()
    conn.execute(
        "INSERT INTO messages (sender, receiver, message, timestamp, is_read) VALUES (?,?,?,?, 'No')",
        (sid, "admin", text, now_datetime()),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "data": []}


def _local_borrow_submit(sid, item, qty):
    from database import get_connection
    from utils import now_date
    conn = get_connection()
    conn.execute(
        "INSERT INTO borrow_records (student_id, item_name, quantity, borrow_date, return_date, status) "
        "VALUES (?,?,?,?,?, 'Pending Approval')",
        (sid, item, qty, now_date(), ""),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "data": []}


def _local_time_in(sid, name, pc):
    from database import get_connection
    from utils import now_date, now_time
    conn = get_connection()
    row = conn.execute(
        "SELECT id FROM attendance WHERE student_id=? AND date=? AND status='In Lab'",
        (sid, now_date()),
    ).fetchone()
    if row:
        conn.close()
        return {"ok": False, "error": "You already have an active lab session today."}
    conn.execute(
        "INSERT INTO attendance (student_id, full_name, pc_name, date, time_in, status) "
        "VALUES (?,?,?,?,?, 'In Lab')",
        (sid, name, pc, now_date(), now_time()),
    )
    conn.execute(
        "UPDATE computers SET status='In Use', assigned_to=? WHERE pc_name=?", (sid, pc)
    )
    conn.commit()
    conn.close()
    return {"ok": True, "data": []}


def _local_time_out(sid):
    from database import get_connection
    from utils import now_date, now_time
    conn = get_connection()
    row = conn.execute(
        "SELECT id, pc_name FROM attendance WHERE student_id=? AND date=? AND status='In Lab' "
        "ORDER BY id DESC LIMIT 1",
        (sid, now_date()),
    ).fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "No active session to time out from."}
    conn.execute(
        "UPDATE attendance SET time_out=?, status='Completed' WHERE id=?", (now_time(), row["id"])
    )
    conn.execute(
        "UPDATE computers SET status='Available', assigned_to='' WHERE pc_name=?", (row["pc_name"],)
    )
    conn.commit()
    conn.close()
    return {"ok": True, "data": []}


# --------------------------------------------------------------- public API
def fetch_announcements():
    return _call("announcements", {}, _local_announcements)


def fetch_messages(student_id):
    return _call("messages_get", {"student_id": student_id},
                 lambda: _local_messages(student_id))


def send_message(student_id, text):
    return _call("messages_send", {"student_id": student_id, "message": text},
                 lambda: _local_send_message(student_id, text))


def fetch_borrow(student_id):
    return _call("borrow_list", {"student_id": student_id},
                 lambda: _local_borrow(student_id))


def submit_borrow(student_id, item_name, quantity="1"):
    return _call("borrow_submit", {"student_id": student_id,
                                   "item_name": item_name, "quantity": quantity},
                 lambda: _local_borrow_submit(student_id, item_name, quantity))


def fetch_attendance(student_id):
    return _call("attendance_list", {"student_id": student_id},
                 lambda: _local_attendance(student_id))


def fetch_available_pcs():
    return _call("pcs_available", {}, _local_pcs)


def time_in(student_id, full_name, pc_name):
    return _call("attendance_time_in",
                 {"student_id": student_id, "full_name": full_name, "pc_name": pc_name},
                 lambda: _local_time_in(student_id, full_name, pc_name))


def time_out(student_id):
    return _call("attendance_time_out", {"student_id": student_id},
                 lambda: _local_time_out(student_id))
