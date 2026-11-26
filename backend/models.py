from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints, model_validator


Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=80)]
Fault = Annotated[str, StringConstraints(pattern=r"^F(0[1-9]|1[0-4])$")]
Urgency = Literal["low", "medium", "high"]
Score = Annotated[float, Field(strict=True, ge=0, le=1)]
Page = Annotated[int, Field(strict=True, ge=1)]
Revision = Annotated[int, Field(strict=True, ge=1)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Pair(Input):
    vehicle_id: Identifier
    session_id: Identifier


class Session(BaseModel):
    session_id: Identifier
    checked_at: Text


class Vehicle(BaseModel):
    vehicle_id: Identifier
    make: Text
    model: Text | None
    engine_model: Text


class Diagnosis(BaseModel):
    vehicle_id: Identifier
    session_id: Identifier
    status: Literal["normal", "anomaly", "check_failed"]
    anomaly_type: Fault | None
    anomaly_title: Text | None
    anomaly_score: Score | None
    confidence: Score | None
    signal_quality: Literal["good", "warning", "bad"]
    description: Text
    model_version: Text

    @model_validator(mode="after")
    def consistent_status(self):
        if (self.anomaly_type is not None) != (self.status == "anomaly"):
            raise ValueError("anomaly_type must be present only for an anomaly")
        if (self.anomaly_title is not None) != (self.status == "anomaly"):
            raise ValueError("anomaly_title must be present only for an anomaly")
        if (self.anomaly_score is None or self.confidence is None) != (self.status == "check_failed"):
            raise ValueError("scores are required for a completed check")
        if self.status == "check_failed" and (self.anomaly_score is not None or self.confidence is not None):
            raise ValueError("both scores must be null for a failed check")
        if (self.signal_quality == "bad") != (self.status == "check_failed"):
            raise ValueError("bad signal quality requires a failed check")
        return self


class Reference(Input):
    manual: Text
    pdf_page: Page


class Source(Reference):
    fragment_id: str
    url: str | None = None


class Evidence(BaseModel):
    section: str
    text: str
    source: Source


def empty_list():
    return Field(default_factory=list)


class Assessment(BaseModel):
    conclusion: str | None
    next_step: str
    risk: str | None = None
    possible_causes: list[str] = empty_list()
    checks: list[str] = empty_list()
    repair_actions: list[str] = empty_list()
    evidence: list[Evidence] = empty_list()


class DiagnosticResult(BaseModel):
    engine_model: str
    diagnosis: Diagnosis
    assessment: Assessment


Question = Annotated[Text, Field(max_length=2000)]


class ChatTurn(Input):
    question: Question
    answer: Annotated[Text, Field(max_length=2000)]


class ChatRequest(Pair):
    question: Question
    history: Annotated[list[ChatTurn], Field(max_length=6)] = empty_list()


class ChatReply(BaseModel):
    answer: str
    needs_clarification: bool = False
    evidence: list[Evidence] = empty_list()
    diagnostic_result: DiagnosticResult | None = None
    draft: "DraftView | None" = None


class DraftFields(Input):
    conclusion: Text | None = None
    urgency: Urgency | None = None
    operations: list[Text] = empty_list()
    spare_parts: list[Text] = empty_list()
    references: list[Reference] = empty_list()


class DraftPatch(DraftFields):
    revision: Revision

    @model_validator(mode="after")
    def valid_patch(self):
        if self.model_fields_set == {"revision"}:
            raise ValueError("supply at least one draft field to edit")
        return self


class Confirm(Input):
    revision: Revision
    urgency: Urgency
    confirmed: StrictBool


class Reconcile(Input):
    revision: Revision
    ticket_id: Annotated[str, StringConstraints(
        strip_whitespace=True, max_length=40, pattern=r"^TKT-[0-9]{4,}$",
    )]
    verified: StrictBool


class Ticket(Pair):
    anomaly_type: Fault
    conclusion: Text
    urgency: Urgency
    operations: Annotated[list[Text], Field(min_length=1)]
    spare_parts: list[Text]
    references: Annotated[list[Reference], Field(min_length=1)]


class TicketReceipt(BaseModel):
    ticket_id: Text
    status: Literal["created"]


class DeliveryError(BaseModel):
    code: str
    message: str


class DraftView(DraftFields, Pair):
    draft_id: str
    revision: Revision
    anomaly_type: Fault
    state: Literal["draft", "sending", "sent", "delivery_failed", "delivery_unknown"]
    ticket_id: str | None = None
    delivery_error: DeliveryError | None = None
