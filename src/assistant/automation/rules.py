"""Versioned persisted automation rules remain independent of prompt schemas."""

from pydantic import BaseModel, ConfigDict


class RuleBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int
