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

            case = Case(id=case_id, **case_kwargs)
            db.add(case)
            db.commit()
            db.refresh(case)
            return case_id
            
        except Exception:
            db.rollback()
            return None
        finally:
            db.close()

    @staticmethod
    def retrieve_case(case_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a case by ID for the form service"""
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
                return case_dict
            else:
                return None
        except Exception:
            return None
        finally:
            db.close()

    @staticmethod
    def update_case(case_id: str, update_data: Dict[str, Any]) -> bool:
        """Update an existing case with new data"""
        db = DBService.get_session()
        
        try:
            # Find the case
            case = db.query(Case).filter(Case.id == case_id).first()
            if not case:
                return False
            
            # Update fields
            updates_made = 0
            for key, value in update_data.items():
                if hasattr(case, key) and value is not None:
                    setattr(case, key, value)
                    updates_made += 1
            
            if updates_made > 0:
                db.commit()
                return True
            else:
                return True  # Return True since no changes needed
            
        except Exception:
            db.rollback()
            return False
        finally:
            db.close()