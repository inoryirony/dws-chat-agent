"""闸门测试用的最小 MCP server：三个工具 alpha/beta/gamma。"""
import json
import sys

for line in sys.stdin:
    try:
        msg = json.loads(line)
    except json.JSONDecodeError:
        continue
    if msg.get("id") == 1 and msg.get("method") == "initialize":
        print(json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "result": {"protocolVersion": "2024-11-05",
                       "capabilities": {"tools": {}},
                       "serverInfo": {"name": "fake", "version": "0"}},
        }), flush=True)
    elif msg.get("id") and msg.get("method") == "tools/list":
        print(json.dumps({
            "jsonrpc": "2.0", "id": msg["id"],
            "result": {"tools": [{"name": "alpha"}, {"name": "beta"}, {"name": "gamma"}]},
        }), flush=True)
    elif msg.get("id") and msg.get("method") == "tools/call":
        print(json.dumps({
            "jsonrpc": "2.0", "id": msg["id"],
            "result": {"ok": True, "tool": msg["params"]["name"]},
        }), flush=True)
