import sqlite3
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

DB = Path("data/hospital.db")


def con():
    DB.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = con()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS appointments (
            id           TEXT PRIMARY KEY,
            doctor_id    INTEGER,
            doctor_name  TEXT,
            specialty    TEXT,
            hospital     TEXT,
            date         TEXT,
            slot         TEXT,
            fee          INTEGER,
            pay_method   TEXT,
            status       TEXT DEFAULT 'Confirmed',
            paid         INTEGER DEFAULT 0,
            patient_name TEXT,
            patient_phone TEXT,
            patient_email TEXT,
            reason       TEXT DEFAULT '',
            created_at   TEXT
        );
        CREATE TABLE IF NOT EXISTS extra_doctors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, specialty_label TEXT, fee INTEGER, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS blocked_dates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT, doctor_id INTEGER DEFAULT 0, reason TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT, message TEXT, created_at TEXT
        );
        """
    )
    c.commit()
    c.close()


app = FastAPI(title="ShiKhar Hospital API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup():
    init_db()


@app.get("/health")
def health():
    return {"status": "ok"}


# ── Appointments ──────────────────────────────────────
class ApptIn(BaseModel):
    doctorId: int
    doctorName: str
    specialty: str = ""
    hospital: str = ""
    date: str
    slot: str
    fee: int
    payMethod: str
    paid: bool = False
    status: str = "Confirmed"
    patientName: str
    patientPhone: str = ""
    patientEmail: str = ""
    reason: str = ""


def _appt_id():
    d = datetime.now()
    stamp = f"{d.year}{d.month:02d}"
    rand = f"{int(d.timestamp()) % 10000:04d}"
    return f"SHK-{stamp}-{rand}"


def _row_to_appt(r):
    return {
        "id": r["id"],
        "doctorId": r["doctor_id"],
        "doctorName": r["doctor_name"],
        "specialty": r["specialty"],
        "hospital": r["hospital"],
        "date": r["date"],
        "slot": r["slot"],
        "fee": r["fee"],
        "payMethod": r["pay_method"],
        "status": r["status"],
        "paid": bool(r["paid"]),
        "patient": {"name": r["patient_name"], "phone": r["patient_phone"], "email": r["patient_email"]},
        "reason": r["reason"],
        "createdAt": r["created_at"],
    }


@app.post("/api/appointments")
def create_appt(a: ApptIn):
    aid = _appt_id()
    c = con()
    c.execute(
        """INSERT INTO appointments
           (id,doctor_id,doctor_name,specialty,hospital,date,slot,fee,pay_method,status,paid,
            patient_name,patient_phone,patient_email,reason,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (aid, a.doctorId, a.doctorName, a.specialty, a.hospital, a.date, a.slot, a.fee,
         a.payMethod, a.status, 1 if a.paid else 0, a.patientName, a.patientPhone,
         a.patientEmail, a.reason, datetime.now().isoformat()),
    )
    c.commit()
    row = c.execute("SELECT * FROM appointments WHERE id=?", (aid,)).fetchone()
    c.close()
    return _row_to_appt(row)


@app.get("/api/appointments")
def list_appts(phone: str = "", email: str = ""):
    c = con()
    if phone or email:
        rows = c.execute(
            "SELECT * FROM appointments WHERE patient_phone=? OR patient_email=? ORDER BY created_at DESC",
            (phone, email),
        ).fetchall()
    else:
        rows = c.execute("SELECT * FROM appointments ORDER BY created_at DESC").fetchall()
    c.close()
    return [_row_to_appt(r) for r in rows]


class ApptPatch(BaseModel):
    status: str | None = None
    paid: bool | None = None


@app.patch("/api/appointments/{aid}")
def patch_appt(aid: str, p: ApptPatch):
    c = con()
    row = c.execute("SELECT * FROM appointments WHERE id=?", (aid,)).fetchone()
    if not row:
        c.close()
        raise HTTPException(404, "Not found")
    if p.status is not None:
        c.execute("UPDATE appointments SET status=? WHERE id=?", (p.status, aid))
    if p.paid is not None:
        c.execute("UPDATE appointments SET paid=? WHERE id=?", (1 if p.paid else 0, aid))
    c.commit()
    row = c.execute("SELECT * FROM appointments WHERE id=?", (aid,)).fetchone()
    c.close()
    return _row_to_appt(row)


# ── Admin-added doctors (persisted) ───────────────────
class DoctorIn(BaseModel):
    name: str
    specialtyLabel: str = ""
    fee: int = 500


@app.get("/api/extra-doctors")
def list_extra_doctors():
    c = con()
    rows = c.execute("SELECT * FROM extra_doctors ORDER BY created_at DESC").fetchall()
    c.close()
    return [{"id": r["id"], "name": r["name"], "specialtyLabel": r["specialty_label"], "fee": r["fee"]} for r in rows]


@app.post("/api/extra-doctors")
def add_extra_doctor(d: DoctorIn):
    c = con()
    cur = c.execute(
        "INSERT INTO extra_doctors (name, specialty_label, fee, created_at) VALUES (?,?,?,?)",
        (d.name, d.specialtyLabel, d.fee, datetime.now().isoformat()),
    )
    c.commit()
    did = cur.lastrowid
    c.close()
    return {"id": did, "name": d.name, "specialtyLabel": d.specialtyLabel, "fee": d.fee}


@app.delete("/api/extra-doctors/{did}")
def del_extra_doctor(did: int):
    c = con()
    c.execute("DELETE FROM extra_doctors WHERE id=?", (did,))
    c.commit()
    c.close()
    return {"message": "deleted"}


# ── Blocked dates ─────────────────────────────────────
class BlockIn(BaseModel):
    date: str
    doctorId: int = 0
    reason: str = "Not Available"


@app.get("/api/blocked-dates")
def list_blocked(doctorId: int = 0):
    c = con()
    rows = c.execute("SELECT * FROM blocked_dates ORDER BY date").fetchall()
    c.close()
    out = [{"id": r["id"], "date": r["date"], "doctorId": r["doctor_id"], "reason": r["reason"]} for r in rows]
    if doctorId:
        out = [b for b in out if b["doctorId"] in (0, doctorId)]
    return out


@app.post("/api/blocked-dates")
def add_blocked(b: BlockIn):
    c = con()
    cur = c.execute(
        "INSERT INTO blocked_dates (date, doctor_id, reason, created_at) VALUES (?,?,?,?)",
        (b.date, b.doctorId, b.reason, datetime.now().isoformat()),
    )
    c.commit()
    bid = cur.lastrowid
    c.close()
    return {"id": bid, "date": b.date, "doctorId": b.doctorId, "reason": b.reason}


@app.delete("/api/blocked-dates/{bid}")
def del_blocked(bid: int):
    c = con()
    c.execute("DELETE FROM blocked_dates WHERE id=?", (bid,))
    c.commit()
    c.close()
    return {"message": "deleted"}


# ── Notifications ─────────────────────────────────────
class NotifIn(BaseModel):
    title: str
    message: str = ""


@app.get("/api/notifications")
def list_notifs():
    c = con()
    rows = c.execute("SELECT * FROM notifications ORDER BY created_at DESC").fetchall()
    c.close()
    return [{"id": r["id"], "title": r["title"], "message": r["message"], "createdAt": r["created_at"]} for r in rows]


@app.post("/api/notifications")
def add_notif(n: NotifIn):
    c = con()
    cur = c.execute(
        "INSERT INTO notifications (title, message, created_at) VALUES (?,?,?)",
        (n.title, n.message, datetime.now().isoformat()),
    )
    c.commit()
    nid = cur.lastrowid
    c.close()
    return {"id": nid, "title": n.title, "message": n.message}


@app.delete("/api/notifications/{nid}")
def del_notif(nid: int):
    c = con()
    c.execute("DELETE FROM notifications WHERE id=?", (nid,))
    c.commit()
    c.close()
    return {"message": "deleted"}
