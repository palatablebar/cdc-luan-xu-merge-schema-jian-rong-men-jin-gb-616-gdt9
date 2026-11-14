from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class VersionReviewIn(BaseModel):
    # 复核结论：approved 放行 / blocked 拦截 / pending_review 仅触发基线重算
    decision: Literal["approved", "blocked", "pending_review"]
    note: str | None = Field(default=None, max_length=1000)


class VersionActivateIn(BaseModel):
    note: str | None = Field(default=None, max_length=1000)
