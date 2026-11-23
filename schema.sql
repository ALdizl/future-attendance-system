PRAGMA foreign_keys = ON;

DROP TABLE IF EXISTS timetable;
DROP TABLE IF EXISTS logs;
DROP TABLE IF EXISTS attendance;
DROP TABLE IF EXISTS sessions;
DROP TABLE IF EXISTS courses;
DROP TABLE IF EXISTS students;
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS settings;

-- حسابات الدكتور والإدارة
CREATE TABLE users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    full_name     TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('doctor', 'admin'))
);

-- الطلاب
CREATE TABLE students (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    student_no    TEXT UNIQUE NOT NULL,
    name          TEXT NOT NULL,
    grade         TEXT,
    email         TEXT,
    grp           TEXT,          -- G1 أو G2
    password_hash TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_students_email ON students(email);

-- المواد
CREATE TABLE courses (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL
);

-- الحصص (الكود الحالي + الكود السابق لفترة السماح)
CREATE TABLE sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id       INTEGER NOT NULL REFERENCES courses(id),
    doctor_id       INTEGER NOT NULL REFERENCES users(id),
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    checkpoint      INTEGER NOT NULL DEFAULT 1,
    current_code    TEXT,
    code_expires_at TEXT,
    prev_code       TEXT,
    prev_expires_at TEXT,
    lat             REAL,
    lng             REAL,
    radius          INTEGER,
    gps_required    INTEGER NOT NULL DEFAULT 1
);

-- الحضور: صف لكل نقطة تحقق (1 و2 و3)، والصف رقم 0 = تعديل يدوي نهائي
CREATE TABLE attendance (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    checkpoint INTEGER NOT NULL,
    status     TEXT NOT NULL CHECK (status IN ('present', 'late', 'absent', 'excused')),
    method     TEXT NOT NULL DEFAULT 'qr',
    device_id  TEXT,
    distance   REAL,
    created_at TEXT NOT NULL,
    UNIQUE (student_id, session_id, checkpoint)
);

-- سجل العمليات
CREATE TABLE logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    details    TEXT,
    created_at TEXT NOT NULL
);

-- الإعدادات (موقع المعهد، مدة الكود، ...)
CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- جدول الحصص الأسبوعي (weekday: 0 = الاثنين ... 6 = الأحد)
CREATE TABLE timetable (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id  INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    doctor_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    weekday    INTEGER NOT NULL,
    start_time TEXT NOT NULL,
    end_time   TEXT NOT NULL,
    room       TEXT,
    grp        TEXT          -- G1 أو G2، وفاضي = لكل المجموعات
);
