from pydantic import BaseModel
from typing import Optional


class JobSchema(BaseModel):
    title: str
    location: Optional[str] = ""
    url: str
