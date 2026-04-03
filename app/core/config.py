import os
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
DATABASE_URL = "sqlite+aiosqlite:///./jobs.db"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
