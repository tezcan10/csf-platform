#!/usr/bin/env python3
"""
Pipeline runner — chains Agent 1 → 3 → 4 based on routing decision.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    export GITHUB_TOKEN=ghp_...
    python agents/pipeline.py
"""

import json, logging, os, sys

# Import agent modules directly so we call run() without subprocess overhead
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent1_repo_watcher        as a1
import agent2_terraform_runner     as a2
import agent3_manifest_validator   as a3
import agent4_argocd_monitor       as a4

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("pipeline")

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
APP_URL = os.getenv("APP_URL", "https://20.82.73.181")

def write_log(name, data):
    os.makedirs(LOG_DIR, exist_ok=True)
    open(os.path.join(LOG_DIR, f"{name}_last.json"), "w").write(json.dumps(data, indent=2))

def run_agent1():
    last_sha = open(a1.STATE_FILE).read().strip() if os.path.exists(a1.STATE_FILE) else None
    head = a1.get_latest_commit(a1.OWNER, a1.REPO, a1.BRANCH)
    if "error" in head:
        log.error("Agent 1 — GitHub error: %s", head["error"]); sys.exit(1)
    current_sha = head["sha"]
    if current_sha == last_sha:
        result = {"agent": "agent1", "status": "no_change", "commit_sha": current_sha}
    else:
        result = a1.run(current_sha, last_sha)
        open(a1.STATE_FILE, "w").write(current_sha)
    write_log("agent1", result)
    return result

def run_agent2(ctx):
    result = a2.run(ctx)
    write_log("agent2", result)
    return result

def run_agent3(ctx):
    result = a3.run(ctx)
    write_log("agent3", result)
    return result

def run_agent4(ctx):
    result = a4.run(ctx)
    write_log("agent4", result)
    return result

def main():
    log.info("══════════════════════════════════════")
    log.info("  CSF Pipeline")
    log.info("══════════════════════════════════════")

    # ── Agent 1 ───────────────────────────────
    log.info("▶  Agent 1 — repo watcher")
    r1 = run_agent1()
    log.info("   status=%s  route=%s", r1.get("status"), r1.get("route_to", "—"))

    if r1.get("status") == "no_change":
        log.info("   No new commits — pipeline idle.")
        sys.exit(0)

    route = r1.get("route_to", "none")

    if route == "none":
        log.info("   App-code only change — ArgoCD handles via image tag.")
        sys.exit(0)

    # ── Agent 2 ───────────────────────────────
    if "agent2" in route:
        log.info("▶  Agent 2 — Terraform runner")
        r2 = run_agent2(r1)
        log.info("   status=%s  nodes=%s", r2.get("status"), r2.get("nodes_ready", "?"))
        if r2.get("status") != "success":
            log.error("   Terraform FAILED — halting pipeline.")
            sys.exit(1)
        r1.update({k: r2[k] for k in ("acr_server","aks_name","resource_group") if k in r2})

    # ── Agent 3 ───────────────────────────────
    if "agent3" in route:
        r1["app_url"] = APP_URL   # inject so it flows through to Agent 4
        log.info("▶  Agent 3 — manifest validator")
        r3 = run_agent3(r1)
        log.info("   status=%s  findings=%d", r3.get("status"), len(r3.get("findings", [])))
        if r3.get("status") != "success":
            log.error("   Validation FAILED — halting pipeline.")
            sys.exit(1)
    else:
        r3 = r1

    # ── Agent 4 ───────────────────────────────
    log.info("▶  Agent 4 — ArgoCD monitor")
    r4 = run_agent4(r3)
    log.info("   status=%s  pods=%s/%s  http=%s",
             r4.get("status"), r4.get("ready_pods","?"),
             r4.get("total_pods","?"), r4.get("health_check_ok"))

    if r4.get("status") == "success":
        log.info("══ Pipeline complete — deployment healthy ══")
        sys.exit(0)
    else:
        log.error("══ Pipeline FAILED — %s ══", r4.get("message",""))
        sys.exit(1)

if __name__ == "__main__":
    main()
