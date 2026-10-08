"""Stand-ins for everything around the two services under test, on one port.

    GET  /keys/{reference_id}         Partner Management key fetch
    POST /dci/registry/sync/search    a farmer registry that really asks the CM
    POST /callback                    the partner's on-search receiver
    GET  /received                    what the callback has been sent

The registry is the part that matters: it validates the aggregator's consent
JWS with the CM's /validate exactly as a real registry does, so a fetch only
succeeds if the grant recorded through POST /consent/v1/grants is really there.

    E2E_KEYS=<dir of *.pub.pem> E2E_CM=http://127.0.0.1:18000 \
        uvicorn fakes:app --port 18090
"""
import os
import pathlib

import httpx
import jwt
from fastapi import FastAPI, Request

KEYS = pathlib.Path(os.environ["E2E_KEYS"])
CM = os.environ["E2E_CM"]

# reference_id -> kid. File <reference_id>.pub.pem holds the key.
KIDS = {"PARTNER_X": "k1", "PARTNER_Y": "k1", "PARTNER_AGGREGATION_LAYER": "agg-2026-01"}

RECORD = {
    "farmer_personal_details": {
        "demographic_info": {
            "name": {"given_name": "Mary", "second_name": "A", "surname": "Bell"},
            "phone_number": "0911000055",
        },
    },
}

app = FastAPI()
received = []


@app.get("/keys/{reference_id}")
async def keys(reference_id: str):
    path = KEYS / ("%s.pub.pem" % reference_id)
    if not path.exists():
        return {"keys": []}
    return {"keys": [{"kid": KIDS.get(reference_id, "k1"), "algorithm": "EdDSA",
                      "public_key": path.read_text()}]}


@app.post("/dci/registry/sync/search")
async def search(request: Request):
    body = await request.json()
    item = body["message"]["search_request"][0]
    consent_jws = item["search_criteria"]["authorize"]["consent_jws"]
    claims = jwt.decode(consent_jws, options={"verify_signature": False})
    async with httpx.AsyncClient(timeout=10) as client:
        decision = (await client.post(CM + "/consent/v1/validate", json={
            "consent_jws": consent_jws,
            "request_context": {"requested_scopes": claims.get("data_scopes", [])},
        })).json()
    header = {"action": "on-search", "status": "succ"}
    if decision.get("decision") != "permit":
        header.update(status="rjct", status_reason_code=str(decision.get("reason_code")),
                      status_reason_message=decision.get("detail") or "")
        return {"header": header, "message": {"search_response": []}}
    allowed = set(decision.get("effective_data_scopes") or [])
    record = {k: v for k, v in RECORD.items() if k in allowed}
    return {"header": header, "message": {"search_response": [{
        "reference_id": item["reference_id"], "status": "succ",
        "data": {"reg_records": [record]}}]}}


@app.post("/callback")
async def callback(request: Request):
    received.append(await request.json())
    return {"ok": True}


@app.get("/received")
async def get_received():
    return received
