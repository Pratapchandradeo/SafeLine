from sqlalchemy import Column, String, DateTime, Boolean, Float, Text, func
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine
import os

Base = declarative_base()

class Case(Base):
    __tablename__ = 'cases'
    
    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=False)
    email = Column(String, nullable=False)
    crime_type = Column(String, nullable=False)
    incident_date = Column(String, nullable=False)
    description = Column(Text, nullable=False)
    amount_lost = Column(Float, nullable=True)
    evidence = Column(String, nullable=True)
    is_emergency = Column(Boolean, default=False)
    consent_recorded = Column(Boolean, default=False)
    transcript = Column(Text, nullable=True)
    created_at = Column(DateTime, default=func.now())