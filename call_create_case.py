# call_create_case.py
from app.services.db_service import DBService
payload = {
    "name": "Unit Test",
    "phone": "9876543210",
    "email": "unit@test.com",
    "crime_type": "scam",
    "incident_date": "2025-10-02",
    "description": "Testing create_case via helper",
    "amount_lost": 0.0,
    "evidence": "",
    "is_emergency": False,
    "consent_recorded": True,
    "transcript": "test"
}
cid = DBService.create_case(payload)
print("create_case returned:", cid)
