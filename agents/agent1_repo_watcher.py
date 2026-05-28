#!/usr/bin/env python3
"""Agent 1 — GitHub Repo Watcher: detect new commits, classify changes, decide routing.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...  GITHUB_TOKEN=ghp_...
    python agent1_repo_watcher.py
"""

import json, logging, os, sys
import anthropic, requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("agent1")

MODEL      = "claude-opus-4-7"
STATE_FILE = os.path.join(os.path.dirname(__file__), ".agent1_state")
LOG_DIR    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
GH_API     = "https://api.github.com"
OWNER      = os.getenv("GITHUB_OWNER",  "tezcan10")
REPO       = os.getenv("GITHUB_REPO",   "csf-gitops")
BRANCH     = os.getenv("GITHUB_BRANCH", "main")

def _gh(path):
    h = {"Authorization": f"Bearer {os.getenv('GITHUB_TOKEN', '')}",
         "Accept": "application/vnd.github+json"}
    r = requests.get(f"{GH_API}{path}", headers=h, timeout=15)
    return r.json() if r.ok else {"error": f"HTTP {r.status_code}"}

def get_latest_commit(owner, repo, branch):
    d = _gh(f"/repos/{owner}/{repo}/commits/{branch}")
    if "error" in d: return d
    return {"sha": d["sha"],
            "message": d["commit"]["message"].splitlines()[0],
            "author":  d["commit"]["author"]["name"],
            "date":    d["commit"]["author"]["date"]}

def get_changed_files(owner, repo, sha):
    d = _gh(f"/repos/{owner}/{repo}/commits/{sha}")
    if "error" in d: return d
    return {"sha": sha, "files": [f["filename"] for f in d.get("files", [])]}

DISPATCH = {
    "get_latest_commit": lambda i: get_latest_commit(i["owner"], i["repo"], i["branch"]),
    "get_changed_files": lambda i: get_changed_files(i["owner"], i["repo"], i["sha"]),
}

TOOLS = [
    {"name": "get_latest_commit",
     "description": "Get latest commit on a branch (sha, message, author, date).",
     "input_schema": {"type": "object",
                      "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}, "branch": {"type": "string"}},
                      "required": ["owner", "repo", "branch"]}},
    {"name": "get_changed_files",
     "description": "List files changed in a specific commit.",
     "input_schema": {"type": "object",
                      "properties": {"owner": {"type": "string"}, "repo": {"type": "string"}, "sha": {"type": "string"}},
                      "required": ["owner", "repo", "sha"]}},
]

SYSTEM = f"""You are Agent 1 — GitHub Repo Watcher. Repo: {OWNER}/{REPO} branch: {BRANCH}.

1. FETCH   get_latest_commit to confirm commit details.
2. FILES   get_changed_files for that sha.
3. CLASSIFY decide route_to from the changed file paths:
   infrastructure/csf/*.tf only      → "agent2"
   infrastructure/csf/k8s/*.yml only → "agent3"
   both tf and k8s                   → "agent2_then_agent3"
   Dockerfile or csf/** only         → "none"
4. REPORT  output JSON as your final message:
   {{"agent":"agent1","status":"change_detected","commit_sha":"...","commit_message":"...",
    "changed_files":[...],"route_to":"...","reason":"..."}}"""

def run(new_sha, last_sha):
    client   = anthropic.Anthropic()
    audit    = []
    messages = [{"role": "user", "content":
        f"New commit detected.\nLast SHA: {last_sha or 'none (first run)'}  New SHA: {new_sha}\n"
        f"Owner: {OWNER}  Repo: {REPO}  Branch: {BRANCH}\nClassify and route."}]

    for _ in range(20):
        resp = client.messages.create(
            model=MODEL, max_tokens=2048, thinking={"type": "adaptive"},
            system=SYSTEM, tools=TOOLS, messages=messages)
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "end_turn":
            final = next((b.text for b in resp.content if b.type == "text"), "{}")
            s, e  = final.rfind("{"), final.rfind("}") + 1
            result = json.loads(final[s:e]) if s != -1 and e > s else {"status": "unknown"}
            result["audit_log"] = audit
            return result

        if resp.stop_reason == "tool_use":
            results = []
            for b in resp.content:
                if b.type != "tool_use": continue
                out = json.dumps(DISPATCH[b.name](b.input), indent=2)
                audit.append({"tool": b.name, "input": b.input, "output": json.loads(out)})
                log.info("  %s → %s", b.name, out[:120].replace("\n", " "))
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
            messages.append({"role": "user", "content": results})

    return {"status": "failure", "message": "iteration limit exceeded", "audit_log": audit}

def main():
    last_sha = open(STATE_FILE).read().strip() if os.path.exists(STATE_FILE) else None
    head = get_latest_commit(OWNER, REPO, BRANCH)
    if "error" in head:
        log.error("GitHub API error: %s", head["error"]); sys.exit(1)

    current_sha = head["sha"]
    log.info("HEAD: %s  last: %s", current_sha[:12], (last_sha or "none")[:12])

    if current_sha == last_sha:
        result = {"agent": "agent1", "status": "no_change", "commit_sha": current_sha}
    else:
        result = run(current_sha, last_sha)
        open(STATE_FILE, "w").write(current_sha)

    os.makedirs(LOG_DIR, exist_ok=True)
    open(os.path.join(LOG_DIR, "agent1_last.json"), "w").write(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("status") != "failure" else 1)

if __name__ == "__main__":
    main()
