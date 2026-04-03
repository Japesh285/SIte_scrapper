import sys
import re
import pandas as pd
from sqlalchemy import create_engine, Column, Integer, String, Float, JSON, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker

file_path = "/home/hp_jp/ai_scrapper/Companies_List (2).xlsx"

try:
    df = pd.read_excel(file_path)
except Exception as e:
    print(f"Error reading Excel file: {e}")
    sys.exit(1)

original_cols = list(df.columns)
print(f"Original columns: {original_cols}")

# Normalize column names
df.columns = (
    df.columns
    .astype(str)
    .str.strip()
    .str.lower()
    .str.replace(r"\s+", "_", regex=True)
    .str.replace(r"[()]", "", regex=True)
)

print(f"Normalized columns: {list(df.columns)}")


def find_column(keywords):
    """Find a column name by matching keywords."""
    for col in df.columns:
        if any(kw in col for kw in keywords):
            return col
    return None


company_col = find_column(["company", "name", "organization", "employer"])

# Prioritize career/career_page columns over generic 'website'
url_col = find_column(["india_career_page"])
if not url_col:
    url_col = find_column(["career_page", "career", "url", "link"])
if not url_col:
    url_col = find_column(["page", "website"])

if not company_col:
    print(f"Error: Could not detect company name column.")
    print(f"Available columns: {list(df.columns)}")
    sys.exit(1)

if not url_col:
    print(f"Error: Could not detect career URL column.")
    print(f"Available columns: {list(df.columns)}")
    sys.exit(1)

print(f"Detected company column: '{company_col}'")
print(f"Detected URL column: '{url_col}'")

# Keep and rename columns
df = df[[company_col, url_col]].rename(
    columns={company_col: "name", url_col: "url"}
)

# Drop rows where both are empty
df = df.dropna(subset=["name", "url"], how="all")

# Drop duplicate URLs (keep first occurrence)
df = df.drop_duplicates(subset=["url"], keep="first")

print(f"Rows to insert (after dedup): {len(df)}")

# Create SQLite engine
engine = create_engine("sqlite:///jobs.db")

# Define ORM models matching the FastAPI schema
Base = declarative_base()


class Site(Base):
    __tablename__ = "sites"

    id = Column(Integer, primary_key=True, index=True)
    domain = Column(String, unique=True, nullable=False)
    type = Column(String, nullable=False)
    confidence = Column(Float, default=0.0)


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, index=True)
    site_id = Column(Integer, ForeignKey("sites.id"), nullable=False)
    title = Column(String, nullable=False)
    location = Column(String, default="")
    url = Column(String, nullable=False)
    raw_json = Column(JSON, default=dict)


# Create all tables
Base.metadata.create_all(engine)
print("✅ Tables created: 'sites' and 'jobs'")

# Insert data from Excel into sites table
Session = sessionmaker(bind=engine)
session = Session()

try:
    for _, row in df.iterrows():
        site = Site(
            domain=row["url"],
            type="unknown",
            confidence=0.0
        )
        session.add(site)
    session.commit()
    print(f"✅ Data loaded: {len(df)} rows inserted into 'sites' table")
except Exception as e:
    session.rollback()
    print(f"❌ Error inserting data: {e}")
    sys.exit(1)
finally:
    session.close()
