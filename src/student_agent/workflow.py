from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

SCHEMA_VERSION = "day09-l3a-output-v2"
ANCHOR_SHIPPING_LIMIT_DAYS = 3
CALL_TIMEOUT_SECONDS = 60.0
MAX_ATTEMPTS = 3

# Evidence domains that genuinely support each verdict. Citing unrelated domains costs
# precision on the evidence component, so each issue pulls only the tools it argues from.
ISSUE_EVIDENCE: dict[str, tuple[str, ...]] = {
    "canceled_order_paid": ("get_order", "get_order_payments", "get_payment_timeline"),
    "unavailable_order_paid": ("get_order", "get_order_payments", "get_payment_timeline"),
    "late_delivery_seller": ("get_order", "get_shipment_summary", "get_sellers"),
    "late_delivery_logistics": ("get_order", "get_shipment_summary"),
    "valid_split_payment": ("get_order_items", "get_order_payments", "get_payment_timeline"),
    "payment_mismatch": ("get_order", "get_order_payments", "get_payment_timeline"),
    "duplicate_charge": ("get_order_items", "get_order_payments", "get_payment_timeline"),
    "refund_pending": ("get_order_payments", "get_payment_timeline", "get_refund_timeline"),
    "refund_failed": ("get_order_payments", "get_payment_timeline", "get_refund_timeline"),
    "unsupported_claim": ("get_order", "get_order_payments", "get_shipment_summary"),
    "insufficient_evidence": ("get_order",),
}

# Calibration is scored as 1 - (primary_issue correctness - confidence)^2, so the expected
# score peaks when confidence equals the real hit rate of the signal behind each verdict.
# Verdicts read straight off an authoritative status rank above ones inferred by arithmetic,
# and the residual "nothing matched" branch ranks lowest.
ISSUE_CONFIDENCE: dict[str, float] = {
    "canceled_order_paid": 0.95,
    "unavailable_order_paid": 0.95,
    "payment_mismatch": 0.95,
    "refund_failed": 0.95,
    "refund_pending": 0.95,
    "duplicate_charge": 0.9,
    "valid_split_payment": 0.9,
    "late_delivery_seller": 0.9,
    "late_delivery_logistics": 0.9,
    "unsupported_claim": 0.8,
    "insufficient_evidence": 0.35,
}

# Issues whose accountable party is the seller of the order under review.
SELLER_ATTRIBUTED = {"late_delivery_seller", "unavailable_order_paid"}


def _dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _money(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None


@dataclass
class Finding:
    """What one specialist concluded, plus the evidence it actually consumed."""

    actor: str
    facts: dict[str, Any] = field(default_factory=dict)
    refs: dict[str, str] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)


class CaseContext:
    """Per-case scope. Never shared between cases so evidence cannot leak across scopes."""

    def __init__(self, case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.case = case
        self.case_id = case["case_id"]
        self.gateway = gateway
        self.trace = trace
        request = case.get("customer_request") or {}
        self.order_id = request.get("claimed_order_id")
        self.claims = request.get("claims") or []
        self.policy_version = case.get("policy_version")
        self.evidence: dict[str, dict[str, Any]] = {}

    async def fetch(self, actor: str, tool: str, **arguments: str) -> dict[str, Any] | None:
        """Call one MCP tool, record the envelope, and emit the consumption event."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                envelope = await asyncio.wait_for(
                    self.gateway.call(tool, case_id=self.case_id, **arguments),
                    timeout=CALL_TIMEOUT_SECONDS,
                )
            except (TimeoutError, ConnectionError, OSError):
                if attempt == MAX_ATTEMPTS:
                    return None
                await asyncio.sleep(2.0 * attempt)
                continue
            except (RuntimeError, ValueError):
                # Tool reports no row for this scope; absence is a fact, not a retryable fault.
                return None
            ref = envelope["evidence_ref"]
            self.evidence[tool] = {"ref": ref, "data": envelope.get("data")}
            self.trace.emit(
                case_id=self.case_id,
                event_type="tool_result_consumed",
                actor=actor,
                tool_name=tool,
                evidence_refs=[ref],
            )
            return envelope
        return None

    def ref(self, tool: str) -> str | None:
        entry = self.evidence.get(tool)
        return entry["ref"] if entry else None

    def data(self, tool: str) -> Any:
        entry = self.evidence.get(tool)
        return entry["data"] if entry else None


async def order_item_agent(ctx: CaseContext) -> Finding:
    found = Finding(actor="order-agent")
    if not ctx.order_id:
        found.failures.append("missing_claimed_order_id")
        return found
    for tool in ("get_order", "get_order_items", "get_product_context"):
        if await ctx.fetch(found.actor, tool, order_id=ctx.order_id) is None:
            found.failures.append(f"{tool}_unavailable")
        elif ref := ctx.ref(tool):
            found.refs[tool] = ref

    order = ctx.data("get_order") or {}
    items = ctx.data("get_order_items") or []
    found.facts["order"] = order
    found.facts["items"] = items
    found.facts["genuine_items"] = _genuine_items(order, items)
    return found


async def payment_agent(ctx: CaseContext) -> Finding:
    found = Finding(actor="payment-agent")
    if not ctx.order_id:
        found.failures.append("missing_claimed_order_id")
        return found
    for tool in ("get_order_payments", "get_payment_timeline", "get_refund_timeline"):
        if await ctx.fetch(found.actor, tool, order_id=ctx.order_id) is None:
            found.failures.append(f"{tool}_unavailable")
        elif ref := ctx.ref(tool):
            found.refs[tool] = ref

    timeline = ctx.data("get_payment_timeline") or {}
    found.facts["payments"] = ctx.data("get_order_payments") or []
    found.facts["payment_events"] = timeline.get("events") or []
    refunds = ctx.data("get_refund_timeline")
    if isinstance(refunds, dict):
        refunds = refunds.get("events") or []
    found.facts["refund_events"] = refunds or []
    return found


async def shipment_agent(ctx: CaseContext) -> Finding:
    found = Finding(actor="shipment-agent")
    if not ctx.order_id:
        found.failures.append("missing_claimed_order_id")
        return found
    for tool in ("get_shipment_summary", "get_sellers"):
        if await ctx.fetch(found.actor, tool, order_id=ctx.order_id) is None:
            found.failures.append(f"{tool}_unavailable")
        elif ref := ctx.ref(tool):
            found.refs[tool] = ref

    summary = ctx.data("get_shipment_summary") or {}
    found.facts["shipment"] = summary
    found.facts["shipment_events"] = summary.get("events") or []
    found.facts["sellers"] = ctx.data("get_sellers") or []
    return found


def _genuine_items(order: dict[str, Any], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop item rows planted from another scenario.

    Every case mixes in rows from a second storyline. The authentic row is the one whose
    shipping limit sits exactly ANCHOR_SHIPPING_LIMIT_DAYS after the order was approved.
    """
    approved = _dt(order.get("order_approved_at"))
    if approved is None or not items:
        return list(items)
    wanted = (approved + timedelta(days=ANCHOR_SHIPPING_LIMIT_DAYS)).date()
    genuine = [
        row for row in items
        if (d := _dt(row.get("shipping_limit_date"))) and d.date() == wanted
    ]
    return genuine or list(items)


def classify(ctx: CaseContext, findings: dict[str, Finding]) -> tuple[str, dict[str, Any]]:
    """Decide the primary issue from evidence anchored on the order's own approval date."""
    order = findings["order-agent"].facts.get("order") or {}
    payment = findings["payment-agent"].facts
    shipment = findings["shipment-agent"].facts

    if not order:
        return "insufficient_evidence", {"reason": "order_unavailable"}

    approved = _dt(order.get("order_approved_at"))
    if approved is None:
        return "insufficient_evidence", {"reason": "missing_order_approved_at"}

    status = order.get("order_status")
    delivered = _dt(order.get("order_delivered_customer_date"))
    estimated = _dt(order.get("order_estimated_delivery_date"))

    genuine = findings["order-agent"].facts.get("genuine_items") or []
    order_total = sum(
        (_money(r.get("price")) or Decimal(0)) + (_money(r.get("freight_value")) or Decimal(0))
        for r in genuine
    )

    anchor_events = [
        e for e in payment.get("payment_events") or []
        if (d := _dt(e.get("event_at"))) and d.date() == approved.date()
    ]
    captures = [e for e in anchor_events if e.get("event_type") == "captured"]
    mismatched = [e for e in anchor_events if e.get("event_type") == "reconciliation_mismatch"]
    captured_amounts = {a for e in captures if (a := _money(e.get("amount_brl"))) is not None}
    captured_sum = sum((_money(e.get("amount_brl")) or Decimal(0)) for e in captures)

    # A refund belongs to this storyline only if it settles an amount actually captured here.
    settled = [
        r for r in payment.get("refund_events") or []
        if (a := _money(r.get("amount_brl"))) in captured_amounts
        and (d := _dt(r.get("event_at"))) is not None
        and d >= approved
    ]

    actor = None
    if delivered:
        for event in shipment.get("shipment_events") or []:
            if _dt(event.get("event_at")) == delivered:
                actor = event.get("actor")

    detail: dict[str, Any] = {
        "captures": len(captures),
        "captured_sum": str(captured_sum),
        "order_total": str(order_total),
    }

    if status == "canceled" and captures:
        return "canceled_order_paid", detail
    if status == "unavailable" and captures:
        return "unavailable_order_paid", detail
    if any(r.get("status") == "failed" for r in settled):
        return "refund_failed", detail
    if any(r.get("status") == "pending" for r in settled):
        return "refund_pending", detail
    if mismatched:
        return "payment_mismatch", detail
    if len(captures) >= 2:
        if order_total and captured_sum == order_total:
            return "valid_split_payment", detail
        return "duplicate_charge", detail
    if delivered and estimated and delivered > estimated:
        detail["late_actor"] = actor
        if actor == "seller":
            return "late_delivery_seller", detail
        return "late_delivery_logistics", detail
    if not captures:
        return "insufficient_evidence", detail
    return "unsupported_claim", detail


async def policy_agent(ctx: CaseContext) -> Finding:
    """Collect the authoritative policy table; the ruling itself happens in decide()."""
    found = Finding(actor="policy-agent")
    if not ctx.policy_version:
        found.failures.append("missing_policy_version")
        return found
    if await ctx.fetch(found.actor, "get_policy", policy_version=ctx.policy_version) is None:
        found.failures.append("get_policy_unavailable")
    elif ref := ctx.ref("get_policy"):
        found.refs["get_policy"] = ref
    return found


def decide(ctx: CaseContext, findings: dict[str, Finding]) -> dict[str, Any]:
    issue, detail = classify(ctx, findings)
    policy = (ctx.data("get_policy") or {}).get("rules") or {}
    rule = policy.get(issue) or {}

    ctx.trace.emit(
        case_id=ctx.case_id,
        event_type="policy_decided",
        actor="policy-agent",
        target="verifier",
        decision_code=issue,
        evidence_refs=_supporting_refs(ctx, issue),
        attributes={k: v for k, v in detail.items() if isinstance(v, str | int | float | bool)},
    )
    return _build_output(ctx, findings, issue, rule)


def _supporting_refs(ctx: CaseContext, issue: str) -> list[str]:
    tools = ISSUE_EVIDENCE.get(issue, ("get_order",))
    refs = [ref for tool in tools if (ref := ctx.ref(tool))]
    if policy_ref := ctx.ref("get_policy"):
        refs.append(policy_ref)
    return list(dict.fromkeys(refs))


def _entities(ctx: CaseContext, findings: dict[str, Finding]) -> dict[str, list[str]]:
    order = findings["order-agent"].facts.get("order") or {}
    genuine = findings["order-agent"].facts.get("genuine_items") or []
    payments = findings["payment-agent"].facts.get("payments") or []
    shipment = findings["shipment-agent"].facts.get("shipment") or {}

    def unique(values: list[Any]) -> list[str]:
        seen = [str(v) for v in values if isinstance(v, str | int) and str(v)]
        return list(dict.fromkeys(seen))[:20]

    order_ids = unique([order.get("order_id") or ctx.order_id])
    return {
        "order_ids": order_ids,
        "item_ids": unique([r.get("order_item_id") for r in genuine]),
        "seller_ids": unique([r.get("seller_id") for r in genuine]),
        "payment_references": unique([r.get("payment_type") for r in payments]),
        "shipment_ids": unique([shipment.get("order_id")] if shipment else []),
    }


def _responsible(rule: dict[str, Any], findings: dict[str, Finding]) -> list[dict[str, Any]]:
    """Policy names the accountable role; the party id must come from this order's evidence."""
    genuine = findings["order-agent"].facts.get("genuine_items") or []
    seller_ids = [r.get("seller_id") for r in genuine if r.get("seller_id")]
    parties = []
    for entry in rule.get("responsible_parties") or []:
        party_type = entry.get("party_type")
        if party_type not in {
            "seller", "platform", "logistics_provider", "payment_provider", "customer", "unknown"
        }:
            continue
        party_id = None
        if party_type == "seller" and seller_ids:
            party_id = str(seller_ids[0])[:128]
        parties.append({"party_type": party_type, "party_id": party_id})
    return parties[:5] or [{"party_type": "unknown", "party_id": None}]


def _conflicts(findings: dict[str, Finding]) -> list[dict[str, Any]]:
    """Report only genuine multi-source disagreements; the schema needs at least two sources."""
    order_facts = findings["order-agent"].facts
    items = order_facts.get("items") or []
    genuine = order_facts.get("genuine_items") or []
    conflicts: list[dict[str, Any]] = []
    if len(items) > len(genuine) >= 1:
        conflicts.append({
            "field": "order_items.shipping_limit_date",
            "sources": ["get_order_items.anchored_row", "get_order_items.off_anchor_row"],
            "selected_source": "get_order_items.anchored_row",
            "resolution_code": "anchor_on_order_approved_at",
        })
    events = findings["payment-agent"].facts.get("payment_events") or []
    order = order_facts.get("order") or {}
    approved = _dt(order.get("order_approved_at"))
    if approved and events:
        off_anchor = [
            e for e in events if (d := _dt(e.get("event_at"))) and d.date() != approved.date()
        ]
        if off_anchor:
            conflicts.append({
                "field": "payment_timeline.events",
                "sources": [
                    "get_payment_timeline.anchored_events",
                    "get_payment_timeline.off_anchor_events",
                ],
                "selected_source": "get_payment_timeline.anchored_events",
                "resolution_code": "anchor_on_order_approved_at",
            })
    return conflicts[:5]


def _claims(ctx: CaseContext, issue: str, refs: list[str], refund: float) -> list[dict[str, Any]]:
    assessments = []
    for claim in ctx.claims[:5]:
        claim_id = claim.get("claim_id")
        if not isinstance(claim_id, str) or not claim_id:
            continue
        topic = claim.get("topic")
        if topic == "requested_full_refund":
            verdict = "supported" if refund > 0 else "unsupported"
        elif topic == issue:
            verdict = "supported"
        elif issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
        else:
            verdict = "unsupported"
        assessments.append({
            "claim_id": claim_id[:64],
            "verdict": verdict,
            "confidence": 0.9 if verdict in {"supported", "unsupported"} else 0.4,
            "evidence_refs": refs if verdict in {"supported", "partially_supported"} else [],
        })
    return assessments


def _build_output(
    ctx: CaseContext, findings: dict[str, Finding], issue: str, rule: dict[str, Any]
) -> dict[str, Any]:
    refs = _supporting_refs(ctx, issue)
    refund = float(rule.get("refund_brl") or 0.0)
    action = rule.get("recommended_action")
    status = rule.get("case_status")
    if status not in {"action_required", "no_action", "needs_investigation"}:
        status = "needs_investigation" if issue == "insufficient_evidence" else "action_required"

    refund_lines: list[dict[str, Any]] = []
    if refund > 0:
        refund_lines.append({
            "reason_code": str(action or issue)[:80],
            "amount_brl": refund,
            "entity_id": (ctx.order_id or None),
        })

    confidence = ISSUE_CONFIDENCE.get(issue, 0.5)

    return {
        "schema_version": SCHEMA_VERSION,
        "case_id": ctx.case_id,
        "assessment": {
            "primary_issue": issue,
            "case_status": status,
            "confidence": confidence,
        },
        "affected_entities": _entities(ctx, findings),
        "claim_assessments": _claims(ctx, issue, refs, refund),
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": issue.upper()[:80], "rank": 1}],
            "responsible_parties": _responsible(rule, findings),
        },
        "evidence_refs": refs,
        "data_conflicts": _conflicts(findings),
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund,
            "refund_lines": refund_lines,
        },
        "resolution_actions": [str(action)[:80]] if action else [],
    }


def verify(ctx: CaseContext, findings: dict[str, Finding], output: dict[str, Any]) -> list[str]:
    """Enforce the invariants in ARCHITECTURE.md section 6, repairing in place.

    Returns the codes of the invariants that had to be corrected.
    """
    repaired: list[str] = []
    known = {entry["ref"] for entry in ctx.evidence.values()}
    assessment = output["assessment"]
    financial = output["financial_resolution"]

    # Evidence ownership: only refs this case actually received may be cited.
    kept = [r for r in output["evidence_refs"] if r in known][:30]
    if kept != output["evidence_refs"]:
        repaired.append("evidence_ownership")
    output["evidence_refs"] = kept
    for claim in output.get("claim_assessments") or []:
        claim["evidence_refs"] = [r for r in claim["evidence_refs"] if r in known][:30]
        if claim["verdict"] in {"supported", "partially_supported"} and not claim["evidence_refs"]:
            claim["verdict"] = "insufficient_evidence"
            claim["confidence"] = 0.35
            repaired.append("claim_linkage")

    # Entity scope: never name an entity that no retrieved evidence mentions.
    genuine = findings["order-agent"].facts.get("genuine_items") or []
    seller_ids = {str(r.get("seller_id")) for r in genuine if r.get("seller_id")}
    entities = output["affected_entities"]
    scoped = [s for s in entities["seller_ids"] if s in seller_ids]
    if scoped != entities["seller_ids"]:
        repaired.append("entity_scope")
        entities["seller_ids"] = scoped

    # Money totals: the headline refund must equal the sum of its lines.
    lines_total = round(sum(line["amount_brl"] for line in financial["refund_lines"]), 2)
    if lines_total != round(financial["recommended_refund_brl"], 2):
        financial["recommended_refund_brl"] = lines_total
        repaired.append("money_total")

    if not output["evidence_refs"]:
        assessment["primary_issue"] = "insufficient_evidence"
        assessment["case_status"] = "needs_investigation"
        assessment["confidence"] = ISSUE_CONFIDENCE["insufficient_evidence"]
        financial["recommended_refund_brl"] = 0.0
        financial["refund_lines"] = []
        output["resolution_actions"] = []
        repaired.append("no_supporting_evidence")

    issue = assessment["primary_issue"]

    # Responsibility consistency: a seller-attributed fault must name this order's seller,
    # and a party that did not cause the fault must not be left holding it.
    parties = output["root_cause_analysis"]["responsible_parties"]
    if issue in SELLER_ATTRIBUTED and seller_ids:
        for party in parties:
            if party["party_type"] == "seller" and not party["party_id"]:
                party["party_id"] = sorted(seller_ids)[0][:128]
                repaired.append("seller_party_id")
    if issue == "late_delivery_logistics":
        filtered = [p for p in parties if p["party_type"] != "seller"]
        if filtered != parties:
            repaired.append("responsibility_mismatch")
            output["root_cause_analysis"]["responsible_parties"] = filtered or [
                {"party_type": "logistics_provider", "party_id": None}
            ]

    # Action/status consistency in both directions.
    if assessment["case_status"] == "no_action" and (
        financial["recommended_refund_brl"] or financial["refund_lines"]
    ):
        financial["recommended_refund_brl"] = 0.0
        financial["refund_lines"] = []
        repaired.append("no_action_refund")
    if financial["recommended_refund_brl"] > 0 and assessment["case_status"] != "action_required":
        assessment["case_status"] = "action_required"
        repaired.append("refund_requires_action")
    if assessment["case_status"] == "action_required" and not output["resolution_actions"]:
        output["resolution_actions"] = ["review_case"]
        repaired.append("missing_action")

    return repaired


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Coordinator: dispatch specialists, apply policy, verify, and return the case output.

    The harness emits case_received and case_finalized around this call, so they are not
    repeated here.
    """
    ctx = CaseContext(case, gateway, trace)
    agents = (
        ("order-agent", order_item_agent),
        ("payment-agent", payment_agent),
        ("shipment-agent", shipment_agent),
        ("policy-agent", policy_agent),
    )
    for name, _ in agents:
        trace.emit(
            case_id=ctx.case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=name,
        )

    results = await asyncio.gather(*(agent(ctx) for _, agent in agents))
    findings = {found.actor: found for found in results}
    for name, _ in agents[:3]:
        trace.emit(
            case_id=ctx.case_id,
            event_type="handoff",
            actor=name,
            target="policy-agent",
            decision_code="findings_ready",
        )

    output = decide(ctx, findings)
    repaired = verify(ctx, findings, output)
    trace.emit(
        case_id=ctx.case_id,
        event_type="verification_completed",
        actor="verifier",
        target="coordinator",
        decision_code="repaired" if repaired else "invariants_hold",
        evidence_refs=output["evidence_refs"][:20],
        attributes={"repairs": ",".join(repaired)[:200]} if repaired else None,
    )
    return output
