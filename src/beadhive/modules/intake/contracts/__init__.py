"""The intake module's public contracts: the consumer port, the outbound ports and the domain
records and refusals they speak (re-exported, as the ``modules/hives`` shape), plus the
``intake.thread`` document codec every ``ReportStore`` implementation shares.
"""

from __future__ import annotations

from ..domain import (
    INTAKE_HOLD_GATE_TYPE,
    INTAKE_HOLD_MARKER,
    INTAKE_HOLD_REASON,
    THREAD_KEY,
    BeadView,
    Blockers,
    Checkpoint,
    CheckpointScope,
    CommentRecord,
    CommentRevision,
    ConsiderRequest,
    DiscoveryOutcome,
    DiscoveryRequest,
    Disposition,
    DispositionOutcome,
    DispositionRequest,
    DuplicatePair,
    EventKind,
    Evidence,
    Flag,
    HiveTriplet,
    HoldFinding,
    HoldFindingKind,
    HoldGate,
    HoldSweep,
    HoldSweepRequest,
    ImportKind,
    ImportOutcome,
    ImportRequest,
    IntakeRefused,
    IntakeStoreError,
    InvestigateOutcome,
    InvestigateRequest,
    IssueBinding,
    IssueStatus,
    LinkKind,
    LinkOutcome,
    LinkRequest,
    OwnIdentity,
    OwnMark,
    PresenceOutcome,
    PresenceRequest,
    QueueItem,
    QueueQuery,
    QueueView,
    RecordState,
    RefusalReason,
    ReportDraft,
    ReportRelations,
    Role,
    SourceSnapshot,
    StorePosture,
    StoreScope,
    Thread,
    ThreadActor,
    ThreadCheckpoint,
    ThreadDocumentError,
    ThreadEvent,
    ThreadIssue,
    ThreadOutcome,
    ThreadPolicy,
    ThreadRequest,
    ThreadState,
    ThreadView,
    WorkDraft,
    WorkProgress,
    blockers_of,
    hold_findings,
    is_hold_gate,
    thread_from_document,
    thread_to_document,
)
from .ports import (
    CheckpointStore,
    IdSource,
    IntakeLock,
    IntakePort,
    LabelPolicy,
    OutboxView,
    ReleaseVerifier,
    ReportStore,
    Sanitizer,
)

__all__ = [
    "INTAKE_HOLD_GATE_TYPE",
    "INTAKE_HOLD_MARKER",
    "INTAKE_HOLD_REASON",
    "Blockers",
    "HoldFinding",
    "HoldFindingKind",
    "HoldGate",
    "HoldSweep",
    "HoldSweepRequest",
    "blockers_of",
    "hold_findings",
    "is_hold_gate",
    "THREAD_KEY",
    "BeadView",
    "Checkpoint",
    "CheckpointScope",
    "CheckpointStore",
    "CommentRecord",
    "CommentRevision",
    "ConsiderRequest",
    "DiscoveryOutcome",
    "DiscoveryRequest",
    "Disposition",
    "DispositionOutcome",
    "DispositionRequest",
    "DuplicatePair",
    "EventKind",
    "Evidence",
    "Flag",
    "HiveTriplet",
    "ImportKind",
    "ImportOutcome",
    "ImportRequest",
    "InvestigateOutcome",
    "InvestigateRequest",
    "IntakeLock",
    "IntakePort",
    "IntakeRefused",
    "IntakeStoreError",
    "IssueBinding",
    "IssueStatus",
    "LabelPolicy",
    "LinkKind",
    "LinkOutcome",
    "LinkRequest",
    "OutboxView",
    "OwnIdentity",
    "OwnMark",
    "PresenceOutcome",
    "PresenceRequest",
    "QueueItem",
    "QueueQuery",
    "QueueView",
    "RecordState",
    "RefusalReason",
    "ReportDraft",
    "ReportRelations",
    "ReportStore",
    "Role",
    "SourceSnapshot",
    "StorePosture",
    "StoreScope",
    "Thread",
    "ThreadActor",
    "ThreadCheckpoint",
    "ThreadDocumentError",
    "ThreadEvent",
    "ThreadIssue",
    "ThreadOutcome",
    "ThreadPolicy",
    "ThreadRequest",
    "ThreadState",
    "ThreadView",
    "WorkDraft",
    "WorkProgress",
    "thread_from_document",
    "thread_to_document",
]

# -- the outbox records and the ``intake.outbox`` codec every ``ReportStore`` shares ---------

from ..domain import (  # noqa: E402
    OUTBOX_KEY,
    LeakFinding,
    LeakKind,
    OpKind,
    OpState,
    OutboxDocument,
    OutboxDocumentError,
    OutboxRecord,
    SanitizerFinding,
    is_grant_id,
    is_outbox_id,
    mint_grant_id,
    mint_outbox_id,
    outbox_from_document,
    outbox_to_document,
)

__all__ += [
    "OUTBOX_KEY",
    "IdSource",
    "LeakFinding",
    "LeakKind",
    "OpKind",
    "OpState",
    "OutboxDocument",
    "OutboxDocumentError",
    "OutboxRecord",
    "Sanitizer",
    "SanitizerFinding",
    "is_grant_id",
    "is_outbox_id",
    "mint_grant_id",
    "mint_outbox_id",
    "outbox_from_document",
    "outbox_to_document",
]

# -- the supervised sender's port records (I9a) -----------------------------------------------

from ..domain import (  # noqa: E402
    CloseBackfill,
    CloseBackfillItem,
    CloseBackfillOutcome,
    OutboxApprove,
    OutboxEntry,
    OutboxListing,
    OutboxQuery,
    OutboxResult,
    RenderedRecord,
    ReplyDraft,
)

__all__ += [
    "CloseBackfill",
    "CloseBackfillItem",
    "CloseBackfillOutcome",
    "OutboxApprove",
    "OutboxEntry",
    "OutboxListing",
    "OutboxQuery",
    "OutboxResult",
    "RenderedRecord",
    "ReplyDraft",
]

# -- the merge and release milestones (I10) ---------------------------------------------------

from ..domain import (  # noqa: E402
    CompletionPolicy,
    MergeFact,
    MilestoneItem,
    MilestoneOutcome,
    MilestonePolicy,
    ReleaseFact,
    Verdict,
)

__all__ += [
    "CompletionPolicy",
    "MergeFact",
    "MilestoneItem",
    "MilestoneOutcome",
    "MilestonePolicy",
    "ReleaseFact",
    "ReleaseVerifier",
    "Verdict",
]

# -- waiting for the reporter (bh-61xkc) -------------------------------------------------------

from ..domain import (  # noqa: E402
    INTAKE_NEEDS_INFO,
    NeedsInfoOutcome,
    NeedsInfoRequest,
    WakeOutcome,
    WakeRequest,
    WakeSweep,
)

__all__ += [
    "INTAKE_NEEDS_INFO",
    "NeedsInfoOutcome",
    "NeedsInfoRequest",
    "WakeOutcome",
    "WakeRequest",
    "WakeSweep",
]

# -- the progress milestones (bh-dev42) --------------------------------------------------------

from ..domain import NoticeFact, StartFact  # noqa: E402

__all__ += ["NoticeFact", "StartFact"]

# -- reproduction work (bh-u3a53): the investigates relation's native edge type ----------------

from ..domain import INVESTIGATES_EDGE, TRACKS_EDGE  # noqa: E402

__all__ += ["INVESTIGATES_EDGE", "TRACKS_EDGE"]

# -- review-gated triage proposals (bh-hrx2r) ---------------------------------------------------

from ..domain import (  # noqa: E402
    PROPOSAL_KEY,
    PROPOSAL_SCHEMA,
    Proposal,
    ProposalDisposition,
    ProposalDocumentError,
    ProposalReview,
    ProposalStatus,
    ProposalTarget,
    ReviewVerdict,
    TargetRole,
    add_review,
    amend,
    mark_applied,
    mark_stale,
    new_proposal,
    proposal_from_document,
    proposal_to_document,
    validate_proposal,
)

__all__ += [
    "PROPOSAL_KEY",
    "PROPOSAL_SCHEMA",
    "Proposal",
    "ProposalDisposition",
    "ProposalDocumentError",
    "ProposalReview",
    "ProposalStatus",
    "ProposalTarget",
    "ReviewVerdict",
    "TargetRole",
    "add_review",
    "amend",
    "mark_applied",
    "mark_stale",
    "new_proposal",
    "proposal_from_document",
    "proposal_to_document",
    "validate_proposal",
]

# -- the deterministic pre-apply checks and the repository facts they read (bh-hrx2r) ---------

from ..domain import (  # noqa: E402
    DEFAULT_REPORTER_TEXT_LIMIT,
    CheckContext,
    CheckFinding,
    CheckKind,
    LeakPolicy,
    ProposalCheck,
    TargetFact,
    adopt_computed_fix,
    check_proposal,
    check_run,
    cited_paths,
    version_key,
)
from .ports import RepoFacts  # noqa: E402

__all__ += [
    "DEFAULT_REPORTER_TEXT_LIMIT",
    "CheckContext",
    "CheckFinding",
    "CheckKind",
    "LeakPolicy",
    "ProposalCheck",
    "TargetFact",
    "adopt_computed_fix",
    "check_proposal",
    "check_run",
    "cited_paths",
    "version_key",
    "RepoFacts",
]

# -- review packets (bh-hrx2r) -------------------------------------------------------------------

from ..domain import (  # noqa: E402
    COMMENTS_ENABLED_KEY,
    COMMENTS_READ_KEY,
    DEFAULT_BATCH_SIZE,
    Cluster,
    ClusterBatch,
    ClusterItem,
    ClusterLink,
    ClusterPolicy,
    ClusterSignal,
    IndexEntry,
    PassMetrics,
    ReviewBatch,
    ReviewMetrics,
    ReviewPacket,
    ReviewPolicy,
    ReviewRequest,
    ReviewRun,
)

__all__ += [
    "COMMENTS_ENABLED_KEY",
    "COMMENTS_READ_KEY",
    "DEFAULT_BATCH_SIZE",
    "Cluster",
    "ClusterBatch",
    "ClusterItem",
    "ClusterLink",
    "ClusterPolicy",
    "ClusterSignal",
    "IndexEntry",
    "PassMetrics",
    "ReviewBatch",
    "ReviewMetrics",
    "ReviewPacket",
    "ReviewPolicy",
    "ReviewRequest",
    "ReviewRun",
]

# -- the label projection (bh-uim9i) ------------------------------------------------------------

from ..domain import (  # noqa: E402
    LabelBatch,
    LabelBatchApprove,
    LabelBatchItem,
    LabelBatchOutcome,
    LabelDefineOutcome,
    LabelDefinePlan,
    LabelDefineRequest,
    LabelSyncItem,
    LabelSyncOutcome,
    LabelSyncRequest,
)
from .ports import AuditLog  # noqa: E402

__all__ += [
    "AuditLog",
    "LabelBatch",
    "LabelBatchApprove",
    "LabelBatchItem",
    "LabelBatchOutcome",
    "LabelDefineOutcome",
    "LabelDefinePlan",
    "LabelDefineRequest",
    "LabelSyncItem",
    "LabelSyncOutcome",
    "LabelSyncRequest",
]

# -- propose, review and apply-reviewed (bh-9aqmv) ---------------------------------------------

from ..domain import (  # noqa: E402
    ANALYST_FIELDS,
    REOPENABLE,
    VERDICT_FIELDS,
    ApplyItem,
    ApplyOutcome,
    ApplyPlan,
    ApplyRequest,
    ApplyStatus,
    ApplyStep,
    ProposalReviewOutcome,
    ProposalReviewRequest,
    ProposeKind,
    ProposeOutcome,
    ProposeRequest,
    VerdictInput,
    apply_order,
    check_rewrite,
    content_from_document,
    may_reopen,
    new_round,
    plan_apply,
    verdict_from_document,
    verification_gap,
)
from .ports import LandedCommits  # noqa: E402

__all__ += [
    "ANALYST_FIELDS",
    "REOPENABLE",
    "VERDICT_FIELDS",
    "ApplyItem",
    "ApplyOutcome",
    "ApplyPlan",
    "ApplyRequest",
    "ApplyStatus",
    "ApplyStep",
    "LandedCommits",
    "ProposalReviewOutcome",
    "ProposalReviewRequest",
    "ProposeKind",
    "ProposeOutcome",
    "ProposeRequest",
    "VerdictInput",
    "apply_order",
    "check_rewrite",
    "content_from_document",
    "may_reopen",
    "new_round",
    "plan_apply",
    "verdict_from_document",
    "verification_gap",
]

# -- what Beads stores, which every ``ReportStore`` (and its memory fake) honours -------------

from ..domain import CLOSE_REASON_LIMIT  # noqa: E402

__all__ += ["CLOSE_REASON_LIMIT"]
