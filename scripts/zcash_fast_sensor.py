#!/usr/bin/env python3
"""Zcash Fast Sensor v0.1.

Five-minute candidate discovery for genuinely new Zcash ecosystem activity.
Sources: narrow GitHub repository metadata/README search, fixed Zcash organizations,
Zcash Community Grants new issues, and newly-created Zcash Community Forum topics.
The sensor only discovers candidates; a separate AI stage decides PUSH/WATCH/SKIP.
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
FORUM = "https://forum.zcashcommunity.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "")
SENSOR_REPO = os.environ.get("SENSOR_REPO", "")
BASE_BRANCH = os.environ.get("SENSOR_BASE_BRANCH", "main")
STATE_PATH = Path(os.environ.get("ZCASH_STATE_PATH", "state/zcash_seen.json"))
MAX_CANDIDATES = int(os.environ.get("MAX_CANDIDATES", "12"))
OVERLAP_MINUTES = int(os.environ.get("OVERLAP_MINUTES", "30"))
RECOVERY_LOOKBACK_MINUTES = int(os.environ.get("RECOVERY_LOOKBACK_MINUTES", "1440"))

FIXED_OWNERS = [
    "zcash",
    "ZcashFoundation",
    "zodl-inc",
    "ShieldedLabs",
    "ZcashCommunityGrants",
]

REPO_QUERIES = [
    'zcash in:name,description,readme',
    '"Zcash" in:readme',
    '"zcash_client_backend" in:readme',
    '"zcash_primitives" in:readme',
    '"zcash_address" in:readme',
    '"librustzcash" in:readme',
    '"lightwalletd" in:readme',
    '"zebrad" in:readme',
    '"zaino" in:readme',
    '"ZcashLightClientKit" in:readme',
    '"cash.z.ecc.android.sdk" in:readme',
]

ZCASH_SIGNAL = {
    "Zcash": re.compile(r"\bzcash\b", re.I),
    "ZEC context": re.compile(r"\bzec\b.{0,80}\b(?:shield|wallet|payment|zcash|orchard|sapling)\b|\b(?:shield|wallet|payment|zcash|orchard|sapling)\b.{0,80}\bzec\b", re.I | re.S),
    "librustzcash": re.compile(r"\blibrustzcash\b|zcash_(?:client_backend|primitives|address)", re.I),
    "node/indexer": re.compile(r"\blightwalletd\b|\bzebrad\b|\bzaino\b", re.I),
    "mobile SDK": re.compile(r"ZcashLightClientKit|cash\.z\.ecc\.android\.sdk", re.I),
}
TECH_SIGNAL = {
    "wallet/payment": re.compile(r"\bwallet\b|\bpayment\b|\bmerchant\b|\bx402\b", re.I),
    "shielded/privacy": re.compile(r"\bshielded\b|\borchard\b|\bsapling\b|\bprivacy\b|\bpczt\b", re.I),
    "node/indexer": re.compile(r"\bnode\b|\bindexer\b|\brpc\b|lightwalletd|zebrad|zaino", re.I),
    "SDK/library": re.compile(r"\bsdk\b|\blibrary\b|\bcrate\b|\bapi\b|Cargo\.toml|Package\.swift|build\.gradle", re.I),
    "protocol/ZK": re.compile(r"\bprotocol\b|\bproof\b|\bzk\b|zero[- ]knowledge|\bcircuit\b", re.I),
    "DeFi/asset": re.compile(r"\bdefi\b|\bdex\b|\bswap\b|\blending\b|\basset\b|\btoken\b|\bzsa\b", re.I),
    "application": re.compile(r"\bapp\b|\bmarketplace\b|\bservice\b|\bplatform\b|\bcli\b", re.I),
    "code/build": re.compile(r"\brust\b|\btypescript\b|\bpython\b|\bgo\b|\bswift\b|\bkotlin\b|\bgithub\b|\bsource\b", re.I),
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
        "User-Agent": "zcash-fast-sensor/0.1",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:1000]
        raise RuntimeError(f"GitHub HTTP {e.code}: {url}: {detail}") from e


def web_json(url: str):
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 zcash-fast-sensor/0.1",
        },
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read())


def load_state(run_at: dt.datetime) -> dict:
    if STATE_PATH.exists():
        s = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    else:
        s = {}
    s.setdefault("version", 1)
    s.setdefault("started_at", iso(run_at))
    s.setdefault("seen", {})
    s.setdefault("watermarks", {})
    for source in ("github", "fixed_orgs", "zcg", "forum"):
        s["watermarks"].setdefault(source, s["started_at"])
    return s


def save_state(s: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(s, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def source_cutoff(state: dict, source: str, run_at: dt.datetime) -> dt.datetime:
    started = parse_iso(state["started_at"])
    wm = parse_iso(state["watermarks"].get(source, state["started_at"]))
    overlap = wm - dt.timedelta(minutes=OVERLAP_MINUTES)
    recovery = run_at - dt.timedelta(minutes=RECOVERY_LOOKBACK_MINUTES)
    return max(started, overlap, recovery)


def readme(full_name: str) -> str:
    try:
        obj = gh(f"/repos/{full_name}/readme")
    except Exception as e:
        if "HTTP 404" in str(e):
            return ""
        raise
    if not isinstance(obj, dict):
        return ""
    raw = obj.get("content") or ""
    if obj.get("encoding") == "base64" and raw:
        try:
            return base64.b64decode(raw).decode("utf-8", "replace")[:70000]
        except Exception:
            return ""
    return str(raw)[:70000]


def classify_repo(repo: dict, md: str) -> tuple[list[str], list[str]]:
    text = "\n".join(
        [
            repo.get("full_name") or "",
            repo.get("description") or "",
            repo.get("homepage") or "",
            " ".join(repo.get("topics") or []),
            md,
        ]
    )
    direct = [name for name, rx in ZCASH_SIGNAL.items() if rx.search(text)]
    tech = [name for name, rx in TECH_SIGNAL.items() if rx.search(text)]
    return direct, tech


def scan_github(cutoff: dt.datetime) -> list[dict]:
    found: dict[int, dict] = {}
    since_date = cutoff.date().isoformat()
    for base in REPO_QUERIES:
        q = f"{base} created:>={since_date} fork:false archived:false"
        qs = urllib.parse.urlencode({"q": q, "sort": "updated", "order": "desc", "per_page": 50})
        res = gh(f"/search/repositories?{qs}") or {}
        if res.get("incomplete_results"):
            raise RuntimeError(f"GitHub repo search incomplete_results=true for query: {base}")
        if int(res.get("total_count") or 0) >= 1000:
            raise RuntimeError(f"GitHub repo search reached 1000-result safety limit for query: {base}")
        for repo in res.get("items", []):
            if parse_iso(repo["created_at"]) >= cutoff:
                found[int(repo["id"])] = repo
        time.sleep(0.10)

    out = []
    for repo in found.values():
        direct, tech = classify_repo(repo, readme(repo["full_name"]))
        if not direct or not tech:
            continue
        out.append(
            {
                "key": f"repo:{repo['id']}",
                "source": "GITHUB_NEW_REPO",
                "stable_id": int(repo["id"]),
                "display": repo["full_name"],
                "url": repo["html_url"],
                "created_at": repo["created_at"],
                "metadata": {
                    "full_name": repo["full_name"],
                    "description": repo.get("description"),
                    "homepage": repo.get("homepage"),
                    "pushed_at": repo.get("pushed_at"),
                    "direct_signals": direct,
                    "tech_signals": tech,
                },
            }
        )
    return out


def owner_repos(owner: str) -> list[dict]:
    try:
        return gh(f"/orgs/{owner}/repos?type=public&sort=created&direction=desc&per_page=100") or []
    except Exception as e:
        if "HTTP 404" not in str(e):
            raise
        return gh(f"/users/{owner}/repos?type=public&sort=created&direction=desc&per_page=100") or []


def scan_fixed_orgs(cutoff: dt.datetime) -> list[dict]:
    out = []
    for owner in FIXED_OWNERS:
        for repo in owner_repos(owner):
            created = repo.get("created_at")
            if not created or parse_iso(created) < cutoff:
                continue
            out.append(
                {
                    "key": f"repo:{repo['id']}",
                    "source": "FIXED_ORG_NEW_REPO",
                    "stable_id": int(repo["id"]),
                    "display": repo["full_name"],
                    "url": repo["html_url"],
                    "created_at": created,
                    "metadata": {
                        "fixed_owner": owner,
                        "full_name": repo["full_name"],
                        "description": repo.get("description"),
                        "homepage": repo.get("homepage"),
                        "pushed_at": repo.get("pushed_at"),
                    },
                }
            )
    return out


def scan_zcg(cutoff: dt.datetime) -> list[dict]:
    rows = gh("/repos/ZcashCommunityGrants/zcashcommunitygrants/issues?state=all&sort=created&direction=desc&per_page=100") or []
    out = []
    for issue in rows:
        if issue.get("pull_request"):
            continue
        created = issue.get("created_at")
        if not created or parse_iso(created) < cutoff:
            continue
        out.append(
            {
                "key": f"zcg:{issue['id']}",
                "source": "ZCG_NEW_ISSUE",
                "stable_id": int(issue["id"]),
                "display": f"#{issue['number']} {issue.get('title') or ''}".strip(),
                "url": issue["html_url"],
                "created_at": created,
                "metadata": {
                    "issue_number": int(issue["number"]),
                    "title": issue.get("title"),
                    "labels": [x.get("name") for x in issue.get("labels") or []],
                    "author": (issue.get("user") or {}).get("login"),
                },
            }
        )
    return out


def forum_page(page: int):
    params = urllib.parse.urlencode({"order": "created", "page": page})
    try:
        return web_json(f"{FORUM}/latest.json?{params}")
    except Exception:
        # Standard Discourse latest endpoint fallback.
        return web_json(f"{FORUM}/latest.json?page={page}")


def scan_forum(cutoff: dt.datetime) -> list[dict]:
    out: dict[int, dict] = {}
    for page in range(0, 3):
        obj = forum_page(page) or {}
        topics = ((obj.get("topic_list") or {}).get("topics") or [])
        if not topics:
            break
        for topic in topics:
            created = topic.get("created_at")
            if not created or parse_iso(created) < cutoff:
                continue
            tid = int(topic["id"])
            slug = topic.get("slug") or "topic"
            out[tid] = {
                "key": f"forum:{tid}",
                "source": "FORUM_NEW_TOPIC",
                "stable_id": tid,
                "display": f"{tid} {topic.get('title') or slug}",
                "url": f"{FORUM}/t/{slug}/{tid}",
                "created_at": created,
                "metadata": {
                    "topic_id": tid,
                    "slug": slug,
                    "title": topic.get("title"),
                    "category_id": topic.get("category_id"),
                    "posters": [((p.get("user") or {}).get("username") or p.get("description")) for p in topic.get("posters") or []],
                },
            }
    return list(out.values())


def existing_event(key: str) -> str | None:
    marker = f"zcash-fast-key:{key}"
    q = f'repo:{SENSOR_REPO} in:body "{marker}"'
    qs = urllib.parse.urlencode({"q": q, "per_page": 10})
    try:
        res = gh(f"/search/issues?{qs}") or {}
    except Exception:
        return None
    for item in res.get("items", []):
        if marker in (item.get("body") or ""):
            return item.get("html_url")
    return None


def source_tag(source: str) -> str:
    return {
        "GITHUB_NEW_REPO": "NEW_REPO",
        "FIXED_ORG_NEW_REPO": "FIXED_ORG",
        "ZCG_NEW_ISSUE": "ZCG",
        "FORUM_NEW_TOPIC": "FORUM",
    }[source]


def candidate_body(c: dict) -> str:
    event_json = json.dumps(c, ensure_ascii=False, sort_keys=True)
    return (
        "## Zcash Fast Sensor candidate\n\n"
        f"**Source:** {c['source']}  \n"
        f"**Created:** {c['created_at']}  \n"
        f"**Detected:** {c['detected_at']}  \n"
        f"**URL:** {c['url']}  \n"
        f"**Candidate:** {c['display']}\n\n"
        f"`zcash-fast-key:{c['key']}`\n\n"
        "<!-- ZCASH_FAST_EVENT\n"
        f"{event_json}\n"
        "-->\n\n---\n"
        "Machine discovery only. AI must verify whether this is a genuinely new project/identity or a meaningful technical event before notifying the user.\n"
    )


def make_pr_event(c: dict) -> str:
    base_ref = gh(f"/repos/{SENSOR_REPO}/git/ref/heads/{urllib.parse.quote(BASE_BRANCH, safe='')}")
    base_sha = base_ref["object"]["sha"]
    stamp = c["detected_at"].replace(":", "").replace("-", "")
    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", c["key"])[-80:]
    branch = f"zcash-fast/{safe_id}-{stamp}"
    gh(f"/repos/{SENSOR_REPO}/git/refs", "POST", {"ref": f"refs/heads/{branch}", "sha": base_sha})

    event = {"schema": "zcash-fast-event/1", "candidate": c}
    content = base64.b64encode((json.dumps(event, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode()).decode("ascii")
    path = f"events/zcash/{safe_id}-{stamp}.json"
    gh(
        f"/repos/{SENSOR_REPO}/contents/{urllib.parse.quote(path, safe='/')}",
        "PUT",
        {"message": f"ZCASH-FAST event: {c['display'][:80]}", "content": content, "branch": branch},
    )

    display = c["display"].replace("\n", " ")[:180]
    title = f"[ZCASH-FAST][{source_tag(c['source'])}] {display}"
    pr = gh(
        f"/repos/{SENSOR_REPO}/pulls",
        "POST",
        {"title": title, "body": candidate_body(c), "head": branch, "base": BASE_BRANCH, "draft": False},
    )
    return pr.get("html_url", "") if isinstance(pr, dict) else ""


def notify(c: dict) -> tuple[str, str]:
    existing = existing_event(c["key"])
    if existing:
        return "existing", existing
    return "pull_request", make_pr_event(c)


def main() -> int:
    if not TOKEN or not SENSOR_REPO:
        print("GITHUB_TOKEN and SENSOR_REPO are required", file=sys.stderr)
        return 2

    run_at = now()
    state = load_state(run_at)
    source_functions = {
        "github": scan_github,
        "fixed_orgs": scan_fixed_orgs,
        "zcg": scan_zcg,
        "forum": scan_forum,
    }
    discovered: dict[str, dict] = {}
    source_status: dict[str, dict] = {}
    failures: list[str] = []

    for source, fn in source_functions.items():
        cutoff = source_cutoff(state, source, run_at)
        try:
            rows = fn(cutoff)
            for c in rows:
                c["detected_at"] = iso(run_at)
                old = discovered.get(c["key"])
                # Prefer a fixed-org source label when the same repository is also found globally.
                if old is None or c["source"] == "FIXED_ORG_NEW_REPO":
                    discovered[c["key"]] = c
            state["watermarks"][source] = iso(run_at)
            source_status[source] = {"status": "PASS", "cutoff": iso(cutoff), "matches": len(rows)}
        except Exception as e:
            msg = f"{source}: {e}"
            failures.append(msg)
            source_status[source] = {"status": "FAIL", "cutoff": iso(cutoff), "error": str(e)[:1000]}
            print(f"ZCASH_FAST_SOURCE_ERROR {msg}", file=sys.stderr)

    fresh = [c for k, c in discovered.items() if k not in state["seen"]]
    fresh.sort(key=lambda c: c["created_at"], reverse=True)

    notify_failures = []
    emitted = 0
    for c in fresh[:MAX_CANDIDATES]:
        try:
            event_type, event_url = notify(c)
            state["seen"][c["key"]] = {
                "source": c["source"],
                "display": c["display"],
                "created_at": c["created_at"],
                "first_seen_at": c["detected_at"],
                "event_type": event_type,
                "event_url": event_url,
            }
            emitted += 1
            print(f"ZCASH_FAST_NEW source={c['source']} candidate={c['display']!r} event={event_type} url={event_url}")
        except Exception as e:
            msg = f"{c['key']}: {e}"
            notify_failures.append(msg)
            print(f"ZCASH_FAST_NOTIFY_ERROR {msg}", file=sys.stderr)

    state["last_run_at"] = iso(run_at)
    state["last_source_status"] = source_status
    save_state(state)

    failures.extend(notify_failures)
    print(json.dumps({
        "run_at": iso(run_at),
        "sources": source_status,
        "unique_matches": len(discovered),
        "fresh_candidates": len(fresh),
        "emitted": emitted,
        "failures": failures,
    }, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
