from pydantic import BaseModel, Field


class SubjectId(BaseModel):
    type: str = Field(..., examples=["national_id"])
    value: str = Field(..., examples=["7615076397"])
