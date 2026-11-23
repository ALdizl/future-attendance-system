# -*- coding: utf-8 -*-
"""نظام الحضور والغياب - Flask + SQLite"""
import base64
import io
import json
import os
import re
import secrets
import socket
import sqlite3
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from functools import wraps
from math import asin, cos, radians, sin, sqrt

import pandas as pd
import qrcode
import qrcode.image.svg
from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "attendance.db")
KEY_FILE = os.path.join(BASE_DIR, "secret.key")

WEEKDAYS = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]  # 0 = الاثنين
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SIGNUP_LIMIT_PER_HOUR = 60   # الطلاب كلهم ممكن يكونوا على واي فاي واحد فمش ينفع الحد يبقى صغير
SIGNUPS = defaultdict(list)   # حماية بسيطة من التسجيل الكتير من نفس الجهاز/الشبكة
ACADEMY_NAME = "أكاديمية المستقبل"      # غيّر الاسم من هنا لو احتجت
STATUS_AR = {"present": "حاضر", "late": "متأخر", "excused": "بعذر", "absent": "غائب"}
CP_NAMES = {1: "بداية الحصة", 2: "منتصف الحصة", 3: "نهاية الحصة"}
MAX_CHECKPOINTS = 3
GROUPS = ("G1", "G2")
FMT = "%Y-%m-%d %H:%M:%S"
DEFAULTS = {"rotate_seconds": "45", "grace_seconds": "30", "radius": "150",
            "gps_required": "1", "lat": "", "lng": "", "base_url": "",
            "email_domain": "fa_hists.edu.eg"}   # نهاية بريد الطلاب المسموح بيها (فاضي = أي بريد)

app = Flask(__name__)
# على الاستضافة (وراها بروكسي https) ضيف في ملف الـ WSGI:  os.environ["BEHIND_PROXY"] = "1"
BEHIND_PROXY = os.environ.get("BEHIND_PROXY") == "1"
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SECURE=BEHIND_PROXY)
if BEHIND_PROXY:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
if os.environ.get("SECRET_KEY"):
    app.secret_key = os.environ["SECRET_KEY"]
else:
    if not os.path.exists(KEY_FILE):
        with open(KEY_FILE, "w") as f:
            f.write(secrets.token_hex(32))
    with open(KEY_FILE) as f:
        app.secret_key = f.read().strip()


# ----------------------------------------------------------------- أدوات عامة
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Africa/Cairo")      # توقيت مصر حتى لو السيرفر في بلد تانية
except Exception:
    TZ = None


def now():
    return datetime.now(TZ).replace(tzinfo=None) if TZ else datetime.now()


def now_str():
    return now().strftime(FMT)


def parse(s):
    return datetime.strptime(s, FMT) if s else None


def to_float(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def haversine(lat1, lon1, lat2, lon2):
    """المسافة بالمتر بين نقطتين"""
    p1, p2 = radians(lat1), radians(lat2)
    a = sin((p2 - p1) / 2) ** 2 + cos(p1) * cos(p2) * sin(radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * asin(sqrt(a))


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def qr_data_uri(text):
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()


# ------------------------------------------------------------- قاعدة البيانات
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def get_setting(key):
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row and row["value"] is not None else DEFAULTS[key]


def set_setting(key, value):
    get_db().execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def log(action, details="", actor=None):
    if actor is None:
        actor = f"{session.get('name', '؟')} ({session.get('role', '؟')})"
    db = get_db()
    db.execute("INSERT INTO logs (actor, action, details, created_at) VALUES (?, ?, ?, ?)",
               (actor, action, details, now_str()))
    db.commit()


def ensure_schema():
    """يضيف الجداول/الأعمدة الجديدة لقاعدة قديمة من غير ما يمسح بياناتها"""
    if not os.path.exists(DB_PATH):
        return
    con = sqlite3.connect(DB_PATH)
    if "email" not in [r[1] for r in con.execute("PRAGMA table_info(students)")]:
        con.execute("ALTER TABLE students ADD COLUMN email TEXT")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_students_email ON students(email)")
    con.execute("""CREATE TABLE IF NOT EXISTS timetable (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
        doctor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        weekday INTEGER NOT NULL, start_time TEXT NOT NULL, end_time TEXT NOT NULL, room TEXT, grp TEXT)""")
    if "grp" not in [r[1] for r in con.execute("PRAGMA table_info(students)")]:
        con.execute("ALTER TABLE students ADD COLUMN grp TEXT")
    if "grp" not in [r[1] for r in con.execute("PRAGMA table_info(timetable)")]:
        con.execute("ALTER TABLE timetable ADD COLUMN grp TEXT")
    con.commit()
    con.close()


# ------------------------------------------------------- الجهاز + الصلاحيات
@app.before_request
def ensure_device_cookie():
    g.new_device = None if "device_id" in request.cookies else uuid.uuid4().hex


@app.after_request
def set_device_cookie(resp):
    if g.get("new_device"):
        resp.set_cookie("device_id", g.new_device, max_age=60 * 60 * 24 * 730,
                        httponly=True, samesite="Lax")
    return resp


def device_id():
    return request.cookies.get("device_id") or g.get("new_device")


def require(*roles):
    def deco(f):
        @wraps(f)
        def wrapper(*a, **kw):
            if "role" not in session:
                nxt = request.full_path.rstrip("?") if request.method == "GET" else ""
                return redirect(url_for("login", next=nxt))
            if session["role"] not in roles:
                flash("مش مسموح لك تفتح الصفحة دي.", "error")
                return redirect(url_for("home"))
            return f(*a, **kw)
        return wrapper
    return deco


def safe_next(nxt):
    return nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") else None


@app.context_processor
def inject():
    logo = next((f for f in ("logo.png", "logo.jpg", "logo.jpeg", "logo.svg", "logo.webp")
                 if os.path.exists(os.path.join(BASE_DIR, "static", f))), None)
    return {"STATUS_AR": STATUS_AR, "CP_NAMES": CP_NAMES, "WEEKDAYS": WEEKDAYS,
            "ACADEMY_NAME": ACADEMY_NAME, "LOGO": logo}


@app.route("/manifest.webmanifest")
def manifest():
    data = {
        "name": "نظام الحضور والغياب - " + ACADEMY_NAME, "short_name": "الحضور",
        "lang": "ar", "dir": "rtl", "start_url": "/", "scope": "/", "display": "standalone",
        "background_color": "#ffffff", "theme_color": "#006036",
        "icons": [{"src": url_for("static", filename="icon-192.png"), "sizes": "192x192", "type": "image/png"},
                  {"src": url_for("static", filename="icon-512.png"), "sizes": "512x512", "type": "image/png"}],
    }
    return app.response_class(json.dumps(data, ensure_ascii=False), mimetype="application/manifest+json")


@app.route("/sw.js")
def service_worker():
    js = "self.addEventListener('install',e=>self.skipWaiting());self.addEventListener('fetch',()=>{});"
    r = app.response_class(js, mimetype="application/javascript")
    r.headers["Cache-Control"] = "no-cache"
    return r


@app.route("/robots.txt")
def robots():
    txt = f"User-agent: *\nAllow: /$\nAllow: /login\nAllow: /signup\nDisallow: /\nSitemap: {request.url_root}sitemap.xml\n"
    return app.response_class(txt, mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap():
    root = request.url_root
    xml = ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           f"<url><loc>{root}login</loc></url><url><loc>{root}signup</loc></url></urlset>")
    return app.response_class(xml, mimetype="application/xml")


@app.errorhandler(403)
@app.errorhandler(404)
def error_page(e):
    return render_template("error.html", code=e.code), e.code


# --------------------------------------------------------- منطق الحصص والحضور
def new_code(db):
    while True:
        c = str(secrets.randbelow(900000) + 100000)
        if not db.execute("SELECT 1 FROM sessions WHERE status='open' AND (current_code=? OR prev_code=?)",
                          (c, c)).fetchone():
            return c


def start_checkpoint(db, sid, cp):
    """يبدأ نقطة تحقق جديدة بكود جديد (ومن غير كود سابق)"""
    exp = now() + timedelta(seconds=int(get_setting("rotate_seconds")))
    db.execute("UPDATE sessions SET checkpoint=?, current_code=?, code_expires_at=?, "
               "prev_code=NULL, prev_expires_at=NULL WHERE id=?",
               (cp, new_code(db), exp.strftime(FMT), sid))
    db.commit()


def rotate_if_needed(db, sid):
    """لو الكود الحالي خلص: يبقى سابق (له فترة سماح) ويتعمل كود جديد"""
    s = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    if s["status"] == "open" and parse(s["code_expires_at"]) <= now():
        n = now()
        grace = timedelta(seconds=int(get_setting("grace_seconds")))
        exp = n + timedelta(seconds=int(get_setting("rotate_seconds")))
        db.execute("UPDATE sessions SET prev_code=?, prev_expires_at=?, current_code=?, "
                   "code_expires_at=? WHERE id=?",
                   (s["current_code"], (n + grace).strftime(FMT), new_code(db), exp.strftime(FMT), sid))
        db.commit()
        s = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    return s


def base_url():
    custom = get_setting("base_url").strip().rstrip("/")
    if custom:
        return custom
    name, _, port = request.host.partition(":")
    host = request.host
    if name in ("127.0.0.1", "localhost"):
        host = lan_ip() + (":" + port if port else "")
    return f"{request.scheme}://{host}"


def final_status(rows):
    """الحالة النهائية لطالب في حصة من صفوف الحضور بتاعته"""
    manual = [r for r in rows if r["checkpoint"] == 0]
    if manual:
        return manual[0]["status"]
    ok = [r for r in rows if r["checkpoint"] > 0 and r["status"] in ("present", "late")]
    if len(ok) >= 2:
        return "late" if any(r["status"] == "late" for r in ok) else "present"
    return "absent"


def statuses_for_sessions(db, sessions, students):
    ids = [s["id"] for s in sessions]
    rows = []
    if ids:
        rows = db.execute(f"SELECT * FROM attendance WHERE session_id IN ({','.join('?' * len(ids))})",
                          ids).fetchall()
    grouped = defaultdict(list)
    for r in rows:
        grouped[(r["student_id"], r["session_id"])].append(r)
    return {(st["id"], se["id"]): final_status(grouped.get((st["id"], se["id"]), []))
            for st in students for se in sessions}


def load_session(db, sid):
    s = db.execute(
        "SELECT s.*, c.name AS course_name, c.code AS course_code, u.full_name AS doctor_name "
        "FROM sessions s JOIN courses c ON c.id=s.course_id JOIN users u ON u.id=s.doctor_id "
        "WHERE s.id=?", (sid,)).fetchone()
    if not s:
        abort(404)
    if session["role"] == "doctor" and s["doctor_id"] != session["uid"]:
        abort(403)
    return s


# ------------------------------------------------------------------- الدخول
@app.route("/")
def home():
    role = session.get("role")
    target = {"student": "student_home", "doctor": "doctor_home", "admin": "admin_dashboard"}.get(role)
    return redirect(url_for(target)) if target else redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    nxt = request.values.get("next", "")
    if request.method == "POST":
        ident = request.form.get("identifier", "").strip()
        pw = request.form.get("password", "")
        db = get_db()
        st = db.execute("SELECT * FROM students WHERE student_no=? OR email=?",
                        (ident, ident.lower())).fetchone()
        if st and check_password_hash(st["password_hash"], pw):
            session.clear()
            session.update(role="student", uid=st["id"], name=st["name"])
            log("دخول", f"طالب {st['student_no']}")
            return redirect(safe_next(nxt) or url_for("home"))
        us = db.execute("SELECT * FROM users WHERE username=?", (ident.lower(),)).fetchone()
        if us and check_password_hash(us["password_hash"], pw):
            session.clear()
            session.update(role=us["role"], uid=us["id"], name=us["full_name"])
            log("دخول", f"مستخدم {us['username']}")
            return redirect(safe_next(nxt) or url_for("home"))
        log("محاولة دخول فاشلة", f"المُدخل: {ident}", actor="غير معروف")
        flash("الرقم أو كلمة السر غلط.", "error")
    return render_template("login.html", next=nxt)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        f = request.form
        no, name = f.get("student_no", "").strip(), " ".join(f.get("name", "").split())
        email, grade = f.get("email", "").strip().lower(), f.get("grade", "").strip()
        grp = f.get("grp", "").strip().upper()
        pw, pw2 = f.get("password", ""), f.get("password2", "")
        ip = request.remote_addr or "?"
        SIGNUPS[ip] = [t for t in SIGNUPS[ip] if t > time.time() - 3600]
        error = None
        if len(SIGNUPS[ip]) >= SIGNUP_LIMIT_PER_HOUR:
            error = "عملت حسابات كتير في وقت قصير. جرّب بعد شوية."
        elif not re.fullmatch(r"[0-9A-Za-z]{3,20}", no):
            error = "رقم الطالب لازم يكون أرقام أو حروف إنجليزي من 3 لـ 20."
        elif len(name) < 3:
            error = "اكتب اسمك كامل."
        elif not EMAIL_RE.match(email):
            error = "البريد الإلكتروني مش مكتوب صح."
        elif get_setting("email_domain").strip() and not email.endswith("@" + get_setting("email_domain").strip().lower()):
            error = "التسجيل متاح فقط ببريد المعهد، اللي بينتهي بـ @" + get_setting("email_domain").strip()
        elif grp not in GROUPS:
            error = "اختار مجموعتك (G1 أو G2)."
        elif len(pw) < 6:
            error = "كلمة السر لازم 6 حروف أو أرقام على الأقل."
        elif pw != pw2:
            error = "كلمتين السر مش زي بعض."
        if not error:
            db = get_db()
            try:
                cur = db.execute("INSERT INTO students (student_no, name, grade, email, grp, password_hash) "
                                 "VALUES (?, ?, ?, ?, ?, ?)", (no, name, grade, email, grp, generate_password_hash(pw)))
                db.commit()
            except sqlite3.IntegrityError:
                error = "رقم الطالب ده أو البريد ده متسجّل قبل كده."
            else:
                SIGNUPS[ip].append(time.time())
                session.clear()
                session.update(role="student", uid=cur.lastrowid, name=name)
                log("إنشاء حساب طالب", f"{no} | {email}")
                flash("تم إنشاء حسابك ✅ أهلًا بيك!", "ok")
                return redirect(url_for("home"))
        flash(error, "error")
    return render_template("signup.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/password", methods=["GET", "POST"])
@require("student", "doctor", "admin")
def change_password():
    if request.method == "POST":
        old, new = request.form.get("old", ""), request.form.get("new", "")
        table = "students" if session["role"] == "student" else "users"
        db = get_db()
        row = db.execute(f"SELECT * FROM {table} WHERE id=?", (session["uid"],)).fetchone()
        if not check_password_hash(row["password_hash"], old):
            flash("كلمة السر الحالية غلط.", "error")
        elif len(new) < 4:
            flash("كلمة السر الجديدة لازم تكون 4 حروف أو أرقام على الأقل.", "error")
        else:
            db.execute(f"UPDATE {table} SET password_hash=? WHERE id=?",
                       (generate_password_hash(new), session["uid"]))
            db.commit()
            log("تغيير كلمة السر")
            flash("تم تغيير كلمة السر ✅", "ok")
            return redirect(url_for("home"))
    return render_template("password.html")


# -------------------------------------------------------------------- الطالب
def timetable_for_day(db, weekday, grp=None):
    """حصص اليوم. لو الطالب ليه مجموعة: بيشوف حصص مجموعته + الحصص اللي للكل بس"""
    q = ("SELECT t.*, c.name AS course_name, u.full_name AS doctor_name FROM timetable t "
         "JOIN courses c ON c.id=t.course_id LEFT JOIN users u ON u.id=t.doctor_id WHERE t.weekday=?")
    args = [weekday]
    if grp in GROUPS:
        q += " AND (t.grp IS NULL OR t.grp='' OR t.grp=?)"
        args.append(grp)
    return db.execute(q + " ORDER BY t.start_time", args).fetchall()


@app.route("/student")
@require("student")
def student_home():
    db = get_db()
    me = db.execute("SELECT * FROM students WHERE id=?", (session["uid"],)).fetchone()
    sessions = db.execute(
        "SELECT s.*, c.name AS course_name FROM sessions s JOIN courses c ON c.id=s.course_id "
        "WHERE s.status='closed' ORDER BY s.id DESC LIMIT 30").fetchall()
    m = statuses_for_sessions(db, sessions, [me])
    history = [{"course": s["course_name"], "date": s["started_at"][:16], "status": m[(me["id"], s["id"])]}
               for s in sessions]
    counts = Counter(h["status"] for h in history)
    wd = now().weekday()
    today, tomorrow = timetable_for_day(db, wd, me["grp"]), timetable_for_day(db, (wd + 1) % 7, me["grp"])
    return render_template("student_home.html", me=me, history=history, counts=counts,
                           today=today, tomorrow=tomorrow, tomorrow_name=WEEKDAYS[(wd + 1) % 7])


@app.route("/student/group", methods=["POST"])
@require("student")
def student_group():
    grp = request.form.get("grp", "").strip().upper()
    if grp in GROUPS:
        db = get_db()
        db.execute("UPDATE students SET grp=? WHERE id=?", (grp, session["uid"]))
        db.commit()
        log("تغيير المجموعة", grp)
        flash("تم حفظ مجموعتك ✅", "ok")
    return redirect(url_for("student_home"))


@app.route("/attend")
@require("student")
def attend_form():
    code = request.args.get("code", "").strip()
    return redirect(url_for("attend_page", code=code)) if code else redirect(url_for("student_home"))


@app.route("/s/<code>")
@require("student")
def attend_page(code):
    return render_template("attend.html", code=code)


@app.route("/attend/submit", methods=["POST"])
def attend_submit():
    if session.get("role") != "student":
        return jsonify(ok=False, message="سجّل دخولك الأول."), 401
    data = request.get_json(silent=True) or {}
    code = str(data.get("code", "")).strip()
    lat, lng = to_float(data.get("lat")), to_float(data.get("lng"))
    db = get_db()
    me = db.execute("SELECT * FROM students WHERE id=?", (session["uid"],)).fetchone()

    def fail(msg, why):
        log("تسجيل حضور مرفوض", f"{why} | الطالب {me['student_no']} | كود {code}")
        return jsonify(ok=False, message=msg)

    s = db.execute("SELECT * FROM sessions WHERE status='open' AND (current_code=? OR prev_code=?)",
                   (code, code)).fetchone()
    if not s:
        return fail("الكود غلط أو الحصة اتقفلت.", "كود غير صالح")

    # 1) صلاحية الكود (حاضر / متأخر بفترة السماح / منتهي)
    n = now()
    grace = timedelta(seconds=int(get_setting("grace_seconds")))
    if code == s["current_code"]:
        exp = parse(s["code_expires_at"])
        status = "present" if n <= exp else ("late" if n <= exp + grace else None)
    else:
        status = "late" if n <= parse(s["prev_expires_at"]) else None
    if status is None:
        return fail("الكود انتهى. صوّر الـ QR الجديد من الشاشة.", "كود منتهي")

    cp = s["checkpoint"]
    # 2) مسجّل قبل كده في نفس نقطة التحقق؟
    if db.execute("SELECT 1 FROM attendance WHERE student_id=? AND session_id=? AND checkpoint=?",
                  (me["id"], s["id"], cp)).fetchone():
        return fail("انت مسجّل بالفعل في نقطة التحقق دي.", "تسجيل مكرر")

    # 3) جهاز واحد لطالب واحد
    dev = device_id()
    if db.execute("SELECT 1 FROM attendance WHERE session_id=? AND device_id=? AND student_id!=?",
                  (s["id"], dev, me["id"])).fetchone():
        return fail("الجهاز ده سجّل حضور طالب تاني في الحصة دي.", "جهاز مستخدم لطالب آخر")

    # 4) الموقع (GPS)
    distance = None
    if s["gps_required"] and s["lat"] is not None and s["lng"] is not None:
        if lat is None or lng is None:
            return fail("لازم تسمح للمتصفح بتحديد موقعك وتحاول تاني.", "الموقع غير متاح")
        distance = haversine(lat, lng, s["lat"], s["lng"])
        if distance > (s["radius"] or 150):
            return fail(f"انت بعيد عن القاعة (حوالي {int(distance)} متر).", f"خارج النطاق {int(distance)}م")

    try:
        db.execute("INSERT INTO attendance (student_id, session_id, checkpoint, status, method, device_id, "
                   "distance, created_at) VALUES (?, ?, ?, ?, 'qr', ?, ?, ?)",
                   (me["id"], s["id"], cp, status, dev, distance, now_str()))
        db.commit()
    except sqlite3.IntegrityError:
        return fail("انت مسجّل بالفعل في نقطة التحقق دي.", "تسجيل مكرر")
    log("تسجيل حضور", f"الطالب {me['student_no']} | حصة {s['id']} | نقطة {cp} | {STATUS_AR[status]}",
        actor=f"{me['name']} (student)")
    msg = f"تم تسجيل حضورك ✅ ({CP_NAMES[cp]})"
    if status == "late":
        msg += " - اتسجلت (متأخر) لأنك استخدمت الكود السابق."
    return jsonify(ok=True, message=msg)


# -------------------------------------------------------------------- الدكتور
@app.route("/doctor")
@require("doctor", "admin")
def doctor_home():
    db = get_db()
    courses = db.execute("SELECT * FROM courses ORDER BY code").fetchall()
    where, args = ("", ()) if session["role"] == "admin" else ("WHERE s.doctor_id=?", (session["uid"],))
    sessions = db.execute(
        "SELECT s.*, c.name AS course_name FROM sessions s JOIN courses c ON c.id=s.course_id "
        f"{where} ORDER BY s.id DESC LIMIT 20", args).fetchall()
    return render_template("doctor_home.html", courses=courses, sessions=sessions)


@app.route("/doctor/start", methods=["POST"])
@require("doctor", "admin")
def doctor_start():
    db = get_db()
    course = db.execute("SELECT * FROM courses WHERE id=?", (request.form.get("course_id", type=int),)).fetchone()
    if not course:
        flash("اختار المادة الأول.", "error")
        return redirect(url_for("doctor_home"))
    old = db.execute("SELECT id FROM sessions WHERE course_id=? AND doctor_id=? AND status='open'",
                     (course["id"], session["uid"])).fetchone()
    if old:
        return redirect(url_for("session_page", sid=old["id"]))
    lat, lng = to_float(get_setting("lat")), to_float(get_setting("lng"))
    exp = now() + timedelta(seconds=int(get_setting("rotate_seconds")))
    cur = db.execute(
        "INSERT INTO sessions (course_id, doctor_id, started_at, status, checkpoint, current_code, "
        "code_expires_at, lat, lng, radius, gps_required) VALUES (?, ?, ?, 'open', 1, ?, ?, ?, ?, ?, ?)",
        (course["id"], session["uid"], now_str(), new_code(db), exp.strftime(FMT), lat, lng,
         int(float(get_setting("radius"))), int(get_setting("gps_required"))))
    db.commit()
    log("فتح حصة", f"{course['name']} | حصة {cur.lastrowid}")
    return redirect(url_for("session_page", sid=cur.lastrowid))


@app.route("/session/<int:sid>")
@require("doctor", "admin")
def session_page(sid):
    db = get_db()
    s = load_session(db, sid)
    students = db.execute("SELECT * FROM students ORDER BY student_no").fetchall()
    gps_on = bool(s["gps_required"] and s["lat"] is not None)
    return render_template("session.html", s=s, students=students, gps_on=gps_on)


@app.route("/session/<int:sid>/state")
@require("doctor", "admin")
def session_state(sid):
    db = get_db()
    load_session(db, sid)
    s = rotate_if_needed(db, sid)
    end = parse(s["ended_at"]) if s["ended_at"] else now()
    out = {"status": s["status"], "checkpoint": s["checkpoint"],
           "elapsed": max(0, int((end - parse(s["started_at"])).total_seconds()))}
    if s["status"] == "open":
        out["code"] = s["current_code"]
        out["seconds_left"] = max(0, int((parse(s["code_expires_at"]) - now()).total_seconds()))
        out["total_seconds"] = int(get_setting("rotate_seconds"))
        if request.args.get("have") != s["current_code"]:
            out["url"] = f"{base_url()}/s/{s['current_code']}"
            out["qr"] = qr_data_uri(out["url"])
    students = db.execute("SELECT * FROM students ORDER BY student_no").fetchall()
    grouped = defaultdict(list)
    for r in db.execute("SELECT * FROM attendance WHERE session_id=?", (sid,)).fetchall():
        grouped[r["student_id"]].append(r)
    rows, counts = [], Counter()
    for st in students:
        rs = grouped.get(st["id"], [])
        fin = final_status(rs)
        cps = sorted(r["checkpoint"] for r in rs if r["checkpoint"] > 0 and r["status"] in ("present", "late"))
        manual = any(r["checkpoint"] == 0 for r in rs)
        label = STATUS_AR[fin]
        if s["status"] == "open" and fin == "absent" and not manual:
            label = "لم يسجّل" if not cps else f"ناقص ({len(cps)}/2)"
        counts[fin] += 1
        rows.append({"no": st["student_no"], "name": st["name"], "cps": cps, "status": fin,
                     "label": label, "manual": manual,
                     "time": max((r["created_at"] for r in rs), default="")[11:16]})
    out["rows"] = rows
    out["counts"] = {k: counts.get(k, 0) for k in STATUS_AR}
    return jsonify(out)


@app.route("/session/<int:sid>/checkpoint", methods=["POST"])
@require("doctor", "admin")
def session_checkpoint(sid):
    db = get_db()
    s = load_session(db, sid)
    if s["status"] == "open" and s["checkpoint"] < MAX_CHECKPOINTS:
        start_checkpoint(db, sid, s["checkpoint"] + 1)
        log("نقطة تحقق جديدة", f"حصة {sid} | نقطة {s['checkpoint'] + 1}")
    return redirect(url_for("session_page", sid=sid))


@app.route("/session/<int:sid>/close", methods=["POST"])
@require("doctor", "admin")
def session_close(sid):
    db = get_db()
    load_session(db, sid)
    db.execute("UPDATE sessions SET status='closed', ended_at=?, current_code=NULL, prev_code=NULL "
               "WHERE id=?", (now_str(), sid))
    db.commit()
    log("قفل حصة", f"حصة {sid}")
    return redirect(url_for("session_page", sid=sid))


@app.route("/session/<int:sid>/manual", methods=["POST"])
@require("doctor", "admin")
def session_manual(sid):
    db = get_db()
    s = load_session(db, sid)
    st = db.execute("SELECT * FROM students WHERE id=?", (request.form.get("student_id", type=int),)).fetchone()
    status = request.form.get("status", "")
    if not st:
        flash("اختار الطالب.", "error")
    elif status == "auto":
        db.execute("DELETE FROM attendance WHERE student_id=? AND session_id=? AND checkpoint=0", (st["id"], sid))
        db.commit()
        log("إلغاء تعديل يدوي", f"حصة {sid} | الطالب {st['student_no']}")
        flash("رجع حساب الحضور التلقائي للطالب.", "ok")
    elif status in STATUS_AR:
        db.execute("INSERT INTO attendance (student_id, session_id, checkpoint, status, method, created_at) "
                   "VALUES (?, ?, 0, ?, 'manual', ?) ON CONFLICT(student_id, session_id, checkpoint) "
                   "DO UPDATE SET status=excluded.status, method='manual', created_at=excluded.created_at",
                   (st["id"], sid, status, now_str()))
        db.commit()
        log("تسجيل يدوي", f"حصة {sid} | الطالب {st['student_no']} {st['name']} -> {STATUS_AR[status]}")
        flash(f"تم تسجيل {st['name']}: {STATUS_AR[status]}", "ok")
    return redirect(url_for("session_page", sid=sid))


# -------------------------------------------------------------------- الإدارة
def session_counts(sessions, students, matrix):
    out = []
    for se in sessions:
        c = Counter(matrix[(st["id"], se["id"])] for st in students)
        out.append({"id": se["id"], "label": se["started_at"][5:16],
                    **{k: c.get(k, 0) for k in STATUS_AR}})
    return out


def course_report(db, course_id):
    course = db.execute("SELECT * FROM courses WHERE id=?", (course_id,)).fetchone()
    sessions = db.execute("SELECT * FROM sessions WHERE course_id=? AND status='closed' ORDER BY id",
                          (course_id,)).fetchall()
    students = db.execute("SELECT * FROM students ORDER BY student_no").fetchall()
    matrix = statuses_for_sessions(db, sessions, students)
    summary = []
    for st in students:
        c = Counter(matrix[(st["id"], se["id"])] for se in sessions)
        counted = len(sessions) - c["excused"]
        pct = round((c["present"] + c["late"]) / counted * 100) if counted else 0
        summary.append({"no": st["student_no"], "name": st["name"], "grade": st["grade"] or "",
                        "total": len(sessions), "present": c["present"], "late": c["late"],
                        "excused": c["excused"], "absent": c["absent"], "pct": pct})
    return {"course": course, "sessions": sessions, "students": students, "matrix": matrix,
            "summary": summary, "per_session": session_counts(sessions, students, matrix)}


def pick_course(db):
    courses = db.execute("SELECT * FROM courses ORDER BY code").fetchall()
    cid = request.args.get("course_id", type=int) or (courses[0]["id"] if courses else None)
    return courses, cid


@app.route("/admin")
@require("admin")
def admin_dashboard():
    db = get_db()
    courses, cid = pick_course(db)
    stats = {
        "students": db.execute("SELECT COUNT(*) FROM students").fetchone()[0],
        "courses": len(courses),
        "sessions": db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
        "open": db.execute("SELECT COUNT(*) FROM sessions WHERE status='open'").fetchone()[0],
    }
    open_sessions = db.execute(
        "SELECT s.*, c.name AS course_name FROM sessions s JOIN courses c ON c.id=s.course_id "
        "WHERE s.status='open' ORDER BY s.id DESC").fetchall()
    chart, totals = [], {k: 0 for k in STATUS_AR}
    if cid:
        rep = course_report(db, cid)
        chart = rep["per_session"][-10:]
        for row in rep["per_session"]:
            for k in STATUS_AR:
                totals[k] += row[k]
    return render_template("admin_dashboard.html", courses=courses, cid=cid, stats=stats,
                           open_sessions=open_sessions, chart=chart, totals=totals)


@app.route("/admin/reports")
@require("admin")
def admin_reports():
    db = get_db()
    courses, cid = pick_course(db)
    rep = course_report(db, cid) if cid else None
    return render_template("admin_reports.html", courses=courses, cid=cid, rep=rep)


@app.route("/admin/export")
@require("admin")
def admin_export():
    db = get_db()
    cid = request.args.get("course_id", type=int)
    if not cid:
        abort(404)
    rep = course_report(db, cid)
    summary_cols = ["الرقم", "الاسم", "الفرقة", "عدد الحصص", "حاضر", "متأخر", "بعذر", "غائب", "نسبة الحضور %"]
    summary = [[r["no"], r["name"], r["grade"], r["total"], r["present"], r["late"], r["excused"],
                r["absent"], r["pct"]] for r in rep["summary"]]
    labels = [f"{se['started_at'][:16]} (#{se['id']})" for se in rep["sessions"]]
    detail = [[st["student_no"], st["name"]] + [STATUS_AR[rep["matrix"][(st["id"], se["id"])]]
                                                for se in rep["sessions"]]
              for st in rep["students"]]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        pd.DataFrame(summary, columns=summary_cols).to_excel(w, sheet_name="ملخص", index=False)
        pd.DataFrame(detail, columns=["الرقم", "الاسم"] + labels).to_excel(w, sheet_name="تفاصيل الحصص", index=False)
        for ws in w.sheets.values():
            ws.sheet_view.rightToLeft = True
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = 18
    buf.seek(0)
    log("تصدير Excel", f"مادة {rep['course']['code']}")
    return send_file(buf, as_attachment=True, download_name=f"attendance_{rep['course']['code']}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/admin/students", methods=["GET", "POST"])
@require("admin")
def admin_students():
    db = get_db()
    if request.method == "POST":
        no, name = request.form.get("student_no", "").strip(), request.form.get("name", "").strip()
        grade, pw = request.form.get("grade", "").strip(), request.form.get("password", "")
        if not no or not name or len(pw) < 4:
            flash("اكتب الرقم والاسم وكلمة سر (4 على الأقل).", "error")
        else:
            try:
                db.execute("INSERT INTO students (student_no, name, grade, password_hash) VALUES (?, ?, ?, ?)",
                           (no, name, grade, generate_password_hash(pw)))
                db.commit()
                log("إضافة طالب", f"{no} {name}")
                flash("تمت إضافة الطالب ✅", "ok")
            except sqlite3.IntegrityError:
                flash("رقم الطالب ده موجود قبل كده.", "error")
        return redirect(url_for("admin_students"))
    students = db.execute("SELECT * FROM students ORDER BY student_no").fetchall()
    return render_template("admin_students.html", students=students)


@app.route("/admin/students/bulk", methods=["POST"])
@require("admin")
def admin_students_bulk():
    db = get_db()
    added = skipped = 0
    for line in request.form.get("lines", "").splitlines():
        parts = [p.strip() for p in re.split(r"[,\t;،]", line)]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue
        no, name = parts[0], parts[1]
        grade = parts[2] if len(parts) > 2 else ""
        pw = parts[3] if len(parts) > 3 and len(parts[3]) >= 4 else no
        try:
            db.execute("INSERT INTO students (student_no, name, grade, password_hash) VALUES (?, ?, ?, ?)",
                       (no, name, grade, generate_password_hash(pw)))
            added += 1
        except sqlite3.IntegrityError:
            skipped += 1
    db.commit()
    log("إضافة طلاب دفعة واحدة", f"تمت {added} | متكرر {skipped}")
    flash(f"تمت إضافة {added} طالب" + (f" (وتجاهلت {skipped} رقم متكرر)" if skipped else ""), "ok")
    return redirect(url_for("admin_students"))


@app.route("/admin/students/<int:sid>/password", methods=["POST"])
@require("admin")
def admin_student_password(sid):
    pw = request.form.get("password", "")
    if len(pw) < 4:
        flash("كلمة السر لازم 4 على الأقل.", "error")
    else:
        db = get_db()
        db.execute("UPDATE students SET password_hash=? WHERE id=?", (generate_password_hash(pw), sid))
        db.commit()
        log("تغيير كلمة سر طالب", f"طالب رقم داخلي {sid}")
        flash("تم تغيير كلمة سر الطالب ✅", "ok")
    return redirect(url_for("admin_students"))


@app.route("/admin/students/<int:sid>/delete", methods=["POST"])
@require("admin")
def admin_student_delete(sid):
    db = get_db()
    db.execute("DELETE FROM students WHERE id=?", (sid,))
    db.commit()
    log("حذف طالب", f"طالب رقم داخلي {sid}")
    flash("تم حذف الطالب وسجل حضوره.", "ok")
    return redirect(url_for("admin_students"))


@app.route("/admin/manage", methods=["GET", "POST"])
@require("admin")
def admin_manage():
    db = get_db()
    if request.method == "POST":
        try:
            if request.form.get("kind") == "course":
                db.execute("INSERT INTO courses (code, name) VALUES (?, ?)",
                           (request.form["code"].strip(), request.form["name"].strip()))
                log("إضافة مادة", request.form["name"])
            else:
                pw = request.form["password"]
                role = "admin" if request.form.get("role") == "admin" else "doctor"
                if len(pw) < 4 or not request.form["username"].strip():
                    raise ValueError
                db.execute("INSERT INTO users (username, full_name, password_hash, role) VALUES (?, ?, ?, ?)",
                           (request.form["username"].strip().lower(), request.form["full_name"].strip(),
                            generate_password_hash(pw), role))
                log("إضافة مستخدم", f"{request.form['username']} ({role})")
            db.commit()
            flash("تمت الإضافة ✅", "ok")
        except (sqlite3.IntegrityError, ValueError, KeyError):
            flash("في بيانات ناقصة أو مكررة. راجعها وحاول تاني.", "error")
        return redirect(url_for("admin_manage"))
    return render_template("admin_manage.html",
                           courses=db.execute("SELECT * FROM courses ORDER BY code").fetchall(),
                           users=db.execute("SELECT * FROM users ORDER BY role, username").fetchall())


@app.route("/admin/settings", methods=["GET", "POST"])
@require("admin")
def admin_settings():
    if request.method == "POST":
        f = request.form
        lat, lng = to_float(f.get("lat")), to_float(f.get("lng"))
        rotate, grace, radius = f.get("rotate_seconds", type=int), f.get("grace_seconds", type=int), f.get("radius", type=int)
        if rotate is None or not 30 <= rotate <= 60 or grace is None or not 0 <= grace <= 120 \
                or radius is None or not 20 <= radius <= 1000:
            flash("راجع الأرقام: مدة الكود من 30 لـ 60 ثانية، فترة السماح من 0 لـ 120، النطاق من 20 لـ 1000 متر.", "error")
        else:
            set_setting("lat", "" if lat is None else lat)
            set_setting("lng", "" if lng is None else lng)
            set_setting("radius", radius)
            set_setting("rotate_seconds", rotate)
            set_setting("grace_seconds", grace)
            set_setting("gps_required", 1 if f.get("gps_required") else 0)
            set_setting("base_url", f.get("base_url", "").strip())
            set_setting("email_domain", f.get("email_domain", "").strip().lower().lstrip("@"))
            get_db().commit()
            log("تعديل الإعدادات")
            flash("تم حفظ الإعدادات ✅ (تسري على الحصص الجديدة)", "ok")
        return redirect(url_for("admin_settings"))
    return render_template("admin_settings.html", s={k: get_setting(k) for k in DEFAULTS}, lan=lan_ip())


@app.route("/admin/timetable", methods=["GET", "POST"])
@require("admin")
def admin_timetable():
    db = get_db()
    if request.method == "POST":
        f = request.form
        course_id, weekday = f.get("course_id", type=int), f.get("weekday", type=int)
        start, end = f.get("start_time", ""), f.get("end_time", "")
        ok_time = all(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t) for t in (start, end))
        if not course_id or weekday is None or not 0 <= weekday <= 6 or not ok_time or start >= end:
            flash("راجع البيانات: المادة واليوم ووقت البداية قبل وقت النهاية.", "error")
        else:
            grp = f.get("grp", "").strip().upper()
            db.execute("INSERT INTO timetable (course_id, doctor_id, weekday, start_time, end_time, room, grp) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (course_id, f.get("doctor_id", type=int), weekday, start, end, f.get("room", "").strip(),
                        grp if grp in GROUPS else None))
            db.commit()
            log("إضافة حصة للجدول", f"مادة {course_id} | {WEEKDAYS[weekday]} {start}")
            flash("تمت إضافة الحصة للجدول ✅", "ok")
        return redirect(url_for("admin_timetable"))
    rows = db.execute(
        "SELECT t.*, c.name AS course_name, u.full_name AS doctor_name FROM timetable t "
        "JOIN courses c ON c.id=t.course_id LEFT JOIN users u ON u.id=t.doctor_id").fetchall()
    rows = sorted(rows, key=lambda r: ((r["weekday"] + 2) % 7, r["start_time"]))   # من السبت
    return render_template("admin_timetable.html", rows=rows, WEEKDAYS=WEEKDAYS,
                           courses=db.execute("SELECT * FROM courses ORDER BY code").fetchall(),
                           doctors=db.execute("SELECT * FROM users ORDER BY full_name").fetchall())


@app.route("/admin/timetable/<int:tid>/delete", methods=["POST"])
@require("admin")
def admin_timetable_delete(tid):
    db = get_db()
    db.execute("DELETE FROM timetable WHERE id=?", (tid,))
    db.commit()
    log("حذف حصة من الجدول", f"رقم {tid}")
    return redirect(url_for("admin_timetable"))


@app.route("/admin/logs")
@require("admin")
def admin_logs():
    rows = get_db().execute("SELECT * FROM logs ORDER BY id DESC LIMIT 300").fetchall()
    return render_template("admin_logs.html", rows=rows)


# ---------------------------------------------------------------------- تشغيل
ensure_schema()

if __name__ == "__main__":
    if not os.path.exists(DB_PATH):
        raise SystemExit("مفيش قاعدة بيانات. شغّل الأول:  python init_db.py")
    ip = lan_ip()
    ssl = None
    scheme = "http"
    if os.environ.get("USE_HTTPS") == "1":
        ssl, scheme = "adhoc", "https"   # محتاج: pip install cryptography
    print("\n=== النظام شغال ===")
    print(f"من الكمبيوتر : {scheme}://127.0.0.1:5000")
    print(f"من الموبايل  : {scheme}://{ip}:5000   (لازم على نفس الواي فاي)\n")
    app.run(host="0.0.0.0", port=5000, debug=False, ssl_context=ssl)
