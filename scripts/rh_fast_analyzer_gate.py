#!/usr/bin/env python3
"""Notification gate for RH Fast AI analyzer.

Only PUSH/WATCH candidates notify the repo owner. SKIP candidates keep their AI
report in the PR body and close silently (no assignee, no bot comment).
Regression-tested against a low-signal public RH candidate.
"""
from __future__ import annotations

import re

import rh_fast_analyzer as analyzer

_DECISION_BY_PR: dict[int, str] = {}
_ORIG_COMMENT = analyzer.comment
_ORIG_ASSIGN = analyzer.assign_owner
_ORIG_CLOSE = analyzer.close_pr


def _decision(body: str) -> str | None:
    matches = re.findall(r"(?m)^(PUSH|WATCH|SKIP)\s*$", body)
    return matches[-1] if matches else None


def _candidate_name(pr_number: int) -> str:
    pr = analyzer.gh(f"/repos/{analyzer.SENSOR_REPO}/pulls/{pr_number}") or {}
    title = pr.get("title") or ""
    if title.startswith(analyzer.PREFIX):
        return title[len(analyzer.PREFIX):].strip()
    for prefix in ("[RH-FAST][PUSH] ", "[RH-FAST][WATCH] ", "[RH-FAST][SKIP] "):
        if title.startswith(prefix):
            return title[len(prefix):].strip()
    return title


def gated_comment(pr_number: int, body: str) -> None:
    decision = _decision(body) if analyzer.MARKER in body else None
    if decision is None:
        # Analyzer errors are kept in the PR body instead of generating user noise.
        if analyzer.ERROR_MARKER in body:
            pr = analyzer.gh(f"/repos/{analyzer.SENSOR_REPO}/pulls/{pr_number}") or {}
            old = pr.get("body") or ""
            analyzer.gh(
                f"/repos/{analyzer.SENSOR_REPO}/pulls/{pr_number}",
                "PATCH",
                {"body": old + "\n\n---\n" + body},
            )
            return
        _ORIG_COMMENT(pr_number, body)
        return

    _DECISION_BY_PR[pr_number] = decision
    name = _candidate_name(pr_number)
    analyzer.gh(
        f"/repos/{analyzer.SENSOR_REPO}/pulls/{pr_number}",
        "PATCH",
        {"title": f"[RH-FAST][{decision}] {name}"},
    )

    if decision == "SKIP":
        # Preserve the report for auditability without creating a notification event.
        pr = analyzer.gh(f"/repos/{analyzer.SENSOR_REPO}/pulls/{pr_number}") or {}
        old = pr.get("body") or ""
        analyzer.gh(
            f"/repos/{analyzer.SENSOR_REPO}/pulls/{pr_number}",
            "PATCH",
            {"body": old + "\n\n---\n" + body},
        )
        print(f"RH_FAST_NOTIFY_SUPPRESSED pr={pr_number} decision=SKIP")
        return

    # PUSH/WATCH: comment + assignment are the intentional notification path.
    _ORIG_COMMENT(pr_number, body)


def gated_assign_owner(pr_number: int) -> None:
    decision = _DECISION_BY_PR.get(pr_number)
    if decision in {"PUSH", "WATCH"}:
        _ORIG_ASSIGN(pr_number)
    else:
        print(f"RH_FAST_ASSIGN_SUPPRESSED pr={pr_number} decision={decision or 'UNKNOWN'}")


def gated_close_pr(pr_number: int) -> None:
    _ORIG_CLOSE(pr_number)


analyzer.comment = gated_comment
analyzer.assign_owner = gated_assign_owner
analyzer.close_pr = gated_close_pr

if __name__ == "__main__":
    raise SystemExit(analyzer.main())
