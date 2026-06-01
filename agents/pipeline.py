#!/usr/bin/env python3
"""
Pipeline runner — chains Agent 1 → 3 → 4 based on routing decision.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    export GITHUB_TOKEN=ghp_...
    python agents/pipeline.py
"""

import json, logging, os, subprocess, sys

# Import agent modules directly so we call run() without subprocess overhead
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent1_repo_watcher        as a1
import agent2_terraform_runner     as a2
import agent3_manifest_validator   as a3
import agent4_argocd_monitor       as a4

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("pipeline")

LOG_DIR    = os.getenv("LOG_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"))
IN_CLUSTER = os.getenv("KUBERNETES_SERVICE_HOST") is not None   # true when running as a pod

def get_app_url():
    """Return APP_URL env var if set, otherwise look up the LoadBalancer IP from kubectl."""
    if os.getenv("APP_URL"):
        return os.getenv("APP_URL")
    try:
        r = subprocess.run(
            ["kubectl", "get", "svc", "csf-app-svc", "-n", "default",
             "-o", "jsonpath={.status.loadBalancer.ingress[0].ip}"],
            capture_output=True, text=True, timeout=10)
        ip = r.stdout.strip()
        return f"https://{ip}" if ip else "https://pending"
    except Exception:
        return "https://pending"

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

def ensure_gitops_checkout():
    """When running in-cluster, clone/pull csf-gitops and return the k8s dir path."""
    if not IN_CLUSTER:
        return "infrastructure/csf/k8s"  # locally, manifests are on disk
    clone_dir = "/tmp/csf-gitops"
    token = os.getenv("GITHUB_TOKEN", "")
    repo_url = f"https://{token}@github.com/tezcan10/csf-gitops.git"
    if os.path.isdir(os.path.join(clone_dir, ".git")):
        r = subprocess.run(["git", "-C", clone_dir, "pull", "--ff-only"],
                           capture_output=True, text=True)
        log.info("git pull csf-gitops: %s", (r.stdout + r.stderr).strip()[:80])
    else:
        r = subprocess.run(["git", "clone", "--depth=1", repo_url, clone_dir],
                           capture_output=True, text=True)
        log.info("git clone csf-gitops: %s", (r.stdout + r.stderr).strip()[:80])
        if r.returncode != 0:
            log.error("git clone failed: %s", r.stderr.strip())
    return os.path.join(clone_dir, "k8s")

def run_agent3(ctx):
    ctx["k8s_dir"] = ensure_gitops_checkout()
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
        if IN_CLUSTER:
            log.warning("▶  Agent 2 skipped — running in-cluster; run Agent 2 manually from laptop to provision infrastructure.")
        else:
            log.info("▶  Agent 2 — Terraform runner")
            r2 = run_agent2(r1)
            log.info("   status=%s  nodes=%s", r2.get("status"), r2.get("nodes_ready", "?"))
            if r2.get("status") != "success":
                log.error("   Terraform FAILED — halting pipeline.")
                sys.exit(1)
            r1.update({k: r2[k] for k in ("acr_server","aks_name","resource_group") if k in r2})

    # ── Agent 3 ───────────────────────────────
    if "agent3" in route:
        r1["app_url"] = get_app_url()   # inject so it flows through to Agent 4
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
