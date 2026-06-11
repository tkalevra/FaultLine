import sys, json, urllib.request, urllib.error, os

API_URL = os.environ.get("FAULTLINE_API_URL", "http://localhost:8002")
URL = API_URL.rstrip("/") + "/mcp"
KEY = os.environ.get("MCP_API_KEY", "")
USER_ID = os.environ.get("FAULTLINE_USER_ID", "")

def _dbg(msg):
    print(f"[faultline-proxy] {msg}", file=sys.stderr, flush=True)

_dbg(f"target={URL} key={'set('+str(len(KEY))+'chars)' if KEY else 'EMPTY'} user={USER_ID[:8]+'...' if USER_ID else 'EMPTY'}")

def post(payload):
    if USER_ID and "params" in payload and "arguments" in payload.get("params", {}):
        args = payload["params"]["arguments"]
        if "user_id" not in args:
            args["user_id"] = USER_ID
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if KEY:
        headers["Authorization"] = "Bearer " + KEY
    if USER_ID:
        headers["X-OpenWebUI-User-Id"] = USER_ID
    req = urllib.request.Request(URL, data=data, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return {"jsonrpc": "2.0", "error": {"code": e.code, "message": body}, "id": payload.get("id")}
    except Exception as e:
        return {"jsonrpc": "2.0", "error": {"code": -32000, "message": str(e)}, "id": payload.get("id")}

stdin = open(sys.stdin.fileno(), mode="rb", buffering=0)
stdout = open(sys.stdout.fileno(), mode="wb", buffering=0)

for raw in stdin:
    raw = raw.strip()
    if not raw:
        continue
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        continue
    is_notification = "method" in msg and "id" not in msg
    resp = post(msg)
    if is_notification:
        continue
    if resp.get("id") is None and msg.get("id") is not None:
        resp["id"] = msg["id"]
    stdout.write(json.dumps(resp).encode("utf-8") + b"\n")
    stdout.flush()
