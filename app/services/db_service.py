# app/services/db_service.py (added retrieve_case method)
import uuid
import os
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
    name = Column(String)
    phone = Column(String)
    email = Column(String)
    crime_type = Column(String)
    incident_date = Column(String)
    description = Column(Text)
    amount_lost = Column(Float, nullable=True)
    evidence = Column(String, nullable=True)
    is_emergency = Column(Boolean, default=False)
    consent_recorded = Column(Boolean, default=False)
    transcript = Column(Text)
    created_at = Column(DateTime, default=func.now())

engine = create_engine(os.getenv('POSTGRES_URI', 'postgresql://postgres:Error@localhost:5432/safe_line'))
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base.metadata.create_all(bind=engine)

class DBService:
    @staticmethod
    def get_session():
        return SessionLocal()

    @staticmethod
    def generate_case_id() -> str:
        return f"CR-{datetime.datetime.now().strftime('%Y%m%d')}-{str(uuid.uuid4())[:4].upper()}"

    @staticmethod
    def create_case(data: Dict[str, Any]) -> Optional[str]:
        db = DBService.get_session()
        try:
            case_id = DBService.generate_case_id()
            case_kwargs = {k: v for k, v in data.items() if hasattr(Case, k)}
            case = Case(id=case_id, **case_kwargs)
            db.add(case)
            db.commit()
            print(f"💾 Saved case: {case_id}")
            return case_id
        except SQLAlchemyError as e:
            db.rollback()
            print(f"DB Error: {e}")
            return None
        finally:
            db.close()

    @staticmethod
    def retrieve_case(case_id: str) -> Optional[Dict[str, Any]]:
        db = DBService.get_session()
        try:
            result = db.query(Case).filter(Case.id == case_id).first()
            if result:
                return {col.name: getattr(result, col.name) for col in result.__table__.columns}
            return None
        except SQLAlchemyError as e:
            print(f"DB Error: {e}")
            return None
        finally:
            db.close()