"""Shared utilities for MoneyFlow 837P skill pack.

Provides SQLite connectivity and 837P parsing helpers used across
parse_837p, underpayment_detector, dashboard_reflector, and claude_code_bridge.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

MONEYFLOW_DIR = os.environ.get("MONEYFLOW_DIR", "/home/tyler/dev/prod/edi/cuntx")
MONEYFLOW_DB = os.environ.get("MONEYFLOW_DB", f"{MONEYFLOW_DIR}/backend/cuntx.db")


# ---------------------------------------------------------------------------
# Database Connection
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    """Get a live SQLite connection to MoneyFlow DB."""
    Path(MONEYFLOW_DB).touch(exist_ok=True)
    conn = sqlite3.connect(MONEYFLOW_DB)
    conn.row_factory = sqlite3.Row
    return conn


def db_query(query: str, params: Tuple = ()) -> List[sqlite3.Row]:
    """Execute a read query and return rows."""
    with get_db() as conn:
        return list(conn.execute(query, params).fetchall())


def db_execute(query: str, params: Tuple = ()) -> sqlite3.Cursor:
    """Execute a write query."""
    with get_db() as conn:
        return conn.execute(query, params)


# ---------------------------------------------------------------------------
# Dashboard Stats
# ---------------------------------------------------------------------------

@dataclass
class DashboardStats:
    total_claims: int = 0
    total_value: float = 0.0
    runs: int = 0
    evv_pending: int = 0
    evv_validated: int = 0
    evv_failed: int = 0
    underpayment_total: float = 0.0
    underpayment_count: int = 0


def get_dashboard_stats() -> DashboardStats:
    """Fetch current dashboard stats from the real MoneyFlow SQLite DB."""
    conn = get_db()
    try:
        claims_row = conn.execute(
            "SELECT COUNT(*) as count, COALESCE(SUM(total_charge), 0) as total FROM claims_summary"
        ).fetchone()

        runs_row = conn.execute(
            "SELECT COUNT(*) as count FROM runs"
        ).fetchone()

        # EVV required claims (evv_required = 1 in claims_summary)
        evv_row = conn.execute("""
            SELECT
                SUM(CASE WHEN evv_required = 1 AND status = 'pending' THEN 1 ELSE 0 END) as pending,
                SUM(CASE WHEN evv_required = 1 AND status = 'processed' THEN 1 ELSE 0 END) as validated,
                SUM(CASE WHEN evv_required = 1 AND status = 'rejected' THEN 1 ELSE 0 END) as failed
            FROM claims_summary
        """).fetchone()

        # Underpayments: where paid < billed (allowed_amount or paid_amount < total_charge)
        up_row = conn.execute("""
            SELECT
                COALESCE(SUM(total_charge - COALESCE(paid_amount, 0)), 0) as total,
                COUNT(*) as count
            FROM claims_summary
            WHERE paid_amount < total_charge
        """).fetchone()

        return DashboardStats(
            total_claims=claims_row["count"] or 0,
            total_value=float(claims_row["total"] or 0),
            runs=runs_row["count"] or 0,
            evv_pending=evv_row["pending"] or 0,
            evv_validated=evv_row["validated"] or 0,
            evv_failed=evv_row["failed"] or 0,
            underpayment_total=float(up_row["total"] or 0),
            underpayment_count=up_row["count"] or 0,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 837P Segment Parsing Helpers
# ---------------------------------------------------------------------------

@dataclass
class NM1Segment:
    entity_id_code: str = ""
    entity_type: str = ""
    name_last: str = ""
    name_first: str = ""
    name_middle: str = ""
    prefix: str = ""
    suffix: str = ""
    qualifier: str = ""
    identifier: str = ""


@dataclass
class REFSegment:
    qualifier: str = ""
    reference_id: str = ""


@dataclass
class DTPSegment:
    qualifier: str = ""
    date_format: str = ""
    date_value: str = ""


@dataclass
class SVCSegment:
    product_service_id: str = ""
    billed_amount: float = 0.0
    paid_amount: float = 0.0
    units: str = ""


@dataclass
class CASSegment:
    group_code: str = ""
    reason_code: str = ""
    amount: float = 0.0
    quantity: str = ""


@dataclass
class Claim:
    control_number: str = ""
    submitter_name: str = ""
    receiver_name: str = ""
    billing_provider_nm1: Optional[NM1Segment] = None
    subscriber_nm1: Optional[NM1Segment] = None
    patient_nm1: Optional[NM1Segment] = None
    service_lines: List[SVCSegment] = field(default_factory=list)
    cas_segments: List[CASSegment] = field(default_factory=list)
    denial_code: str = ""
    billed_amount: float = 0.0
    paid_amount: float = 0.0
    date_of_service: Optional[date] = None


def parse_segment(line: str) -> Tuple[str, Dict[str, str]]:
    """Parse a single X12 segment line into (segment_type, fields dict).

    Handles the 837P segment structure including:
    - ISA/GS/GE/IEA envelope
    - HL hierarchy (billing provider / subscriber / patient)
    - NM1 name segments (IL=subscriber, PR=payer, 85=provider, etc.)
    - CLM claim header
    - SV1 service lines with composite HC codes (HC:T1019:U1:HR)
    - LX line counters, DTP dates, CAS adjustments, HI diagnosis
    - REF reference qualifiers
    """
    parts = line.strip().split("*")
    if not parts:
        return "", {}

    seg_id = parts[0]
    fields = {}

    if seg_id == "NM1":
        field_names = [
            "entity_id_code", "entity_type", "name_last", "name_first",
            "name_middle", "prefix", "suffix", "qualifier", "identifier",
        ]
        for i, name in enumerate(field_names):
            fields[name] = parts[i + 1] if i + 1 < len(parts) else ""

    elif seg_id == "CLM":
        # CLM*claim_id*billed_amount***hc_code*freq*sn*yes*yes
        fields["claim_id"] = parts[1] if len(parts) > 1 else ""
        fields["billed_amount"] = parts[2] if len(parts) > 2 else "0"
        fields["healthcare_code"] = parts[6] if len(parts) > 6 else ""

    elif seg_id == "REF":
        fields["qualifier"] = parts[1] if len(parts) > 1 else ""
        fields["reference_id"] = parts[2] if len(parts) > 2 else ""

    elif seg_id == "DTP":
        fields["qualifier"] = parts[1] if len(parts) > 1 else ""
        fields["date_format"] = parts[2] if len(parts) > 2 else ""
        fields["date_value"] = parts[3] if len(parts) > 3 else ""

    elif seg_id in ("SVC", "SV1"):
        # SV1*HC:T1019:U1:HR*26.91*UN*3.87***1
        # parts: [SV1, HC:T1019:U1:HR(cd), 26.91(charge), UN(unit_cd), 3.87(units), ...]
        fields["product_service_id"] = parts[1] if len(parts) > 1 else ""
        fields["billed_amount"] = parts[2] if len(parts) > 2 else "0"
        fields["paid_amount"] = (
            parts[3] if len(parts) > 3 and parts[3].replace(".", "").isdigit() else "0"
        )
        fields["units"] = parts[4] if len(parts) > 4 else ""

    elif seg_id == "LX":
        fields["line_number"] = parts[1] if len(parts) > 1 else ""

    elif seg_id == "HI":
        # HI*ABK:R69 — composite diagnosis code
        fields["healthcare_code"] = parts[1] if len(parts) > 1 else ""

    elif seg_id == "CAS":
        fields["group_code"] = parts[1] if len(parts) > 1 else ""
        fields["reason_code"] = parts[2] if len(parts) > 2 else ""
        fields["amount"] = parts[3] if len(parts) > 3 else "0"
        fields["quantity"] = parts[4] if len(parts) > 4 else ""

    elif seg_id == "HL":
        # HL*hl_id*parent*level*charge_yn
        # HL*1 = billing provider, HL*2 = subscriber, HL*3 = patient
        fields["hl_id"] = parts[1] if len(parts) > 1 else ""
        fields["parent"] = parts[2] if len(parts) > 2 else ""
        fields["level"] = parts[3] if len(parts) > 3 else ""
        fields["charge_yn"] = parts[4] if len(parts) > 4 else ""

    return seg_id, fields


def parse_837p_file(filepath: str) -> List[Claim]:
    """Parse an 837P file into Claim objects.

    Handles the X12 837P hierarchical structure where:
    - HL*1 = Billing provider level
    - HL*2 = Subscriber level (child of HL*1), contains NM1*IL (subscriber), NM1*PR (payer), CLM, LX+SV1
    - HL*3 = Patient level (child of HL*2)
    - NM1*IL may appear before CLM, so we track pending subscriber context

    File uses ~ as segment terminator (X12 standard).
    """
    claims: Dict[str, Claim] = {}
    current_claim_id = ""

    # Pending context — subscriber/payer NM1 that appear before CLM
    pending_subscriber_nm1: Optional[NM1Segment] = None
    pending_payer_nm1: Optional[NM1Segment] = None

    def _make_nm1(flds: Dict[str, str]) -> NM1Segment:
        return NM1Segment(
            entity_id_code=flds.get("entity_id_code", ""),
            entity_type=flds.get("entity_type", ""),
            name_last=flds.get("name_last", ""),
            name_first=flds.get("name_first", ""),
            name_middle=flds.get("name_middle", ""),
            qualifier=flds.get("qualifier", ""),
            identifier=flds.get("identifier", ""),
        )

    with open(filepath, "r") as fh:
        # File uses ~ as segment terminator; split and strip each segment
        segment_strs = [s.strip() for s in fh.read().split("~") if s.strip()]

    for raw_seg in segment_strs:
        seg_id, fields = parse_segment(raw_seg)

        # Track HL hierarchy — reset pending context at new billing provider level
        if seg_id == "HL":
            hl_id = fields.get("hl_id", "")
            if hl_id == "1":
                # New billing provider — clear pending subscriber/payer
                pending_subscriber_nm1 = None
                pending_payer_nm1 = None
                current_claim_id = ""

        elif seg_id == "CLM":
            current_claim_id = fields.get("claim_id", "")
            claim = Claim(
                control_number=current_claim_id,
                billed_amount=float(fields.get("billed_amount", 0) or 0),
            )
            if pending_subscriber_nm1:
                claim.subscriber_nm1 = pending_subscriber_nm1
            if pending_payer_nm1:
                claim.billing_provider_nm1 = pending_payer_nm1
            claims[current_claim_id] = claim

        elif seg_id == "NM1":
            nm1 = _make_nm1(fields)
            eid_code = fields.get("entity_id_code", "")  # IL, PR, 85, 41, 40, QC
            # entity_type: 1 = person, 2 = organization

            if eid_code == "IL":
                # Subscriber
                if current_claim_id and claims.get(current_claim_id):
                    claims[current_claim_id].subscriber_nm1 = nm1
                else:
                    pending_subscriber_nm1 = nm1
            elif eid_code == "PR":
                # Payer
                if current_claim_id and claims.get(current_claim_id):
                    claims[current_claim_id].billing_provider_nm1 = nm1
                else:
                    pending_payer_nm1 = nm1
            elif eid_code == "85":
                # Billing provider
                if current_claim_id and claims.get(current_claim_id):
                    claims[current_claim_id].billing_provider_nm1 = nm1
            elif eid_code == "41":
                # Submitter name
                pass
            elif eid_code == "40":
                # Receiver name
                pass
            elif eid_code == "QC":
                # Patient
                if current_claim_id and claims.get(current_claim_id):
                    claims[current_claim_id].patient_nm1 = nm1

        elif seg_id in ("SVC", "SV1") and current_claim_id:
            svc = SVCSegment(
                product_service_id=fields.get("product_service_id", ""),
                billed_amount=float(fields.get("billed_amount", 0) or 0),
                paid_amount=float(fields.get("paid_amount", 0) or 0),
                units=fields.get("units", ""),
            )
            if current_claim_id in claims:
                claims[current_claim_id].service_lines.append(svc)

        elif seg_id == "CAS" and current_claim_id:
            cas = CASSegment(
                group_code=fields.get("group_code", ""),
                reason_code=fields.get("reason_code", ""),
                amount=float(fields.get("amount", 0) or 0),
                quantity=fields.get("quantity", ""),
            )
            if current_claim_id in claims:
                claims[current_claim_id].cas_segments.append(cas)

    return list(claims.values())


# ---------------------------------------------------------------------------
# OA-18 Underpayment Detection
# ---------------------------------------------------------------------------

@dataclass
class UnderpaymentRecord:
    claim_id: str = ""
    payer: str = ""
    denial_code: str = ""
    billed: float = 0.0
    paid: float = 0.0
    variance: float = 0.0
    date_of_service: Optional[str] = None
    status: str = "detected"


def detect_oa18_underpayments(claims: Optional[List[Claim]] = None) -> List[UnderpaymentRecord]:
    """Detect OA-18 denials and payment variances from claims or DB.

    OA-18 is "Coordination of Benefits / Other Insurance" denial.
    Real schema uses claims_summary with total_charge, paid_amount, denial_reason.
    """
    if claims is None:
        rows = db_query("""
            SELECT claim_id, payer, denial_reason, total_charge, paid_amount,
                   allowed_amount, service_date, status
            FROM claims_summary
            WHERE denial_reason LIKE '%OA-18%'
               OR denial_reason LIKE '%other insurance%'
               OR denial_reason LIKE '%OON%'
               OR (paid_amount IS NOT NULL AND paid_amount < total_charge)
            ORDER BY (total_charge - COALESCE(paid_amount, 0)) DESC
            LIMIT 100
        """)
        return [
            UnderpaymentRecord(
                claim_id=r["claim_id"] or "",
                payer=r["payer"] or "",
                denial_code=r["denial_reason"] or "",
                billed=float(r["total_charge"] or 0),
                paid=float(r["paid_amount"] or 0),
                variance=float((r["total_charge"] or 0) - (r["paid_amount"] or 0)),
                date_of_service=r["service_date"],
                status=r["status"] or "",
            )
            for r in rows
        ]

    results = []
    for claim in claims:
        if claim.denial_code == "OA-18" or claim.paid_amount < claim.billed_amount:
            results.append(
                UnderpaymentRecord(
                    claim_id=claim.control_number,
                    payer="",
                    denial_code=claim.denial_code,
                    billed=claim.billed_amount,
                    paid=claim.paid_amount,
                    variance=claim.billed_amount - claim.paid_amount,
                )
            )
    return results


# ---------------------------------------------------------------------------
# EVV Queue Helpers
# ---------------------------------------------------------------------------

@dataclass
class EVVQueueItem:
    claim_id: str = ""
    service_date: str = ""
    caregiver_id: str = ""
    patient_id: str = ""
    status: str = "pending"
    error_code: str = ""
    error_message: str = ""
    retry_count: int = 0


def get_evv_pending() -> List[EVVQueueItem]:
    """Get EVV-required claims pending validation."""
    rows = db_query("""
        SELECT claim_id, service_date, patient_name, status,
               denial_reason, procedure_code, modifiers
        FROM claims_summary
        WHERE evv_required = 1
          AND status IN ('pending', 'rejected', 'evv_pending')
        ORDER BY service_date ASC
        LIMIT 100
    """)
    return [
        EVVQueueItem(
            claim_id=r["claim_id"] or "",
            service_date=r["service_date"] or "",
            caregiver_id="",  # not in claims_summary
            patient_id=r["patient_name"] or "",
            status=r["status"] or "pending",
            error_code=r.get("denial_reason", "") or "",
            error_message=r.get("denial_reason", "") or "",
            retry_count=0,
        )
        for r in rows
    ]


def get_evv_summary() -> Dict[str, int]:
    """Get EVV queue summary counts from claims_summary."""
    rows = db_query("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN evv_required = 1 AND status = 'pending' THEN 1 ELSE 0 END) as pending,
            SUM(CASE WHEN evv_required = 1 AND status = 'processed' THEN 1 ELSE 0 END) as validated,
            SUM(CASE WHEN evv_required = 1 AND status = 'rejected' THEN 1 ELSE 0 END) as failed
        FROM claims_summary
    """)
    if rows:
        row = rows[0]
        return {
            "total": row["total"] or 0,
            "pending": row["pending"] or 0,
            "validated": row["validated"] or 0,
            "failed": row["failed"] or 0,
        }
    return {"total": 0, "pending": 0, "validated": 0, "failed": 0}


# ---------------------------------------------------------------------------
# Hermes Tool Registration
# ---------------------------------------------------------------------------

_TOOLS_REGISTERED = False


def register_moneyflow_tools():
    """Register all shared tools with the Hermes tool registry."""
    global _TOOLS_REGISTERED
    if _TOOLS_REGISTERED:
        return

    try:
        import tools as hermes_tools
    except ImportError:
        return

    hermes_tools.register(
        name="moneyflow_dashboard_stats",
        description="Get live MoneyFlow dashboard stats (claims, value, runs, EVV queue, underpayments)",
        parameters={},
        fn=get_dashboard_stats,
    )

    hermes_tools.register(
        name="moneyflow_parse_837p",
        description="Parse an 837P file and return structured Claim objects",
        parameters={
            "filepath": {"type": "string", "description": "Path to 837P file"},
        },
        fn=parse_837p_file,
    )

    hermes_tools.register(
        name="moneyflow_detect_oa18",
        description="Detect OA-18 underpayment denials from claims",
        parameters={
            "claims": {"type": "array", "description": "Parsed claims (optional)"},
        },
        fn=detect_oa18_underpayments,
    )

    hermes_tools.register(
        name="moneyflow_evv_pending",
        description="Get all pending EVV validations",
        parameters={},
        fn=get_evv_pending,
    )

    hermes_tools.register(
        name="moneyflow_evv_summary",
        description="Get EVV queue summary counts",
        parameters={},
        fn=get_evv_summary,
    )

    _TOOLS_REGISTERED = True
