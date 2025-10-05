import os
from sqlalchemy import create_engine, Column, String, DateTime, Boolean, Float, Text, func
from sqlalchemy.orm import sessionmaker, declarative_base
from dotenv import load_dotenv

load_dotenv()

Base = declarative_base()

# Define the Case model HERE in this file
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

    def __repr__(self):
        return f"<Case(id='{self.id}', name='{self.name}', crime_type='{self.crime_type}')>"

# Database setup - SHARED configuration
db_url = os.getenv("POSTGRES_URI")
if not db_url:
    raise RuntimeError("POSTGRES_URI env var not set.")

# Create engine with connection pooling
engine = create_engine(db_url, echo=True, pool_pre_ping=True, pool_recycle=3600)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    """Get database session for Flask app"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    """Initialize database tables"""
    try:
        Base.metadata.create_all(bind=engine)
    except Exception:
        pass

# Initialize database when this module is imported
init_db()