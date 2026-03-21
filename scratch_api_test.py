# scratch_api_test.py
"""Phase 0: Validate the API connection.

Run from repo root. Saves real API responses for inspection.
Copy output into tests/fixtures/*.json before writing model code.
"""
import json
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

headers = {
    "Authorization": f"Bearer {os.environ['UW_API_TOKEN']}",
    "UW-CLIENT-API-ID": "100001",
}

# 1. Hit flow-alerts
r = httpx.get(
    "https://api.unusualwhales.com/api/option-trades/flow-alerts",
    headers=headers,
    params={"limit": 5, "is_otm": "true"},
)
print("=== flow-alerts ===")
print(r.status_code)
data = r.json()
if data.get("data"):
    print(json.dumps(data["data"][0], indent=2))
else:
    print(json.dumps(data, indent=2))

# 2. Hit darkpool/recent
r2 = httpx.get("https://api.unusualwhales.com/api/darkpool/recent", headers=headers)
print("\n=== darkpool/recent ===")
data2 = r2.json()
if data2.get("data"):
    print(json.dumps(data2["data"][0], indent=2))
else:
    print(json.dumps(data2, indent=2))

# 3. Hit market-tide
r3 = httpx.get("https://api.unusualwhales.com/api/market/market-tide", headers=headers)
print("\n=== market-tide ===")
data3 = r3.json()
if data3.get("data"):
    print(json.dumps(data3["data"][0], indent=2))
else:
    print(json.dumps(data3, indent=2))
