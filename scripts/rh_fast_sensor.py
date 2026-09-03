#!/usr/bin/env python3
"""RH GitHub Fast Sensor v0.2 — low-noise NEW_REPO discovery."""
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

# Redundant discovery queries are intentional. Exact freshness and RH evidence are
# re-checked after retrieval, so search-engine false positives do not become alerts.
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

DIRECT = {
    "Robinhood Chain": re.compile(r"\brobinhood[\s_-]+chain\b", re.I),
    "chainId 4663": re.compile(r"(?:chain\s*id|chainid|chain_id)\s*[:=]?\s*[`'\"]?4663\b", re.I),
    "RH mainnet RPC": re.compile(r"rpc\.mainnet\.chain\.robinhood\.com", re.I),
    "RH testnet RPC": re.compile(r"rpc\.testnet\.chain\.robinhood\.com", re.I),
    "RH Blockscout": re.compile(r"robinhoodchain\.blockscout\.com", re.I),
    "RH testnet 46630": re.compile(r"\b46630\b", re.I),
}
TECH = {
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
    "Contract tooling": re.compile(r"\bsolidity\b|\bfoundry\b|\bhardhat\b|\bviem\b|\bwagmi\b|smart\s+contract", re.I),
}


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(x: dt.datetime) -> str:
    return x.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(x: str) -> dt.datetime:
    return dt.datetime.fromisoformat(x.replace("Z", "+00:00"))


def gh(path: str, method: str = "GET", payload: dict | None = None):
    url = path if path.startswith("http") else API + path
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "rh-fast-sensor/0.2",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:1000]
        raise RuntimeError(f"GitHub HTTP {e.code}: {url}: {detail}") from e


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"version": 1, "started_at": iso(now()), "seen_repo_ids": {}}
    s = json.loads(STATE_PATH.read_text())
    s.setdefault("version", 1)
    s.setdefault("started_at", iso(now()))
    s.setdefault("seen_repo_ids", {})
    return s


def save_state(s: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(s, indent=2, sort_keys=True) + "\n")


def readme(full_name: str) -> str:
    try:
        obj = gh(f"/repos/{full_name}/readme")
    except RuntimeError as e:
        if "HTTP 404" in str(e):
            return ""
        raise
    if not isinstance(obj, dict):
        return ""
    raw = obj.get("content", "")
    if obj.get("encoding") == "base64" and raw:
        try:
            return base64.b64decode(raw).decode("utf-8", "replace")[:60000]
        except Exception:
            return ""
    return str(raw)[:60000]


def search_new(cutoff: dt.datetime) -> dict[int, dict]:
    found: dict[int, dict] = {}
    since_date = cutoff.date().isoformat()
    for base in BASE_QUERIES:
        q = f"{base} created:>={since_date} fork:false archived:false"
        qs = urllib.parse.urlencode({"q": q, "sort": "updated", "order": "desc", "per_page": 50})
        res = gh(f"/search/repositories?{qs}") or {}
        for repo in res.get("items", []):
            if parse_iso(repo["created_at"]) >= cutoff:
                found[int(repo["id"])] = repo
        time.sleep(0.15)
    return found


def classify(repo: dict, md: str) -> tuple[list[str], list[str]]:
    text = "\n".join([
        repo.get("full_name") or "",
        repo.get("description") or "",
        repo.get("homepage") or "",
        " ".join(repo.get("topics") or []),
        md,
    ])
    return (
        [k for k, rx in DIRECT.items() if rx.search(text)],
        [k for k, rx in TECH.items() if rx.search(text)],
    )


def existing_issue(full_name: str) -> bool:
    q = f'repo:{SENSOR_REPO} is:issue in:title "[RH-FAST][NEW_REPO] {full_name}"'
    qs = urllib.parse.urlencode({"q": q, "per_page": 5})
    return bool((gh(f"/search/issues?{qs}") or {}).get("total_count", 0))


def notify(c: dict) -> str:
    title = f"[RH-FAST][NEW_REPO] {c['full_name']}"
    body = (
        "## RH Fast Sensor candidate\n\n"
        f"**Trigger:** NEW_REPO  \n**Created:** {c['created_at']}  \n"
        f"**Detected:** {c['detected_at']}  \n**Repo:** {c['html_url']}  \n"
        f"**Homepage:** {c.get('homepage') or 'unknown'}  \n"
        f"**Description:** {c.get('description') or 'none'}\n\n"
        f"**Hard RH evidence:** {', '.join(c['direct_signals'])}\n\n"
        f"**Technical signals:** {', '.join(c['tech_signals'])}\n\n"
        f"**Fast score:** {c['score']}\n\n---\n"
        "Machine-generated discovery only: not a quality verdict, token endorsement, or proof that the project identity itself is new. "
        "Deep code/identity/deployment checks remain the RH Tech radar's job.\n"
    )
    obj = gh(f"/repos/{SENSOR_REPO}/issues", "POST", {"title": title, "body": body})
    return obj.get("html_url", "") if isinstance(obj, dict) else ""


def main() -> int:
    if not TOKEN or not SENSOR_REPO:
        print("GITHUB_TOKEN and SENSOR_REPO are required", file=sys.stderr)
        return 2

    run_at = now()
    state = load_state()
    cutoff = max(run_at - dt.timedelta(minutes=LOOKBACK_MINUTES), parse_iso(state["started_at"]))
    repos = search_new(cutoff)
    candidates = []

    for repo_id, repo in repos.items():
        if str(repo_id) in state["seen_repo_ids"]:
            continue
        direct, tech = classify(repo, readme(repo["full_name"]))
        if not direct or not tech:
            continue
        age_min = max(0.0, (run_at - parse_iso(repo["created_at"])).total_seconds() / 60)
        candidates.append({
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
            "score": 3 * len(direct) + min(8, len(tech)) + (3 if age_min <= 30 else 1),
        })

    candidates.sort(key=lambda x: (x["score"], x["created_at"]), reverse=True)
    failures = []
    changed = False
    for c in candidates[:MAX_CANDIDATES]:
        try:
            issue_url = "existing" if existing_issue(c["full_name"]) else notify(c)
            state["seen_repo_ids"][str(c["repo_id"])] = {
                "full_name": c["full_name"],
                "created_at": c["created_at"],
                "first_seen_at": c["detected_at"],
                "issue": issue_url,
            }
            changed = True
            print(f"RH_FAST_NEW {c['full_name']} score={c['score']} issue={issue_url}")
        except Exception as e:
            failures.append(f"{c['full_name']}: {e}")
            print(f"RH_FAST_NOTIFY_ERROR {c['full_name']}: {e}", file=sys.stderr)

    # Persist only real dedupe changes; no empty 5-minute heartbeat commits.
    if changed:
        save_state(state)

    print(json.dumps({
        "run_at": iso(run_at), "cutoff": iso(cutoff), "query_matches": len(repos),
        "candidates": len(candidates[:MAX_CANDIDATES]), "state_changed": changed,
        "failures": failures,
    }, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
