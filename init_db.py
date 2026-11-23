# -*- coding: utf-8 -*-
"""ينشئ قاعدة البيانات attendance.db ويضيف بيانات تجريبية.
للتشغيل:  python init_db.py
لو القاعدة موجودة ومحتاج تمسحها وتبدأ من الأول:  python init_db.py --reset
"""
import os
import sqlite3
import sys

from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "attendance.db")

if os.path.exists(DB_PATH) and "--reset" not in sys.argv:
    print("attendance.db موجودة بالفعل. لو عاوز تمسحها وتبدأ من الأول شغّل:")
    print("    python init_db.py --reset")
    sys.exit()

if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

db = sqlite3.connect(DB_PATH)
with open(os.path.join(BASE_DIR, "schema.sql"), encoding="utf-8") as f:
    db.executescript(f.read())

h = generate_password_hash
db.executemany(
    "INSERT INTO users (username, full_name, password_hash, role) VALUES (?, ?, ?, ?)",
    [
        ("admin", "الدكتورة (الإدارة)", h("admin123"), "admin"),
        ("doctor1", "دكتور المادة", h("1234"), "doctor"),
    ],
)
db.executemany(
    "INSERT INTO students (student_no, name, grade, password_hash) VALUES (?, ?, ?, ?)",
    [
        ("1001", "أحمد علي", "الفرقة الأولى", h("1234")),
        ("1002", "منى سمير", "الفرقة الأولى", h("1234")),
        ("1003", "كريم حسن", "الفرقة الأولى", h("1234")),
    ],
)
db.execute("INSERT INTO courses (code, name) VALUES (?, ?)", ("INF101", "نظم المعلومات"))
db.commit()
db.close()
print("تم إنشاء attendance.db بنجاح ✅")
print("الحسابات التجريبية:")
print("  إدارة : admin / admin123")
print("  دكتور : doctor1 / 1234")
print("  طالب  : 1001 / 1234  (و1002 و1003 بنفس كلمة السر)")
