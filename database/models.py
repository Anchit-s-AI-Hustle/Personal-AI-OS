"""
Light value-object models. We don't use a full ORM — sqlite3.Row is plenty
for our access patterns — but typed constructors make the rest of the code
cleaner.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

URGENCY_VALUES = ("Low", "Medium", "High", "Critical")
SOURCE_TYPES = ("Email", "Meeting", "Conversation")

# Keep aligned with ai/prompts.py:GROWTH_PILLARS_LIST.
GROWTH_PILLARS = (
    "Acquisition",
    "Conversion",
    "AOV",
    "Retention",
    "Marketplace",
    "Operations",
    "Brand & Content",
    "Margin",
    "Team & Process",
    "Other",
)


def normalise_urgency(raw: Optional[str]) -> str:
    if not raw:
        return "Medium"
    s = raw.strip().lower()
    table = {
        "low": "Low",
        "medium": "Medium",
        "med": "Medium",
        "normal": "Medium",
        "high": "High",
        "urgent": "High",
        "critical": "Critical",
        "blocker": "Critical",
        "p0": "Critical",
        "p1": "High",
        "p2": "Medium",
        "p3": "Low",
    }
    return table.get(s, "Medium")


def normalise_growth_pillar(raw: Optional[str]) -> str:
    """Snap whatever Gemini returned to the closest known pillar."""
    if not raw:
        return "Other"
    s = raw.strip()
    # Exact match (case-insensitive).
    for p in GROWTH_PILLARS:
        if p.lower() == s.lower():
            return p
    # Cheap synonym table for common misses.
    syn = {
        "acq": "Acquisition",
        "acquire": "Acquisition",
        "performance marketing": "Acquisition",
        "paid media": "Acquisition",
        "cro": "Conversion",
        "checkout": "Conversion",
        "pdp": "Conversion",
        "upsell": "AOV",
        "bundle": "AOV",
        "gifting": "AOV",
        "ltv": "Retention",
        "subscription": "Retention",
        "loyalty": "Retention",
        "klaviyo": "Retention",
        "amazon": "Marketplace",
        "flipkart": "Marketplace",
        "marketplaces": "Marketplace",
        "ops": "Operations",
        "fulfilment": "Operations",
        "fulfillment": "Operations",
        "customer service": "Operations",
        "cx": "Operations",
        "pr": "Brand & Content",
        "content": "Brand & Content",
        "social": "Brand & Content",
        "influencer": "Brand & Content",
        "cogs": "Margin",
        "freight": "Margin",
        "packaging": "Margin",
        "hiring": "Team & Process",
        "process": "Team & Process",
    }
    low = s.lower()
    for needle, pillar in syn.items():
        if needle in low:
            return pillar
    return "Other"


@dataclass
class ExtractedTask:
    # Short imperative — appears in the "Task Heading" sheet column and is
    # also the dedup key (combined with source_ref_id).
    task_heading: str

    # Longer 1-3 sentence detail of what to do — "Task Description" column.
    task_description: str = ""

    # Why-this-matters rationale — "Why We're Doing This" column.
    rationale: str = ""

    # One of GROWTH_PILLARS — "Growth Pillar" column.
    growth_pillar: str = "Other"

    # Existing fields, repurposed.
    urgency: str = "Medium"           # -> "Priority" column
    deadline: Optional[str] = None    # -> "Go Live" column
    sender_or_speaker: Optional[str] = None  # -> "SPOC" column

    # Free-text context attached at extraction time (the email/chunk summary).
    summary: Optional[str] = None

    def __post_init__(self) -> None:
        self.task_heading = (self.task_heading or "").strip()
        self.task_description = (self.task_description or "").strip()
        self.rationale = (self.rationale or "").strip()
        self.urgency = normalise_urgency(self.urgency)
        self.growth_pillar = normalise_growth_pillar(self.growth_pillar)

    @property
    def task(self) -> str:
        """Backward-compatible accessor: heading is what older code called 'task'."""
        return self.task_heading


@dataclass
class EmailExtraction:
    summary: str
    tasks: list[ExtractedTask] = field(default_factory=list)
    is_actionable: bool = False


@dataclass
class MeetingChunkExtraction:
    summary: str
    tasks: list[ExtractedTask] = field(default_factory=list)
    ideas: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    opportunities: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    follow_ups: list[str] = field(default_factory=list)
