# app/services/db_service.py
import os
import uuid
import datetime
from typing import Dict, Any, Optional
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine, Column, String, DateTime, Boolean, Float, Text, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.exc import SQLAlchemyError
from dotenv import load_dotenv

load_dotenv()

Base = declarative_base()

class Case(Base):
    __tablename__ = 'cases'
    id = Column(String, primary_key=True)
    name = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    email = Column(String, nullable=True)
    crime_type = Column(String, nullable=True)
    incident_date = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    amount_lost = Column(Float, nullable=True)
    evidence = Column(String, nullable=True)
    is_emergency = Column(Boolean, default=False)
    consent_recorded = Column(Boolean, default=False)
    transcript = Column(Text, nullable=True)
    created_at = Column(DateTime, default=func.now())

db_url = os.getenv("POSTGRES_URI")
if not db_url:
    raise RuntimeError("POSTGRES_URI env var not set for this process.")
print("ℹ️ DBService using POSTGRES_URI:", db_url)

# Create engine with more debugging
engine = create_engine(db_url, echo=True, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Create tables if they don't exist
try:
    Base.metadata.create_all(bind=engine)
    print("✅ Database tables created/verified")
except Exception as e:
    print(f"⚠️ Database table creation failed: {e}")

class DBService:
    @staticmethod
    def get_session():
        return SessionLocal()

    @staticmethod
    def generate_case_id() -> str:
        timestamp = datetime.datetime.now().strftime('%Y%m%d')
        unique_id = str(uuid.uuid4())[:8].upper()
        return f"CR-{timestamp}-{unique_id}"

    @staticmethod
    def create_case(data: Dict[str, Any]) -> Optional[str]:
        print(f"🔍 DBService.create_case called with data keys: {list(data.keys())}")
        print(f"🔍 Data content: {data}")
        
        db = DBService.get_session()
        case_id = DBService.generate_case_id()
        
        # Filter only valid columns and convert empty strings to None
        valid_cols = {col.name for col in Case.__table__.columns}
        case_kwargs = {}
        
        for k, v in data.items():
            if k in valid_cols:
                # Convert empty strings to None for nullable fields
                if v == "":
                    case_kwargs[k] = None
                else:
                    case_kwargs[k] = v

        print(f"ℹ️ Creating case {case_id} with filtered data: {case_kwargs}")
        
        try:
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