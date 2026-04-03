from sqlalchemy import Column, Integer, String, Float, ForeignKey, JSON, UniqueConstraint
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class Site(Base):
    __tablename__ = "sites"

    id = Column(Integer, primary_key=True, index=True)
    domain = Column(String, unique=True, nullable=False)
    type = Column(String, nullable=False)
    confidence = Column(Float, default=0.0)

    jobs = relationship("Job", back_populates="site")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("site_id", "url", name="uq_jobs_site_url"),)

    id = Column(Integer, primary_key=True, index=True)
    site_id = Column(Integer, ForeignKey("sites.id"), nullable=False)
    title = Column(String, nullable=False)
    location = Column(String, default="")
    url = Column(String, nullable=False)
    raw_json = Column(JSON, default=dict)

    site = relationship("Site", back_populates="jobs")
