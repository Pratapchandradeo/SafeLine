# app/services/form_service.py
import os

class FormService:
    def __init__(self):
        self.base_url = os.getenv('BASE_URL', 'http://localhost:5000')

    def get_prefill_link(self, case_id: str) -> str:
        return f"{self.base_url}/f/{case_id}"

    def get_case_data_for_form(self, case_id: str) -> dict:
        try:
            # Import here to avoid circular imports
            from app.services.db_service import DBService
            
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

    def update_case_from_form(self, case_id: str, form_data: dict) -> bool:
        """Update case with edited form data"""
        try:
            # Import here to avoid circular imports
            from app.services.db_service import DBService
            
            print(f"🔍 FormService.update_case_from_form called for: {case_id}")
            print(f"📝 Form data: {form_data}")
            
            # Map form fields to database fields
            update_data = {
                'name': form_data.get('name'),
                'phone': form_data.get('phone'),
                'email': form_data.get('email'),
                'crime_type': form_data.get('crime_type'),
                'incident_date': form_data.get('incident_date'),
                'description': form_data.get('description'),
                'amount_lost': form_data.get('amount_lost'),
                'evidence': form_data.get('evidence')
            }
            
            # FIX: Properly handle optional amount_lost field
            amount_lost = update_data['amount_lost']
            if amount_lost and str(amount_lost).strip():  # Check if not empty
                try:
                    update_data['amount_lost'] = float(amount_lost)
                except (ValueError, TypeError):
                    # If conversion fails, set to None (optional field)
                    update_data['amount_lost'] = None
            else:
                # Empty string or None should be stored as NULL in database
                update_data['amount_lost'] = None
            
            # For other string fields, keep empty strings if needed
            # But convert empty strings to None for optional fields
            optional_fields = ['email', 'evidence']
            for field in optional_fields:
                if update_data.get(field) == '':
                    update_data[field] = None
            
            # Remove None values but keep empty strings for required fields
            update_data = {k: v for k, v in update_data.items() if v is not None}
            
            success = DBService.update_case(case_id, update_data)
            if success:
                print(f"✅ Case updated successfully: {case_id}")
            else:
                print(f"❌ Failed to update case: {case_id}")
            
            return success
            
        except Exception as e:
            print(f"❌ Error in update_case_from_form: {e}")
            import traceback
            traceback.print_exc()
            return False