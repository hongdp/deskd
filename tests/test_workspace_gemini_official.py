"""Opt-in pinned official CLI -> Gemini adapter -> synthetic upstream acceptance.

Uses a new scratch HOME and a synthetic stdio MCP tool. No real model/key,
broker, existing daemon or role kernel isolation is involved in this proof.
"""

import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from deskd.workspace.installation import OFFICIAL_LINUX_X64_SHA256


def test_official_cli_completes_gemini_tool_roundtrip(tmp_path):
    executable = os.environ.get("DESKD_GEMINI_OFFICIAL_BINARY")
    if not executable:
        pytest.skip("requires explicitly supplied pinned official binary")
    binary = Path(executable)
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == OFFICIAL_LINUX_X64_SHA256
    from deskd.workspace.gemini import GeminiProxy

    for name in ("home", "codex", "tmp", "work"):
        (tmp_path / name).mkdir(mode=0o700)
    tool = tmp_path / "echo_mcp.py"
    receipt = tmp_path / "tool-receipt.json"
    tool.write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "for line in sys.stdin:\n"
        " r=json.loads(line); method=r.get('method')\n"
        " if 'id' not in r: continue\n"
        " if method=='initialize': result={'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'synthetic','version':'1'}}\n"
        " elif method=='tools/list': result={'tools':[{'name':'echo','description':'SYNTHETIC_GEMINI_ECHO_PROBE','inputSchema':{'type':'object','properties':{'text':{'type':'string'}},'required':['text'],'additionalProperties':False}}]}\n"
        " elif method=='tools/call':\n"
        f"  Path({str(receipt)!r}).write_text(json.dumps(r['params']['arguments']))\n"
        "  result={'content':[{'type':'text','text':'SYNTHETIC_ECHO_RESULT'}]}\n"
        " else: result={}\n"
        " print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}),flush=True)\n"
    )
    requests = []
    signature = "U1lOVEhFVElDLVRIT1VHSFQtU0lHTkFUVVJF"

    def upstream(model, body, key):
        assert model == "gemini-3.8-flash"
        assert key == "SYNTHETIC_GOOGLE_KEY"
        requests.append(body)
        if len(requests) == 1:
            declarations = [
                declaration
                for group in body.get("tools", [])
                for declaration in group.get("functionDeclarations", [])
            ]
            echo = [d for d in declarations if "SYNTHETIC_GEMINI_ECHO_PROBE" in d.get("description", "")]
            assert len(echo) == 1, "official harness did not expose the synthetic MCP tool"
            parts = [{"functionCall": {"name": echo[0]["name"], "args": {"text": "SYNTHETIC_INPUT"}}, "thoughtSignature": signature}]
        else:
            assert len(requests) == 2, "unexpected provider retry or repeated tool call"
            encoded = json.dumps(body["contents"])
            assert "SYNTHETIC_ECHO_RESULT" in encoded
            assert signature in encoded, "thought signature was lost across official tool round trip"
            parts = [{"text": "SYNTHETIC_GEMINI_ROUNDTRIP_OK"}]
        yield {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 2, "totalTokenCount": 12}}

    with socket.socket() as selected:
        selected.bind(("127.0.0.1", 0))
        port = selected.getsockname()[1]
    proxy = GeminiProxy(port=port, model="gemini-3.8-flash", key_source=lambda: "SYNTHETIC_GOOGLE_KEY", authorize=lambda: None, upstream=upstream)
    proxy.start()
    token = proxy.token()
    assert token != "SYNTHETIC_GOOGLE_KEY"
    config = f'''
model = "gemini-3.8-flash"
model_provider = "deskd_gemini"
model_reasoning_effort = "medium"
approval_policy = "never"
sandbox_mode = "read-only"
web_search = "disabled"
[features]
plugins = false
apps = false
hooks = false
image_generation = false
goals = false
memories = false
external_agent_memory_import = false
browser_use = false
computer_use = false
remote_models = false
api_key_model_discovery = false
multi_agent = false
shell_snapshot = false
js_repl = false
code_mode = false
enable_request_compression = false
[model_providers.deskd_gemini]
name = "deskd Gemini synthetic acceptance"
base_url = "http://127.0.0.1:{port}/v1"
wire_api = "responses"
env_key = "SYNTHETIC_DESKD_MODEL_CAPABILITY"
requires_openai_auth = false
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0
[mcp_servers.synthetic]
command = {json.dumps(sys.executable)}
args = ["-I", "-S", {json.dumps(str(tool))}]
[mcp_servers.synthetic.tools.echo]
approval_mode = "approve"
[mcp_servers.codex_tui]
command = "/bin/false"
enabled = false
[analytics]
enabled = false
[memories]
generate_memories = false
use_memories = false
'''
    (tmp_path / "codex/config.toml").write_text(config)
    # Exercise the same private auth-command mechanism as the installed
    # gateway helper, using only a synthetic capability in a fresh environment.
    auth = tmp_path / "synthetic_auth.py"
    auth.write_text("import os,sys\nsys.stdout.write(os.environ['SYNTHETIC_DESKD_MODEL_CAPABILITY']+'\\n')\n")
    config = config.replace('env_key = "SYNTHETIC_DESKD_MODEL_CAPABILITY"\n', '')
    config += f'''\n[model_providers.deskd_gemini.auth]
command = {json.dumps(sys.executable)}
args = ["-I", "-S", {json.dumps(str(auth))}]
cwd = {json.dumps(str(tmp_path / "work"))}
timeout_ms = 5000
refresh_interval_ms = 300000
'''
    (tmp_path / "codex/config.toml").write_text(config)
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "HOME": str(tmp_path / "home"),
        "CODEX_HOME": str(tmp_path / "codex"),
        "TMPDIR": str(tmp_path / "tmp"),
        "SYNTHETIC_DESKD_MODEL_CAPABILITY": token,
    }
    try:
        result = subprocess.run(
            [str(binary), "exec", "--json", "--ephemeral", "--skip-git-repo-check", "-C", str(tmp_path / "work"), "Run the synthetic MCP echo tool once, then report its result."],
            env=environment, cwd=tmp_path / "work", capture_output=True, text=True, timeout=60,
        )
        (tmp_path / "official.stdout").write_text(result.stdout)
        (tmp_path / "official.stderr").write_text(result.stderr)
        assert result.returncode == 0, result.stderr[-3000:]
        assert len(requests) == 2, result.stderr[-3000:]
        assert json.loads(receipt.read_text()) == {"text": "SYNTHETIC_INPUT"}
        assert "SYNTHETIC_GEMINI_ROUNDTRIP_OK" in result.stdout
        assert "SYNTHETIC_GOOGLE_KEY" not in result.stdout + result.stderr
    finally:
        proxy.close()
