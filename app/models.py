"""Pydantic schemas for state and handoffs. Simplified versions of the design spec."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel


# ---------- News + Triage ----------
class NewsItem(BaseModel):
    id: str
    headline: str
    body: str
    tickers: list[str]
    source: str
    published_at: str  # iso


class NewsTriageResult(BaseModel):
    news_id: str
    decision: Literal["act", "watch", "ignore"]
    materiality_score: float
    primary_dimension: str
    impacted_tickers: list[str]
    is_duplicate: bool = False
    reasoning: str
    policy_sections_cited: list[str] = []
    retrieved_evidence: list[dict] = []
    recommended_next_agent: Optional[str] = None


# ---------- Fundamental ----------
class FundamentalRead(BaseModel):
    ticker: str
    invocation_mode: str
    as_of: str
    business_quality: dict
    management: dict
    valuation: dict
    catalysts: dict
    risks: dict
    thesis_strength: Optional[str] = None
    thesis_intact: Optional[str] = None
    recommended_action: str
    reasoning_narrative: str
    sources_referenced: list[dict] = []
    policy_sections_cited: list[str] = []


# ---------- Plan ----------
class TradingPlan(BaseModel):
    id: str
    ticker: str
    created_at: str
    status: Literal["draft", "pending_hitl", "active", "paused", "closed", "rejected"] = "draft"
    thesis: dict
    entry: dict
    monitoring: dict
    guardrails: dict
    exit: dict
    history: list[dict] = []
    past_similar_plan_refs: list[str] = []
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    rejection_reason: Optional[str] = None


class PlanDraftResult(BaseModel):
    status: Literal["drafted", "not_eligible", "conflict_detected", "insufficient_data"]
    plan_id: Optional[str] = None
    plan: Optional[TradingPlan] = None
    reasoning_narrative: str = ""
    precedent_summary: str = ""
    pre_check_results: dict = {}
    policy_sections_cited: list[str] = []
    sources_referenced: list[dict] = []
    next_step: str = ""


# ---------- Risk ----------
class RiskDecision(BaseModel):
    proposal_id: str
    proposal_type: str
    verdict: Literal["approve", "approve_with_modification", "defer_to_hitl", "reject"]
    modification: Optional[dict] = None
    policy_checks: list[dict] = []
    simulate_order_result: dict = {}
    recommended_routing: str
    reasoning_narrative: str
    policy_sections_cited: list[str] = []
    sources_referenced: list[dict] = []


# ---------- Audit ----------
class AuditNote(BaseModel):
    about_journal_id: str
    audited_agent: str
    compliant: bool
    findings: list[dict] = []
    overall_severity: Literal["info", "low", "medium", "high"]
    recommended_action: str
    reasoning_narrative: str
    policy_sections_cited: list[str] = []


# ---------- Graph state ----------
class RunState(BaseModel):
    run_id: str
    trigger_type: str
    trigger_meta: dict
    as_of: str
    ticker: Optional[str] = None
    news_item: Optional[NewsItem] = None
    triage: Optional[NewsTriageResult] = None
    fundamental: Optional[FundamentalRead] = None
    plan_draft: Optional[PlanDraftResult] = None
    risk: Optional[RiskDecision] = None
    audits: list[AuditNote] = []
    final_status: Optional[str] = None
    errors: list[str] = []

    def model_dump_json_safe(self) -> dict:
        return self.model_dump(mode="json")
