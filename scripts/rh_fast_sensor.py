#!/usr/bin/env python3
"""RH GitHub Fast Sensor v0.1

Purpose: discover newly-created public GitHub repositories with hard Robinhood Chain
technical evidence within minutes, then emit a low-noise GitHub issue in the sensor
repository. This is candidate generation only; deep technical/project analysis remains
in the RH Tech radar.

No third-party packages and no external secrets are required. The workflow-provided
GITHUB_TOKEN is used only for GitHub REST reads and issue creation in this repository.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "")
SENSOR_REPO = os.environ.get("SENSOR_REPO", "")
STATE_PATH = Path(os.environ.get("STATE_PATH", "state/seen.json"))
LOOKBACK_MINUTES = int(os.environ.get("LOOKBACK_MINUTES", "120"))
MAX_CANDIDATES = int(os.environ.get("MAX_CANDIDATES", "10"))

# Search is intentionally redundant. Exact freshness is enforced after retrieval using
# repository.created_at, so the date qualifier is only an API-side volume reducer.
BASE_QUERIES = [
    '"Robinhood Chain" in:name,description,readme',
    '4663 in:readme',
    '46630 in:readme',
    '"rpc.mainnet.chain.robinhood.com" in:readme',
    '"rpc.testnet.chain.robinhood.com" in:readme',
    '"robinhoodchain.blockscout.com" in:readme',
    'Robinhood Morpho in:name,description,readme',
    'Robinhood Uniswap in:name,description,readme',
    'Robinhood agent in:name,description,readme',
    'Robinhood RWA in:name,description,readme',
]

DIRECT_PATTERNS = {
    "Robinhood Chain": re.compile(r"\brobinhood[\s_-]+chain\b", re.I),
    "chainId 4663": re.compile(r"(?:chain\s*id|chainid|chain_id)\s*[:=]?\s*[`'\"]?4663\b", re.I),
    "RH mainnet RPC": re.compile(r"rpc\.mainnet\.chain\.robinhood\.com", re.I),
    "RH testnet RPC": re.compile(r"rpc\.testnet\.chain\.robinhood\.com", re.I),
    "RH Blockscout": re.compile(r"robinhoodchain\.blockscout\.com", re.I),
    "RH testnet chain 46630": re.compile(r"\b46630\b", re.I),
}

TECH_PATTERNS = {
    "Morpho": re.compile(r"\bmorpho\b", re.I),
    "Uniswap": re.compile(r"\buniswap\b|\bpoolmanager\b", re.I),
    "Hook": re.compile(r"\bhook\b|beforeSwap|afterSwap", re.I),
    "RWA/stock": re.compile(r"\brwa\b|tokeni[sz]ed\s+(?:stock|equity)|stock\s+token", re.I),
    "Oracle": re.compile(r"\boracle\b|chainlink", re.I),
    "Lending/Vault": re.compile(r"\blending\b|\bborrow\b|\bvault\b|\bcollateral\b", re.I),
    "Agent/MCP": re.compile(r"\bagent\b|\bmcp\b", re.I),
    "Launchpad": re.compile(r"\blaunchpad\b|token\s+launch", re.I),
    "Bridge": re.compile(r"\bbridge\b|cross[- ]chain", re.I),
    "Derivatives": re.compile(r"\bperps?\b|\boptions?\b|derivative", re.I),
    "Smart-contract tooling": re.compile(r"\bsolidity\b|\bfoundry\b|\bhardhat\b|\bviem\b|\bwagmi\b|smart\s+contract", re.I),
}


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def gh(path_or_url: str, method: str = "GET", payload: dict | None = None):
    url = path_or_url if path_or_url.startswith("http") else API + path_or_url
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "rh-github-fast-sensor/0.1",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:1200]
        raise RuntimeError(f"GitHub HTTP {e.code} for {url}: {detail}") from e


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"version": 1, "started_at": iso(now_utc()), "last_run_at": None, "seen_repo_ids": {}}
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    state.setdefault("version", 1)
    state.setdefault("started_at", iso(now_utc()))
    state.setdefault("last_run_at", None)
    state.setdefault("seen_repo_ids", {})
    return state


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def fetch_readme(full_name: str) -> str:
    try:
        obj = gh(f"/repos/{full_name}/readme")
    except RuntimeError as exc:
        # Empty/new repositories commonly have no README; that should not fail the run.
        if "HTTP 404" in str(exc):
            return ""
        raise
    if not isinstance(obj, dict):
        return ""
    content = obj.get("content", "")
    if obj.get("encoding") == "base64" and content:
        try:
            return base64.b64decode(content).decode("utf-8", "replace")[:60000]
        except Exception:
            return ""
    return str(content)[:60000]


def search_recent(cutoff: dt.datetime) -> dict[int, dict]:
    # GitHub repository search date qualifier is used only as a coarse reducer; exact
    # cutoff filtering happens below from created_at.
    since_date = cutoff.date().isoformat()
    found: dict[int, dict] = {}
    for base in BASE_QUERIES:
        q = f"{base} created:>={since_date} fork:false archived:false"
        params = urllib.parse.urlencode({"q": q, "sort": "updated", "order": "desc", "per_page": 50})
        result = gh(f"/search/repositories?{params}")
        for repo in (result or {}).get("items", []):
            created = parse_iso(repo["created_at"])
            if created >= cutoff:
                found[int(repo["id"])] = repo
        # Be polite to the Search API and reduce secondary-rate-limit risk.
        time.sleep(0.15)
    return found


def signals(repo: dict, readme: str) -> tuple[list[str], list[str], str]:
    text = "\n".join(
        [
            repo.get("full_name") or "",
            repo.get("description") or "",
            repo.get("homepage") or "",
            " ".join(repo.get("topics") or []),
            readme,
        ]
    )
    direct = [name for name, rx in DIRECT_PATTERNS.items() if rx.search(text)]
    tech = [name for name, rx in TECH_PATTERNS.items() if rx.search(text)]
    return direct, tech, text


def already_has_issue(full_name: str) -> bool:
    if not SENSOR_REPO:
        return False
    q = f'repo:{SENSOR_REPO} is:issue in:title "[RH-FAST][NEW_REPO] {full_name}"'
    params = urllib.parse.urlencode({"q": q, "per_page": 5})
    result = gh(f"/search/issues?{params}")
    return bool((result or {}).get("total_count", 0))


def create_candidate_issue(candidate: dict) -> str:
    if not SENSOR_REPO:
        raise RuntimeError("SENSOR_REPO is required for notification")
    full_name = candidate["full_name"]
    title = f"[RH-FAST][NEW_REPO] {full_name}"
    body = f"""## RH Fast Sensor candidate\n\n**Trigger:** NEW_REPO  \n**Created:** {candidate['created_at']}  \n**Detected:** {candidate['detected_at']}  \n**Repo:** {candidate['html_url']}  \n**Homepage:** {candidate.get('homepage') or 'unknown'}  \n**Description:** {candidate.get('description') or 'none'}\n\n**Hard RH evidence:** {', '.join(candidate['direct_signals'])}\n\n**Technical signals:** {', '.join(candidate['tech_signals'])}\n\n**Fast score:** {candidate['score']}\n\n---\nThis issue is machine-generated candidate discovery only. It is **not** a project-quality verdict, token endorsement, or proof that the project itself is newly created beyond the GitHub repository timestamp. Deep identity/code/deployment checks belong to the RH Tech radar.\n"""
    obj = gh(f"/repos/{SENSOR_REPO}/issues", method="POST", payload={"title": title, "body": body})
    return obj.get("html_url", "") if isinstance(obj, dict) else ""


def main() -> int:
    run_at = now_utc()
    cutoff = run_at - dt.timedelta(minutes=LOOKBACK_MINUTES)
    state = load_state()
    started_at = parse_iso(state["started_at"])
    # Never alert on repositories created before this sensor was installed. The wider
    # lookback only protects against cron/indexing delays after installation.
    effective_cutoff = max(cutoff, started_at)

    repos = search_recent(effective_cutoff)
    candidates = []
    for repo_id, repo in repos.items():
        rid = str(repo_id)
        if rid in state["seen_repo_ids"]:
            continue
        readme = fetch_readme(repo["full_name"])
        direct, tech, _ = signals(repo, readme)
        # Hard gate: real Robinhood Chain evidence AND at least one technical signal.
        if not direct or not tech:
            continue
        age_minutes = max(0.0, (run_at - parse_iso(repo["created_at"])).total_seconds() / 60.0)
        score = 3 * len(direct) + min(8, len(tech)) + (3 if age_minutes <= 30 else 1)
        candidates.append(
            {
                "repo_id": repo_id,
                "full_name": repo["full_name"],
                "html_url": repo["html_url"],
                "created_at": repo["created_at"],
                "pushed_at": repo.get("pushed_at"),
                "detected_at": iso(run_at),
                "description": repo.get("description"),
                "homepage": repo.get("homepage"),
                "direct_signals": direct,
                "tech_signals": tech,
                "score": score,
            }
        )

    candidates.sort(key=lambda x: (x["score"], x["created_at"]), reverse=True)
    candidates = candidates[:MAX_CANDIDATES]

    failures = []
    for candidate in candidates:
        rid = str(candidate["repo_id"])
        try:
            if already_has_issue(candidate["full_name"]):
                issue_url = "existing"
            else:
                issue_url = create_candidate_issue(candidate)
            state["seen_repo_ids"][rid] = {
                "full_name": candidate["full_name"],
                "created_at": candidate["created_at"],
                "first_seen_at": candidate["detected_at"],
                "issue": issue_url,
            }
            print(f"RH_FAST_NEW {candidate['full_name']} score={candidate['score']} issue={issue_url}")
        except Exception as exc:
            failures.append(f"{candidate['full_name']}: {exc}")
            print(f"RH_FAST_NOTIFY_ERROR {candidate['full_name']}: {exc}", file=sys.stderr)

    state["last_run_at"] = iso(run_at)
    state["last_query_count"] = len(repos)
    state["last_candidate_count"] = len(candidates)
    save_state(state)

    print(
        json.dumps(
            {
                "run_at": iso(run_at),
                "effective_cutoff": iso(effective_cutoff),
                "query_matches": len(repos),
                "candidates": len(candidates),
                "failures": failures,
            },
            ensure_ascii=False,
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
