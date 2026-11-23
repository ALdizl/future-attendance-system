# -*- coding: utf-8 -*-
"""للاستضافة (الموقع الحقيقي): ينشئ قاعدة بيانات فاضية فيها حساب الإدارة بس، من غير أي حسابات تجريبية.
التشغيل:  python setup_production.py
"""
import getpass
import os
import sqlite3
import sys

from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "attendance.db")

if os.path.exists(DB_PATH):
    sys.exit("attendance.db موجودة بالفعل، مش هعمل حاجة (عشان ماتتمسحش بيانات).")

pw = getpass.getpass("اكتب كلمة سر قوية لحساب الإدارة (admin) - 8 حروف على الأقل: ")
if len(pw) < 8:
    sys.exit("كلمة السر قصيرة. شغّل الأمر تاني.")
if pw != getpass.getpass("اكتبها تاني للتأكيد: "):
    sys.exit("الكلمتين مش زي بعض. شغّل الأمر تاني.")

db = sqlite3.connect(DB_PATH)
with open(os.path.join(BASE_DIR, "schema.sql"), encoding="utf-8") as f:
    db.executescript(f.read())
db.execute("INSERT INTO users (username, full_name, password_hash, role) VALUES (?, ?, ?, ?)",
           ("admin", "الدكتورة (الإدارة)", generate_password_hash(pw), "admin"))
db.commit()
db.close()
print("تم إنشاء قاعدة البيانات وحساب الإدارة admin ✅ (من غير أي حسابات تجريبية)")
