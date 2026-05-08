"""
Shared types across STT backends.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TranscriptionResult:
    text: str
    language: Optional[str]
    language_probability: Optional[float]
    duration: Optional[float]
    segments: list[dict] = field(default_factory=list)
