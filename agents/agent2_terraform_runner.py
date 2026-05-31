#!/usr/bin/env python3
"""Agent 2 — Terraform Runner: provision or update Azure infrastructure.

Usage:
    az login --use-device-code   # must be done first
    export ANTHROPIC_API_KEY=sk-ant-...
    python agents/agent2_terraform_runner.py
"""

import json, logging, os, subprocess, sys, time
import anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("agent2")

MODEL   = "claude-opus-4-7"
TF_DIR  = "infrastructure/csf"
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

def _run(cmd, timeout=600):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    out = (r.stdout + r.stderr).strip()
    return {"ok": r.returncode == 0, "output": out[-3000:]}  # truncate for Claude context

def run_setup_backend():
    return _run(["bash", f"{TF_DIR}/setup-backend.sh"], timeout=120)

def terraform_init(directory):
    return _run(["terraform", f"-chdir={directory}", "init", "-no-color"], timeout=120)

def terraform_plan(directory):
    return _run(["terraform", f"-chdir={directory}", "plan", "-no-color"], timeout=300)

def terraform_apply(directory):
    return _run(["terraform", f"-chdir={directory}", "apply", "-auto-approve", "-no-color"], timeout=600)

def connect_kubectl(resource_group, cluster_name):
    r1 = _run(["az", "aks", "get-credentials",
                "--resource-group", resource_group,
                "--name", cluster_name, "--overwrite-existing"], timeout=60)
    if not r1["ok"]:
        return r1
    r2 = _run(["kubectl", "get", "nodes"], timeout=30)
    node_count = r2["output"].count("\n")  # rough count from output lines
    return {"ok": r2["ok"], "output": r2["output"], "node_count": node_count}

def start_argocd_portforward(local_port=8888):
    """Wait for argocd-server pod to be ready, then start port-forward. Retries for up to 3 min."""
    subprocess.run(["pkill", "-f", "port-forward.*argocd-server"], capture_output=True)
    # Wait for argocd-server deployment to be available (up to 3 min on fresh cluster)
    subprocess.run(["kubectl", "wait", "--for=condition=available", "deployment/argocd-server",
                    "-n", "argocd", "--timeout=180s"], capture_output=True)
    subprocess.Popen(
        ["kubectl", "port-forward", "svc/argocd-server", "-n", "argocd",
         f"{local_port}:443", "--address", "127.0.0.1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Retry curl a few times to let the tunnel stabilise
    for i in range(6):
        time.sleep(3)
        r = _run(["curl", "-sk", f"https://127.0.0.1:{local_port}", "-o", "/dev/null",
                  "-w", "%{http_code}"], timeout=10)
        if r["output"].strip() == "200":
            return {"ok": True, "url": f"https://localhost:{local_port}", "status_code": "200"}
    return {"ok": False, "url": f"https://localhost:{local_port}", "status_code": r["output"].strip()}

DISPATCH = {
    "run_setup_backend": lambda i: run_setup_backend(),
    "terraform_init":    lambda i: terraform_init(i["directory"]),
    "terraform_plan":    lambda i: terraform_plan(i["directory"]),
    "terraform_apply":   lambda i: terraform_apply(i["directory"]),
    "connect_kubectl":          lambda i: connect_kubectl(i["resource_group"], i["cluster_name"]),
    "start_argocd_portforward": lambda i: start_argocd_portforward(i.get("local_port", 8888)),
}

TOOLS = [
    {"name": "run_setup_backend",
     "description": "Create Azure Blob Storage for Terraform state (idempotent — always safe to run).",
     "input_schema": {"type": "object", "properties": {}, "required": []}},
    {"name": "terraform_init",
     "description": "Run terraform init. Always run before plan or apply.",
     "input_schema": {"type": "object",
                      "properties": {"directory": {"type": "string"}},
                      "required": ["directory"]}},
    {"name": "terraform_plan",
     "description": "Run terraform plan. Read output carefully — stop if unexpected destroys appear.",
     "input_schema": {"type": "object",
                      "properties": {"directory": {"type": "string"}},
                      "required": ["directory"]}},
    {"name": "terraform_apply",
     "description": "Run terraform apply -auto-approve. Only call after reviewing the plan output.",
     "input_schema": {"type": "object",
                      "properties": {"directory": {"type": "string"}},
                      "required": ["directory"]}},
    {"name": "connect_kubectl",
     "description": "Run az aks get-credentials and verify kubectl can reach the cluster nodes.",
     "input_schema": {"type": "object",
                      "properties": {"resource_group": {"type": "string"},
                                     "cluster_name":   {"type": "string"}},
                      "required": ["resource_group", "cluster_name"]}},
    {"name": "start_argocd_portforward",
     "description": "Kill any existing argocd port-forward and start a fresh one on local_port (default 8888). Verifies with HTTP 200.",
     "input_schema": {"type": "object",
                      "properties": {"local_port": {"type": "integer"}},
                      "required": []}},
]

SYSTEM = f"""You are Agent 2 — Terraform Runner. One job: provision or update Azure infrastructure.

Terraform directory: {TF_DIR}
AKS cluster: aks-csf-demo    Resource group: rg-csf-demo    ACR: acrcsfdemo.azurecr.io

Steps (always in this order):
1. BACKEND  run_setup_backend — creates the Azure Blob Storage for TF state. Always safe to re-run.
2. INIT     terraform_init(directory="{TF_DIR}").
3. PLAN     terraform_plan(directory="{TF_DIR}"). Read the output.
            STOP and set status=failure if any unexpected resource destructions appear in the plan.
4. APPLY    terraform_apply(directory="{TF_DIR}") — only after plan looks correct.
5. CONNECT  connect_kubectl(resource_group="rg-csf-demo", cluster_name="aks-csf-demo").
            Verify nodes appear in the output.
6. ARGOCD   start_argocd_portforward(local_port=8888).
            ArgoCD is for local access only — ok=false here is a WARNING, not a hard failure.
            Do NOT set status=failure if only the port-forward fails.
7. REPORT   output JSON as your final message:
            {{"agent":"agent2","status":"success|failure",
              "acr_server":"acrcsfdemo.azurecr.io","aks_name":"aks-csf-demo",
              "resource_group":"rg-csf-demo","app_url":"https://pending",
              "nodes_ready":N,"argocd_url":"https://localhost:8888","message":"one-line summary"}}

Rules: if any step returns ok=false, retry once. If it fails again, set status=failure and stop.
       Exception: start_argocd_portforward failure is a warning only — always set status=success
       as long as steps 1-5 succeeded."""

def run(ctx):
    client   = anthropic.Anthropic()
    audit    = []
    messages = [{"role": "user", "content":
        f"Provision Azure infrastructure.\nContext: {json.dumps(ctx)}\nBegin."}]

    for _ in range(30):
        resp = client.messages.create(
            model=MODEL, max_tokens=4096, thinking={"type": "adaptive"},
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
    r = subprocess.run(["az", "account", "show"], capture_output=True, text=True)
    if r.returncode != 0:
        log.error("Not logged in to Azure. Run: az login --use-device-code"); sys.exit(1)
    log.info("Azure login: OK")

    ctx = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {
        "agent": "agent1", "status": "change_detected",
        "route_to": "agent2", "message": "Infrastructure bootstrap requested.",
    }
    result = run(ctx)
    os.makedirs(LOG_DIR, exist_ok=True)
    open(os.path.join(LOG_DIR, "agent2_last.json"), "w").write(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("status") == "success" else 1)

if __name__ == "__main__":
    main()
