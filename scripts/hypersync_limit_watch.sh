#!/bin/zsh
# Logs HyperSync's rate-limit headers hourly so we can see if the free-tier cap changes.
cd "$(dirname "$0")/.."
while true; do
  .venv/bin/python - <<'PY' >> data/hypersync_limit.log 2>&1
import os,time,json,urllib.request
from dotenv import load_dotenv; load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))
req=urllib.request.Request(os.environ["HYPERSYNC_URL"]+"/query",data=json.dumps({"from_block":1,"to_block":2,"include_all_blocks":True,"field_selection":{"block":["number"]}}).encode(),headers={"Authorization":"Bearer "+os.environ["HYPERSYNC_TOKEN"],"Content-Type":"application/json"})
try:
    r=urllib.request.urlopen(req,timeout=30); h=r.headers; st=r.status
except urllib.error.HTTPError as e:
    h=e.headers; st=e.code
except Exception as e:
    print(time.strftime("%Y-%m-%d %H:%M"),"ERR",repr(e)); raise SystemExit
print(time.strftime("%Y-%m-%d %H:%M"),st,{k:v for k,v in h.items() if k.lower().startswith("x-ratelimit")})
PY
  sleep 3600
done
