"""
Pydantic v2 schemas — all API data objects per backend_service_specification-v1.0.md
"""
from __future__ import annotations

import json
from typing import Any, Generic, Literal, Optional, TypeAlias, TypeVar

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator
from observability import get_request_id

T = TypeVar("T")

Engine: TypeAlias = Literal["legacy", "lightrag"]
LightRAGMode: TypeAlias = Literal["local", "global", "hybrid", "mix", "naive"]


# ---------------------------------------------------------------------------
# Universal response envelope
# ---------------------------------------------------------------------------

class APIResponse(BaseModel, Generic[T]):
    code: int = 0
    msg: str = "success"
    request_id: str = Field(default_factory=get_request_id)
    data: Optional[T] = None

    @classmethod
    def ok(cls, data: Any = None) -> "APIResponse":
        return cls(code=0, msg="success", data=data)

    @classmethod
    def err(cls, code: int, msg: str) -> "APIResponse":
        return cls(code=code, msg=msg, data=None)


# ---------------------------------------------------------------------------
# A. Document schemas
# ---------------------------------------------------------------------------

class EngineIndexStatus(BaseModel):
    status: str = "pending"
    job_id: Optional[str] = None
    error: Optional[str] = None
    stats: dict[str, Any] = Field(default_factory=dict)


class DocumentInfo(BaseModel):
    doc_id: str
    filename: str
    format: str
    size_bytes: int
    pages: Optional[int] = None
    uploaded_at: str
    upload_date: Optional[str] = None
    status: str  # uploaded | indexed | failed
    language: str = "ch"
    enable_formula: bool = True
    enable_table: bool = True
    indexes: dict[str, EngineIndexStatus] = Field(default_factory=dict)
    available_engines: list[Engine] = Field(default_factory=list)


class DocumentListData(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[DocumentInfo]


class DeleteDocumentData(BaseModel):
    deleted: bool
    doc_id: str
    removed_nodes: int
    removed_edges: int


# ---------------------------------------------------------------------------
# B. Indexing job schemas
# ---------------------------------------------------------------------------

class IndexingProgress(BaseModel):
    parsed_pages: int = 0
    total_pages: int = 0
    extracted_entities: int = 0


class IndexingJobStatus(BaseModel):
    job_id: str
    doc_id: str
    status: str  # submitted|queued|parsing|extracting|indexing|done|failed|cancelled
    stage: str = ""
    progress: IndexingProgress = Field(default_factory=IndexingProgress)
    created_at: str
    elapsed_seconds: float = 0.0
    error: Optional[str] = None
    engines: dict[str, EngineIndexStatus] = Field(default_factory=dict)


class StartIndexRequest(BaseModel):
    doc_id: str
    engine: Optional[Engine] = None


class RetryIndexRequest(BaseModel):
    engine: Engine


class CancelJobData(BaseModel):
    cancelled: bool
    job_id: str
    previous_status: str


# ---------------------------------------------------------------------------
# C. KG schemas
# ---------------------------------------------------------------------------

class KGNode(BaseModel):
    id: str
    name: str
    type: str
    source_doc: str
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    confidence: Optional[str] = None
    page: int = 0
    pages: list[int] = Field(default_factory=list)
    degree: int = 0
    description: Optional[str] = None
    engine: Engine = "legacy"


class KGNodeDetail(KGNode):
    degree_centrality: float = 0.0
    neighbor_count: int = 0


class KGEdge(BaseModel):
    id: Optional[str] = None
    source: str
    target: str
    relation: str = "CO_OCCURS_IN"
    doc_id: str
    page: int = 0
    pages: list[int] = Field(default_factory=list)
    description: Optional[str] = None
    weight: float = 1.0
    engine: Engine = "legacy"


class KGNodeListData(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[KGNode]


class KGEdgeListData(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[KGEdge]


class KGStatsData(BaseModel):
    total_nodes: int
    total_edges: int
    density: float
    type_distribution: dict[str, int]
    relation_types: dict[str, int]
    top5_central_nodes: list[dict]
    source_documents: list[str]


class KGExportData(BaseModel):
    format: str
    doc_id: Optional[str]
    total_nodes: int
    total_edges: int
    exported_at: str
    nodes: list[KGNode]
    edges: list[KGEdge]


class NeighborInfo(BaseModel):
    id: str
    name: str
    type: str
    page: int


class NeighborsData(BaseModel):
    center: NeighborInfo
    hops: int
    neighbors_by_hop: dict[str, list[NeighborInfo]]
    total_neighbors: int


# ---------------------------------------------------------------------------
# D. QA schemas
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    role: str  # human | ai
    content: str


class QueryRequest(BaseModel):
    question: str
    history: list[ChatMessage] = Field(default_factory=list)
    session_id: Optional[str] = None
    engine: Engine = "legacy"
    retrieval_mode: Optional[LightRAGMode] = None
    document_ids: Optional[list[str]] = Field(default=None, max_length=1000)


class QuerySessionCreateRequest(BaseModel):
    engine: Engine = "lightrag"
    retrieval_mode: Optional[LightRAGMode] = "mix"


class ToolCallRecord(BaseModel):
    """Public tool-call shape used by the service and frontend.

    The first API specification used ``tool/input/output``.  Validation aliases
    keep persisted records and older internal callers readable while responses
    serialize with the current ``tool_name/tool_input/tool_output`` contract.
    """

    step: int = 0
    tool_name: str = Field(validation_alias=AliasChoices("tool_name", "tool"))
    tool_input: str = Field(default="", validation_alias=AliasChoices("tool_input", "input"))
    tool_output: str = Field(default="", validation_alias=AliasChoices("tool_output", "output"))

    @field_validator("tool_input", "tool_output", mode="before")
    @classmethod
    def stringify_tool_payload(cls, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, ensure_ascii=False, default=str)
        return str(value)

    @property
    def tool(self) -> str:
        return self.tool_name

    @property
    def input(self) -> str:
        return self.tool_input

    @property
    def output(self) -> str:
        return self.tool_output


class QueryReference(BaseModel):
    doc_id: str
    filename: str
    page: Optional[int] = None
    chunk_id: str
    excerpt: str


class CitedEntityRecord(BaseModel):
    id: str
    name: str
    type: str


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="allow")

    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    model: Optional[str] = None
    provider: Optional[str] = None


class QAResult(BaseModel):
    """Successful query result, matching ``qa_service.run_query`` output.

    ``query_id``, ``elapsed_seconds`` and ``created_at`` remain accepted as
    validation aliases for records written against the original specification.
    """

    id: str = Field(validation_alias=AliasChoices("id", "query_id"))
    session_id: Optional[str] = None
    question: str
    answer: str
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    cited_nodes: list[str] = Field(default_factory=list)
    cited_chunks: list[str] = Field(default_factory=list)
    duration_seconds: float = Field(
        validation_alias=AliasChoices("duration_seconds", "elapsed_seconds")
    )
    timestamp: str = Field(validation_alias=AliasChoices("timestamp", "created_at"))
    session: Optional[dict[str, Any]] = None
    engine: Engine = "legacy"
    retrieval_mode: Optional[LightRAGMode] = None
    references: list[QueryReference] = Field(default_factory=list)
    cited_entities: list[str | CitedEntityRecord] = Field(default_factory=list)
    model: Optional[str] = None
    provider: Optional[str] = None
    usage: TokenUsage = Field(default_factory=TokenUsage)

    @property
    def query_id(self) -> str:
        return self.id

    @property
    def elapsed_seconds(self) -> float:
        return self.duration_seconds

    @property
    def created_at(self) -> str:
        return self.timestamp


class ChatSessionMessage(BaseModel):
    id: str
    role: Literal["human", "ai"]
    content: str
    timestamp: str
    query_id: Optional[str] = None
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    cited_nodes: list[str] = Field(default_factory=list)
    cited_chunks: list[str] = Field(default_factory=list)
    cited_entities: list[str | CitedEntityRecord] = Field(default_factory=list)
    references: list[QueryReference] = Field(default_factory=list)
    engine: Optional[Engine] = None
    retrieval_mode: Optional[LightRAGMode] = None
    model: Optional[str] = None
    provider: Optional[str] = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    duration_seconds: Optional[float] = None


class ChatSessionSummaryData(BaseModel):
    id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int
    last_question: str
    last_answer: str
    engine: Engine = "legacy"
    retrieval_mode: Optional[LightRAGMode] = None


class ChatSessionData(ChatSessionSummaryData):
    messages: list[ChatSessionMessage] = Field(default_factory=list)


class ChatSessionListData(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[ChatSessionSummaryData]


class QAHistoryData(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[QAResult]


class BatchQueryRequest(BaseModel):
    questions: list[str] = Field(..., max_length=20)
    engine: Engine = "legacy"
    retrieval_mode: Optional[LightRAGMode] = None
    document_ids: Optional[list[str]] = Field(default=None, max_length=1000)


class BatchQueryData(BaseModel):
    batch_id: str
    total: int
    status: Literal["submitted", "running", "done", "cancelled"]
    created_at: str
    engine: Engine = "legacy"
    retrieval_mode: Optional[LightRAGMode] = None


class BatchItemResult(BaseModel):
    id: Optional[str] = Field(default=None, validation_alias=AliasChoices("id", "query_id"))
    question: str
    answer: Optional[str] = None
    error: Optional[str] = None
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    cited_nodes: list[str] = Field(default_factory=list)
    cited_chunks: list[str] = Field(default_factory=list)
    duration_seconds: Optional[float] = Field(
        default=None,
        validation_alias=AliasChoices("duration_seconds", "elapsed_seconds"),
    )
    timestamp: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("timestamp", "created_at"),
    )
    engine: Optional[Engine] = None
    retrieval_mode: Optional[LightRAGMode] = None
    references: list[QueryReference] = Field(default_factory=list)
    cited_entities: list[str | CitedEntityRecord] = Field(default_factory=list)
    model: Optional[str] = None
    provider: Optional[str] = None
    usage: TokenUsage = Field(default_factory=TokenUsage)


class BatchResultData(BaseModel):
    batch_id: str
    total: int
    completed: int
    failed: int
    status: Literal["submitted", "running", "done", "cancelled"]
    created_at: str = ""
    updated_at: Optional[str] = None
    cancel_requested: bool = False
    results: list[BatchItemResult] = Field(default_factory=list)
    engine: Engine = "legacy"
    retrieval_mode: Optional[LightRAGMode] = None


class BatchSummaryData(BaseModel):
    batch_id: str
    total: int
    completed: int
    failed: int
    status: Literal["submitted", "running", "done", "cancelled"]
    created_at: str
    updated_at: Optional[str] = None
    cancel_requested: bool = False
    engine: Engine = "legacy"
    retrieval_mode: Optional[LightRAGMode] = None


class BatchListData(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[BatchSummaryData]


class CancelBatchData(BaseModel):
    batch_id: str
    previous_status: str
    status: Literal["submitted", "running", "done", "cancelled"]
    cancel_requested: bool


# ---------------------------------------------------------------------------
# E. Search schemas
# ---------------------------------------------------------------------------

class EntitySearchData(BaseModel):
    query: str
    total: int
    items: list[KGNode]


class PathNode(BaseModel):
    id: str
    name: str
    type: str


class PathEdge(BaseModel):
    source: str
    target: str
    relation: str


class PathInfo(BaseModel):
    length: int
    nodes: list[PathNode]
    edges: list[PathEdge]


class PathSearchData(BaseModel):
    from_node: PathNode = Field(alias="from")
    to_node: PathNode = Field(alias="to")
    max_hops: int
    paths: list[PathInfo]
    total_paths: int

    model_config = {"populate_by_name": True}


class GraphSearchData(BaseModel):
    query: str
    matched_nodes: list[KGNode]
    subgraph_edges: list[KGEdge]


# ---------------------------------------------------------------------------
# F. System schemas
# ---------------------------------------------------------------------------

class ComponentHealth(BaseModel):
    status: str  # ok | error
    path: Optional[str] = None
    exists: Optional[bool] = None
    base_url: Optional[str] = None
    key_configured: Optional[bool] = None
    kg_nodes_exists: Optional[bool] = None
    kg_edges_exists: Optional[bool] = None
    uploads_dir_exists: Optional[bool] = None
    mode: Optional[str] = None
    data_dir: Optional[str] = None
    persistence: Optional[str] = None
    persistent: Optional[bool] = None
    warning: Optional[str] = None
    version: Optional[str] = None
    detail: Optional[str] = None
    configured: Optional[bool] = None
    queue_depth: Optional[int] = None


class HealthData(BaseModel):
    status: str
    version: str
    uptime_seconds: float
    components: dict[str, ComponentHealth]


class SystemStatsData(BaseModel):
    total_documents: int
    indexed_documents: int
    failed_documents: int
    total_nodes: int
    total_edges: int
    type_distribution: dict[str, int]
    total_queries: int
    active_jobs: int
    storage_used_mb: float


class FormatInfo(BaseModel):
    ext: str
    description: str
    max_size_mb: int
    max_pages: int
    requires_ocr: bool


class FormatsData(BaseModel):
    formats: list[FormatInfo]
    ocr_languages: list[dict]
    notes: list[str]


class DemoData(BaseModel):
    nodes: list[KGNode]
    edges: list[KGEdge]
    stats: dict


# ---------------------------------------------------------------------------
# B3 index result
# ---------------------------------------------------------------------------

class IndexResultStats(BaseModel):
    blocks: int = 0
    block_types: dict[str, int] = Field(default_factory=dict)
    pages: int = 0
    raw_extractions: int = 0
    nodes: int = 0
    edges: int = 0
    type_counts: dict[str, int] = Field(default_factory=dict)
    alignment_counts: dict[str, int] = Field(default_factory=dict)
    elapsed_seconds: float = 0.0


class ExtractionRecord(BaseModel):
    text: str
    type: str
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    alignment: Optional[str] = None
    page: int = 0
    doc_id: str


class IndexResultData(BaseModel):
    job_id: str
    doc_id: str
    status: str
    stats: Optional[IndexResultStats] = None
    extractions: Optional[list[ExtractionRecord]] = None
    nodes: Optional[list[KGNode]] = None
    edges: Optional[list[KGEdge]] = None
