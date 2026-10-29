"""Shared base class for every domain model: immutable, and unknown fields are errors."""

from pydantic import BaseModel, ConfigDict


class DomainModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
