#!/usr/bin/env python3
"""Agent 4 — ArgoCD Deployment Monitor: confirm success or roll back.
Usage: python agent4_argocd_monitor.py '{"app_name":"csf-app","app_url":"https://1.2.3.4"}'"""

import json, logging, os, subprocess, sys, time
import anthropic, requests

requests.packages.urllib3.disable_warnings()
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("agent4")

MODEL     = "claude-opus-4-7"
ARGOCD_NS = "argocd"
POLL_WAIT = 10
LOG_DIR   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

def _kubectl(args):
    r = subprocess.run(["kubectl"] + args, capture_output=True, text=True, timeout=30)
    return json.loads(r.stdout) if r.returncode == 0 else {"error": r.stderr.strip()}

def wait_for_deployment(app_name, expected_revision=None, expected_replicas=None,
                        namespace="default", label_selector="app=csf-app"):
    """Poll ArgoCD until revision+Synced+Healthy, then poll pods until expected_replicas ready."""
    sync_status = health_status = revision = "Unknown"

    # Phase 1 — wait for ArgoCD to sync the right revision
    for i in range(30):   # 30 × 10s = 5 min (ArgoCD default poll cycle is 3 min)
        time.sleep(POLL_WAIT)
        d = _kubectl(["get", "application", app_name, "-n", ARGOCD_NS, "-o", "json"])
        if "error" in d:
            log.info("  argocd poll %d: error %s", i+1, d["error"])
            continue
        s = d.get("status", {})
        sync_status   = s.get("sync",   {}).get("status", "Unknown")
        health_status = s.get("health", {}).get("status", "Unknown")
        revision      = s.get("sync",   {}).get("revision")
        # Accept both full SHA and short SHA (prefix match in either direction)
        rev_ok = (not expected_revision) or (revision == expected_revision) or \
                 (revision or "").startswith(expected_revision or "") or \
                 (expected_revision or "").startswith(revision or "")
        log.info("  argocd poll %d: %s/%s rev_ok=%s", i+1, sync_status, health_status, rev_ok)
        if sync_status == "Synced" and health_status == "Healthy" and rev_ok:
            break
    else:
        return {"ok": False, "phase": "argocd_sync",
                "error": f"ArgoCD did not become Synced+Healthy on revision {expected_revision} within 5 min",
                "sync_status": sync_status, "health_status": health_status, "revision": revision}

    # Phase 2 — wait for pods to reach expected count
    ready = total = 0
    for i in range(18):
        d = _kubectl(["get", "pods", "-n", namespace, "-l", label_selector, "-o", "json"])
        if "error" not in d:
            pods  = d.get("items", [])
            total = len(pods)
            ready = sum(1 for p in pods if any(
                c.get("type") == "Ready" and c.get("status") == "True"
                for c in p.get("status", {}).get("conditions", [])))
            target = expected_replicas if expected_replicas else total
            log.info("  pods poll %d: %d/%d (target %s)", i+1, ready, total, target)
            if ready == target and ready > 0:
                return {"ok": True, "sync_status": sync_status, "health_status": health_status,
                        "revision": revision, "ready_pods": ready, "total_pods": total}
        time.sleep(POLL_WAIT)

    return {"ok": False, "phase": "pod_readiness",
            "error": f"Pods did not converge to {expected_replicas} within 3 min (last: {ready}/{total})",
            "sync_status": sync_status, "health_status": health_status,
            "ready_pods": ready, "total_pods": total}

def http_health_check(url, timeout_sec=15):
    try:
        r = requests.get(url, timeout=timeout_sec, verify=False, allow_redirects=True)
        return {"ok": r.status_code < 400, "status_code": r.status_code}
    except requests.exceptions.RequestException as e:
        return {"ok": False, "error": str(e)}

def argocd_rollback(app_name, revision=""):
    try:
        r = subprocess.run(["argocd", "app", "rollback", app_name,
                            "--server", "localhost:8080", "--insecure"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            return {"success": True, "message": f"Rolled back {app_name}"}
    except FileNotFoundError:
        pass
    patch = json.dumps({"spec": {"syncPolicy": {"automated": None}}})
    r = subprocess.run(["kubectl", "patch", "application", app_name,
                        "-n", ARGOCD_NS, "--type=merge", "-p", patch],
                       capture_output=True, text=True, timeout=30)
    return {"success": r.returncode == 0,
            "message": "Auto-sync disabled — revert the git commit to complete rollback."}

DISPATCH = {
    "wait_for_deployment": lambda i: wait_for_deployment(
        i["app_name"], i.get("expected_revision"), i.get("expected_replicas"),
        i.get("namespace", "default"), i.get("label_selector", "app=csf-app")),
    "http_health_check":   lambda i: http_health_check(i["url"], i.get("timeout_sec", 15)),
    "argocd_rollback":     lambda i: argocd_rollback(i["app_name"], i.get("revision", "")),
}

TOOLS = [
    {"name": "wait_for_deployment",
     "description": "Wait for ArgoCD to sync the expected revision AND for pods to reach expected_replicas. "
                    "Handles all polling internally. Returns ok=true only when both conditions are met. "
                    "Always pass expected_revision and expected_replicas from context.",
     "input_schema": {"type": "object",
                      "properties": {
                          "app_name":          {"type": "string"},
                          "expected_revision": {"type": "string"},
                          "expected_replicas": {"type": "integer"},
                          "namespace":         {"type": "string"},
                          "label_selector":    {"type": "string"},
                      },
                      "required": ["app_name"]}},
    {"name": "http_health_check",
     "description": "HTTP GET health check. ok=true means 2xx/3xx.",
     "input_schema": {"type": "object",
                      "properties": {"url": {"type": "string"}, "timeout_sec": {"type": "integer"}},
                      "required": ["url"]}},
    {"name": "argocd_rollback",
     "description": "Trigger rollback. Call ONLY when wait_for_deployment or http_health_check returned ok=false.",
     "input_schema": {"type": "object",
                      "properties": {"app_name": {"type": "string"}, "revision": {"type": "string"}},
                      "required": ["app_name"]}},
]

SYSTEM = """You are Agent 4 — ArgoCD Deployment Monitor. One job: confirm success or roll back.

1. WAIT     wait_for_deployment(app_name, expected_revision=commit_sha, expected_replicas=N from context).
            If ok=false → call argocd_rollback immediately, then go to step 4 with status=failure.
            Do NOT call http_health_check if wait_for_deployment returned ok=false.
2. HTTP     http_health_check(url from context). Skip if app_url="https://pending".
            If ok=false → call argocd_rollback, then go to step 4 with status=failure.
3. ROLLBACK argocd_rollback only if step 1 or step 2 explicitly failed.
4. REPORT   output JSON as final message:
   {"agent":"agent4","status":"success"|"failure","sync_status":"...","health_status":"...",
    "ready_pods":N,"total_pods":N,"health_check_ok":bool,"rollback_triggered":bool,"message":"..."}

Default app_name=csf-app. If app_url is "https://pending" set health_check_ok=null."""

def run(ctx):
    client   = anthropic.Anthropic()
    audit    = []
    messages = [{"role": "user", "content":
        f"Monitor deployment of {ctx.get('app_name','csf-app')}.\n"
        f"Context: {json.dumps(ctx)}\n"
        f"App URL: {ctx.get('app_url','https://pending')}\n"
        f"Expected replicas: {ctx.get('expected_replicas','unknown')}\n"
        f"Expected revision (commit_sha): {ctx.get('commit_sha','unknown')}\nBegin."}]

    for _ in range(20):
        resp = client.messages.create(model=MODEL, max_tokens=4096,
            thinking={"type": "adaptive"}, system=SYSTEM, tools=TOOLS, messages=messages)
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
                inp = dict(b.input)
                # inject context values so Claude can't accidentally omit them
                if b.name == "wait_for_deployment":
                    if ctx.get("commit_sha"):
                        inp.setdefault("expected_revision", ctx["commit_sha"])
                    if ctx.get("expected_replicas"):
                        inp.setdefault("expected_replicas", ctx["expected_replicas"])
                out = json.dumps(DISPATCH[b.name](inp), indent=2)
                audit.append({"tool": b.name, "input": inp, "output": json.loads(out)})
                log.info("  %s → %s", b.name, out[:120].replace("\n", " "))
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
            messages.append({"role": "user", "content": results})

    return {"status": "failure", "message": "iteration limit exceeded", "audit_log": audit}

def main():
    ctx = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {
        "agent": "agent3", "status": "success",
        "app_name": "csf-app", "app_url": "https://pending",
        "message": "Manifests applied.",
    }
    result = run(ctx)
    os.makedirs(LOG_DIR, exist_ok=True)
    open(os.path.join(LOG_DIR, "agent4_last.json"), "w").write(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("status") == "success" else 1)

if __name__ == "__main__":
    main()
