from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


Identifier = Annotated[str, StringConstraints(min_length=1, max_length=64)]
Revision = Annotated[int, Field(ge=1, strict=True)]
FaultCode = Annotated[str, StringConstraints(pattern=r"^F(0[1-9]|1[0-4])$")]
Score = Annotated[float, Field(ge=0, le=1)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
Risk = Literal["low", "medium", "high", "unassessed"]
Urgency = Literal["low", "medium", "high"]
Origin = Literal["fixture", "team"]
SectionKind = Literal["signs", "causes", "checks", "work", "clarify", "risk"]


class Pair(BaseModel):
    vehicle_id: Identifier
    session_id: Identifier

    def key(self) -> tuple[str, str]:
        return self.vehicle_id, self.session_id


class Diagnosis(Pair):
    status: Literal["normal", "anomaly", "check_failed"]
    anomaly_type: FaultCode | None
    anomaly_title: str | None
    anomaly_score: Score | None
    confidence: Score | None
    signal_quality: Literal["good", "warning", "bad"]
    description: Text
    model_version: Text


class Reference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manual: Text
    pdf_page: Annotated[int, Field(ge=1, strict=True)]


class DraftEdits(BaseModel):
    conclusion: Text
    urgency: Urgency
    operations: list[Text] = Field(min_length=1, max_length=30)
    spare_parts: list[Text] = Field(max_length=30)


class DraftFields(DraftEdits):
    references: list[Reference] = Field(min_length=1, max_length=20)


class TicketRequest(Pair, DraftFields):
    anomaly_type: FaultCode


class TicketReceipt(BaseModel):
    ticket_id: Identifier
    status: Literal["created"]


class DraftUpdate(Pair):
    draft_id: Identifier
    revision: Revision
    edits: DraftEdits


class TicketSubmit(DraftUpdate):
    priority_confirmed: Annotated[bool, Field(strict=True)]


class TicketReconciliation(Pair):
    revision: Revision
    ticket_id: Annotated[str, StringConstraints(strip_whitespace=True, max_length=40, pattern=r"^TKT-[0-9]{4,}$")]
    verified: Annotated[bool, Field(strict=True)]


class ChatRequest(Pair):
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1600)]


class Section(BaseModel):
    kind: SectionKind
    items: list[Text] = Field(min_length=1, max_length=24)
    references: list[Reference] = Field(default_factory=list, max_length=20)


class Reply(BaseModel):
    analysis: dict | None = None
    summary: Text
    sections: list[Section] = Field(default_factory=list, max_length=12)


class Brief(BaseModel):
    risk: Risk
    rationale: Text
    first_check: Text
    references: list[Reference] = Field(default_factory=list, max_length=20)


class ToolCall(BaseModel):
    tool: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")]
    status: Literal["ok", "error", "timeout", "unknown", "rejected"]
    duration_ms: int = Field(ge=0)
    model: Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9_./:@-]{1,100}$")] | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
