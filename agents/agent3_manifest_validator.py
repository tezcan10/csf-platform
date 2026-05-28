#!/usr/bin/env python3
"""Agent 3 — Manifest Validator: validate k8s manifests are safe, then hand off to Agent 4.
Usage: python agent3_manifest_validator.py '{"app_name":"csf-app","app_url":"https://20.31.233.253"}'"""

import glob, json, logging, os, subprocess, sys
import anthropic

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
log = logging.getLogger("agent3")

MODEL   = "claude-opus-4-7"
K8S_DIR = "infrastructure/csf/k8s"
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

def list_manifests(directory):
    files = sorted(glob.glob(f"{directory}/**/*.yml",  recursive=True) +
                   glob.glob(f"{directory}/**/*.yaml", recursive=True))
    return {"directory": directory, "files": files, "count": len(files)}

def read_manifest(path):
    try:
        return {"path": path, "content": open(path).read()}
    except OSError as e:
        return {"error": str(e)}

def kubectl_dry_run(path):
    r = subprocess.run(["kubectl", "apply", "--dry-run=server", "-f", path],
                       capture_output=True, text=True)
    return {"path": path, "ok": r.returncode == 0, "output": (r.stdout or r.stderr).strip()}

def kubectl_diff(path):
    r = subprocess.run(["kubectl", "diff", "-f", path], capture_output=True, text=True)
    # exit code 1 = diff exists (not an error); >1 = real error
    return {"path": path, "has_changes": r.returncode == 1, "ok": r.returncode in (0, 1),
            "diff": r.stdout.strip() or "(no changes)",
            "error": r.stderr.strip() if r.returncode > 1 else None}

DISPATCH = {
    "list_manifests":  lambda i: list_manifests(i["directory"]),
    "read_manifest":   lambda i: read_manifest(i["path"]),
    "kubectl_dry_run": lambda i: kubectl_dry_run(i["path"]),
    "kubectl_diff":    lambda i: kubectl_diff(i["path"]),
}

TOOLS = [
    {"name": "list_manifests",
     "description": "List all YAML manifest files in a directory (recursive).",
     "input_schema": {"type": "object", "properties": {"directory": {"type": "string"}}, "required": ["directory"]}},
    {"name": "read_manifest",
     "description": "Read a manifest. Inspect for: resource limits, namespace set, no :latest image tag.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "kubectl_dry_run",
     "description": "Server-side dry-run. Catches API errors, wrong apiVersion, RBAC issues. ok=false is a hard failure.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "kubectl_diff",
     "description": "Show what would change in the live cluster. ok=true even when changes exist.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
]

SYSTEM = """You are Agent 3 — Kubernetes Manifest Validator. One job: validate manifests are safe, then report.

1. LIST    list_manifests(directory="infrastructure/csf/k8s").
2. READ    read_manifest on each file. Flag: missing resource limits, image tag :latest, missing namespace.
           Extract spec.replicas from any Deployment manifest — include as expected_replicas in the report.
3. DRY-RUN kubectl_dry_run on each file. ok=false is a hard failure → status=failure.
4. DIFF    kubectl_diff on each file. Summarise what would change.
5. REPORT  output JSON as final message:
   {"agent":"agent3","status":"success"|"failure","app_name":"...","app_url":"...",
    "expected_replicas":N,"findings":["warnings or empty"],"message":"one-line summary"}

Findings are warnings only — don't block on best-practice issues alone. Default app_name=csf-app."""

def run(ctx):
    client   = anthropic.Anthropic()
    audit    = []
    messages = [{"role": "user", "content":
        f"Validate manifests for {ctx.get('app_name','csf-app')}.\nContext: {json.dumps(ctx)}\nBegin."}]

    for _ in range(40):
        resp = client.messages.create(model=MODEL, max_tokens=4096,
            thinking={"type": "adaptive"}, system=SYSTEM, tools=TOOLS, messages=messages)
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "end_turn":
            final = next((b.text for b in resp.content if b.type == "text"), "{}")
            s, e  = final.rfind("{"), final.rfind("}") + 1
            result = json.loads(final[s:e]) if s != -1 and e > s else {"status": "unknown"}
            result.setdefault("app_url",          ctx.get("app_url"))           # forward for Agent 4
            result.setdefault("commit_sha",       ctx.get("commit_sha"))        # forward for Agent 4 revision check
            result.setdefault("expected_replicas", ctx.get("expected_replicas")) # forward if already set
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
    ctx = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {
        "agent": "agent2", "status": "success",
        "app_name": "csf-app", "app_url": "https://20.31.233.253",
        "message": "Infrastructure ready.",
    }
    result = run(ctx)
    os.makedirs(LOG_DIR, exist_ok=True)
    open(os.path.join(LOG_DIR, "agent3_last.json"), "w").write(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    sys.exit(0 if result.get("status") == "success" else 1)

if __name__ == "__main__":
    main()
