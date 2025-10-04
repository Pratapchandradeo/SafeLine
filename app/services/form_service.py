# app/services/form_service.py
import os
from app.services.db_service import DBService

class FormService:
    def __init__(self):
        self.base_url = os.getenv('BASE_URL', 'http://localhost:5000')

    def get_prefill_link(self, case_id: str) -> str:
        return f"{self.base_url}/f/{case_id}"

    def get_case_data_for_form(self, case_id: str) -> dict:
        try:
            print(f"🔍 FormService.get_case_data_for_form called for: {case_id}")
            case_data = DBService.retrieve_case(case_id)
            if case_data:
                print(f"✅ Found case data for form: {case_id}")
                return case_data
            else:
                print(f"❌ No case data found for: {case_id}")
                return {}
        except Exception as e:
            print(f"❌ Error in get_case_data_for_form: {e}")
            import traceback
            traceback.print_exc()
            return {}