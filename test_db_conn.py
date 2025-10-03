# test_db_conn.py
import os, uuid, datetime, traceback
from sqlalchemy import create_engine, text

db_url = os.getenv("POSTGRES_URI")
print("POSTGRES_URI seen by this process:", repr(db_url))
if not db_url:
    raise SystemExit("POSTGRES_URI not set in this environment")

try:
    engine = create_engine(db_url, echo=True, pool_pre_ping=True)
    with engine.connect() as conn:
        # show DB and user
        print("current_database/user ->", conn.execute(text("SELECT current_database(), current_user")).fetchone())
        # count rows
        cnt = conn.execute(text("SELECT COUNT(*) FROM cases")).scalar()
        print("cases count:", cnt)
        # insert one test row (unique id)
        tid = f"TEST-{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}-{str(uuid.uuid4())[:4]}"
        print("inserting test id:", tid)
        conn.execute(text(
            "INSERT INTO cases (id, name, phone, email, description, created_at) VALUES (:id, :name, :phone, :email, :desc, now())"
        ), {"id": tid, "name": "DBG Test", "phone": "9999999999", "email": "dbg@example.com", "desc": "test insert"})
        # commit if using transactional connection
        conn.execute(text("COMMIT"))
        print("inserted. new count:", conn.execute(text("SELECT COUNT(*) FROM cases")).scalar())
except Exception:
    traceback.print_exc()
    print("DB connection or SQL error above")
