import os
import hashlib
import secrets
import smtplib
import sqlite3
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

DB = Path("data/hospital.db")
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "shikhar123")


# ── Password hashing (pbkdf2, no extra deps) ──
def hash_pw(pw: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()
    return f"{salt}${h}"


def verify_pw(pw: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$")
        return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex() == h
    except Exception:
        return False


# ── Email (Gmail SMTP; no-op if not configured) ──
def send_email(to_email: str, subject: str, html: str) -> bool:
    smtp_email = os.getenv("SMTP_EMAIL", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    if not smtp_email or not smtp_password or not to_email:
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"ShiKhar Hospital <{smtp_email}>"
    msg["To"] = to_email
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(smtp_email, smtp_password)
            s.sendmail(smtp_email, to_email, msg.as_string())
        return True
    except Exception:
        return False


def appointment_email_html(a) -> str:
    return f"""
    <div style="font-family:Segoe UI,sans-serif;max-width:500px;margin:auto;border:1px solid #e2e8f0;border-radius:14px;padding:26px">
      <div style="text-align:center;margin-bottom:16px">
        <div style="display:inline-grid;place-items:center;width:46px;height:46px;border-radius:12px;background:#0d8578;color:#fff;font-size:22px">✚</div>
        <h2 style="color:#106a61;margin:8px 0 0">ShiKhar Hospital</h2>
        <p style="color:#64748b;font-size:13px;margin:2px 0">Appointment Confirmation</p>
      </div>
      <p>Hi <b>{a['patientName']}</b>, your appointment is <b style="color:#0d8578">confirmed</b>.</p>
      <div style="background:#f0fdfa;border-radius:12px;padding:16px;margin:14px 0">
        <div style="font-size:20px;font-weight:800;color:#106a61;letter-spacing:1px">{a['id']}</div>
        <div style="margin-top:8px;font-size:14px">👨‍⚕️ <b>{a['doctorName']}</b> ({a.get('specialty','')})</div>
        <div style="font-size:14px">📅 {a['date']} &nbsp; ⏰ {a['slot']}</div>
        <div style="font-size:14px">💳 {a['payMethod']} — {'Paid ₹' + str(a['fee']) if a.get('paid') else 'Pay ₹' + str(a['fee']) + ' at hospital'}</div>
      </div>
      <p style="font-size:12.5px;color:#64748b">Please arrive 10 minutes early. To reschedule or cancel, visit your patient dashboard.</p>
      <p style="text-align:center;color:#94a3b8;font-size:11px;margin-top:18px">Get well soon — ShiKhar Hospital</p>
    </div>
    """


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
        CREATE TABLE IF NOT EXISTS patients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, email TEXT UNIQUE, phone TEXT,
            password_hash TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY, email TEXT, is_admin INTEGER DEFAULT 0, created_at TEXT
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


@app.get("/api/booked-slots")
def booked_slots(doctorId: int, date: str):
    """Slots already booked for a doctor on a date (so the UI can disable them)."""
    c = con()
    rows = c.execute(
        "SELECT slot FROM appointments WHERE doctor_id=? AND date=? AND status != 'Cancelled'",
        (doctorId, date),
    ).fetchall()
    c.close()
    return [r["slot"] for r in rows]


@app.post("/api/appointments")
def create_appt(a: ApptIn, background: BackgroundTasks):
    c = con()
    # Prevent double-booking the same doctor + date + slot
    clash = c.execute(
        "SELECT 1 FROM appointments WHERE doctor_id=? AND date=? AND slot=? AND status != 'Cancelled'",
        (a.doctorId, a.date, a.slot),
    ).fetchone()
    if clash:
        c.close()
        raise HTTPException(409, "This slot was just booked. Please pick another time.")
    aid = _appt_id()
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
    appt = _row_to_appt(row)
    # send confirmation email (background — never blocks/breaks booking)
    if a.patientEmail:
        background.add_task(
            send_email, a.patientEmail,
            f"Appointment Confirmed — {aid}",
            appointment_email_html({
                "id": aid, "patientName": a.patientName, "doctorName": a.doctorName,
                "specialty": a.specialty, "date": a.date, "slot": a.slot,
                "payMethod": a.payMethod, "fee": a.fee, "paid": a.paid,
            }),
        )
    return appt


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


# ── Auth (patients + admin) ───────────────────────────
class RegisterIn(BaseModel):
    name: str
    email: str
    phone: str = ""
    password: str


class LoginIn(BaseModel):
    email: str
    password: str


class AdminLoginIn(BaseModel):
    username: str
    password: str


def _issue_token(email: str, is_admin: int = 0) -> str:
    tok = secrets.token_hex(24)
    c = con()
    c.execute("INSERT INTO sessions (token, email, is_admin, created_at) VALUES (?,?,?,?)",
              (tok, email, is_admin, datetime.now().isoformat()))
    c.commit()
    c.close()
    return tok


@app.post("/api/auth/register")
def register(r: RegisterIn):
    if len(r.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    c = con()
    exists = c.execute("SELECT 1 FROM patients WHERE email=?", (r.email.lower().strip(),)).fetchone()
    if exists:
        c.close()
        raise HTTPException(400, "An account with this email already exists")
    c.execute(
        "INSERT INTO patients (name, email, phone, password_hash, created_at) VALUES (?,?,?,?,?)",
        (r.name, r.email.lower().strip(), r.phone, hash_pw(r.password), datetime.now().isoformat()),
    )
    c.commit()
    c.close()
    return {"token": _issue_token(r.email.lower().strip()), "name": r.name, "email": r.email.lower().strip()}


@app.post("/api/auth/login")
def login(l: LoginIn):
    c = con()
    row = c.execute("SELECT * FROM patients WHERE email=?", (l.email.lower().strip(),)).fetchone()
    c.close()
    if not row or not verify_pw(l.password, row["password_hash"]):
        raise HTTPException(401, "Invalid email or password")
    return {"token": _issue_token(row["email"]), "name": row["name"], "email": row["email"]}


@app.post("/api/auth/admin-login")
def admin_login(a: AdminLoginIn):
    if a.username != ADMIN_USER or a.password != ADMIN_PASS:
        raise HTTPException(401, "Invalid admin credentials")
    return {"token": _issue_token("__admin__", 1), "name": "Administrator"}


@app.get("/api/auth/me")
def me(token: str):
    c = con()
    row = c.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
    c.close()
    if not row:
        raise HTTPException(401, "Invalid token")
    return {"email": row["email"], "isAdmin": bool(row["is_admin"])}
