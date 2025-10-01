# app/services/form_service.py
import os
from app.services.db_service import DBService

class FormService:
    def __init__(self):
        self.base_url = os.getenv('BASE_URL', 'http://localhost:5000')

    def get_prefill_link(self, case_id: str) -> str:
        return f"{self.base_url}/f/{case_id}"

    def get_case_data_for_form(self, case_id: str) -> dict:
        db_service = DBService()
        case = db_service.retrieve_case(case_id)  # Add retrieve_case to DBService if needed
        return case or {}