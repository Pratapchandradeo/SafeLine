# app/services/db_service.py
import os
import uuid
import datetime
from typing import Dict, Any, Optional
from sqlalchemy.exc import SQLAlchemyError

# Import shared database configuration and Case model
from app.services.database import SessionLocal, Case, init_db

class DBService:
    @staticmethod
    def get_session():
        """Get a new database session"""
        return SessionLocal()

    @staticmethod
    def generate_case_id() -> str:
        timestamp = datetime.datetime.now().strftime('%Y%m%d')
        unique_id = str(uuid.uuid4())[:8].upper()
        return f"CR-{timestamp}-{unique_id}"

    @staticmethod
    def create_case(data: Dict[str, Any]) -> Optional[str]:
        """Create a new case in the database"""
        print(f"🔍 DBService.create_case called with data keys: {list(data.keys())}")
        
        db = DBService.get_session()
        case_id = DBService.generate_case_id()
        
        try:
            # Filter only valid columns and convert empty strings to None
            valid_cols = {col.name for col in Case.__table__.columns}
            case_kwargs = {}
            
            for k, v in data.items():
                if k in valid_cols:
                    # Convert empty strings to None for nullable fields
                    if v == "" or v is None:
                        case_kwargs[k] = None
                    else:
                        case_kwargs[k] = v

            print(f"ℹ️ Creating case {case_id} with filtered data: {case_kwargs}")
            
            case = Case(id=case_id, **case_kwargs)
            db.add(case)
            db.commit()
            db.refresh(case)
            print(f"✅ Case saved successfully: {case_id}")
            return case_id
            
        except Exception as e:
            print(f"❌ Failed to save case: {e}")
            import traceback
            traceback.print_exc()
            db.rollback()
            return None
        finally:
            db.close()

    @staticmethod
    def retrieve_case(case_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a case by ID for the form service"""
        print(f"🔍 DBService.retrieve_case called for: {case_id}")
        db = DBService.get_session()
        try:
            case = db.query(Case).filter(Case.id == case_id).first()
            if case:
                # Convert SQLAlchemy object to dictionary
                case_dict = {
                    'id': case.id,
                    'name': case.name,
                    'phone': case.phone,
                    'email': case.email,
                    'crime_type': case.crime_type,
                    'incident_date': case.incident_date,
                    'description': case.description,
                    'amount_lost': case.amount_lost,
                    'evidence': case.evidence,
                    'is_emergency': case.is_emergency,
                    'consent_recorded': case.consent_recorded,
                    'transcript': case.transcript,
                    'created_at': case.created_at.isoformat() if case.created_at else None
                }
                print(f"✅ Retrieved case: {case_id}")
                return case_dict
            else:
                print(f"❌ Case not found: {case_id}")
                return None
        except Exception as e:
            print(f"❌ Error retrieving case {case_id}: {e}")
            import traceback
            traceback.print_exc()
            return None
        finally:
            db.close()

    @staticmethod
    def update_case(case_id: str, update_data: Dict[str, Any]) -> bool:
        """Update an existing case with new data"""
        print(f"🔍 DBService.update_case called for: {case_id}")
        print(f"📝 Update data: {update_data}")
        
        db = DBService.get_session()
        
        try:
            # Find the case
            case = db.query(Case).filter(Case.id == case_id).first()
            if not case:
                print(f"❌ Case not found for update: {case_id}")
                return False
            
            # Update fields
            updates_made = 0
            for key, value in update_data.items():
                if hasattr(case, key) and value is not None:
                    current_value = getattr(case, key)
                    print(f"🔄 Updating {key}: from '{current_value}' to '{value}'")
                    setattr(case, key, value)
                    updates_made += 1
            
            if updates_made > 0:
                db.commit()
                print(f"✅ Case updated successfully: {case_id} ({updates_made} fields updated)")
                return True
            else:
                print(f"⚠️ No updates were made for case: {case_id}")
                return True  # Return True since no changes needed
            
        except Exception as e:
            print(f"❌ Failed to update case {case_id}: {e}")
            import traceback
            traceback.print_exc()
            db.rollback()
            return False
        finally:
            db.close()