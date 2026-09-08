import sys, json
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, ".")
from app.database import init_db, query
init_db()
courses = query("SELECT id,name,code FROM courses ORDER BY id")
print("Courses:")
for c in courses:
    print(f"  {c['id']}: [{c['code']}] {c['name']}")
print()
acal = query("SELECT * FROM academic_calendar")
print(f"Academic calendar entries: {len(acal)}")
for a in acal[:20]:
    print(f"  {a}")
print()
chap = query("SELECT * FROM chapters")
print(f"Chapters: {len(chap)}")
for c in chap:
    print(f"  {c}")
