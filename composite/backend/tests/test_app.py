"""The FastAPI app: wiring, no database engine, describe endpoints, and a query.

Runs in a subprocess so the framework's process-wide registries (config,
components, app) start clean and the environment is set before import.
"""

import json
import os
import subprocess
import sys
import textwrap

from conftest import USE_CASES


def test_app_end_to_end(tmp_path, composite_p12):
    script = textwrap.dedent(
        f"""
        import json, sys, uuid, asyncio
        from datetime import datetime, timezone
        import httpx
        sys.path.insert(0, {os.path.dirname(__file__)!r})
        from conftest import Key, PolicyStub, RegistryStub, FR_URL, CSR_URL, FAN, SCOPES

        partner = Key("k1")
        from openg2p_agri_composite.services import composite_service as cs
        orig_init = cs.CompositeService.__init__
        registries = RegistryStub()
        policies = PolicyStub()
        def stub(request):
            return policies(request) if policies.handles(request) else registries(request)
        def pm(request):
            if request.url.path.endswith("/PARTNER_BANK_A"):
                return httpx.Response(200, json={{"keys": [{{"kid": "k1", "algorithm": "ES256", "public_key": partner.public_pem}}]}})
            return httpx.Response(404)
        def init(self, *a, **kw):
            orig_init(self, *a, http_transport=httpx.MockTransport(stub), pm_transport=httpx.MockTransport(pm), **kw)
        cs.CompositeService.__init__ = init

        from openg2p_fastapi_common.context import dbengine
        from openg2p_agri_composite.main import app
        assert dbengine.get() is None, "no database engine must be created"

        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
                assert (await c.get("/ping")).status_code == 200
                jwks = (await c.get("/.well-known/jwks.json")).json()
                # No kid set: the .p12 certificate's thumbprint, as in the signatures.
                assert len(jwks["keys"]) == 1 and jwks["keys"][0]["kid"] and jwks["keys"][0]["alg"] == "ES256", jwks
                assert "d" not in jwks["keys"][0], jwks
                lst = (await c.get("/composite/v1/use-cases")).json()
                assert [u["use_case"] for u in lst["use_cases"]] == ["loan-profile@1"], lst
                assert (await c.get("/composite/v1/use-cases/loan-profile@1")).json()["title"]
                assert (await c.get("/composite/v1/use-cases/nope")).status_code == 404
                t = await c.get("/composite/v1/internal/consent-templates", params={{"partner": "bank-a"}})
                assert [x["code"] for x in t.json()["templates"]] == ["loan-profile@1"], t.text
                header = {{"version": "1.0.0", "message_id": str(uuid.uuid4()),
                          "message_ts": datetime.now(timezone.utc).isoformat(), "action": "query",
                          "sender_id": "bank-a", "receiver_id": "agri-composite"}}
                now = datetime.now(timezone.utc).isoformat()
                consent = partner.sign_compact({{"jti": "j", "aud": "bank-a", "subject_id": {{"type": "FAYDA_FAN", "value": FAN}},
                    "purpose": {{"code": "credit-assessment"}}, "issued_at": now,
                    "grants": [{{"data_controller": c, "data_scopes": s}} for c, s in SCOPES.items()]}})
                message = {{"subject": {{"type": "FAYDA_FAN", "value": FAN}}, "parameters": {{"crop_year": 2019}}, "consent_jws": consent}}
                env = {{"signature": partner.sign_detached({{"header": header, "message": message}}), "header": header, "message": message}}
                r = await c.post("/composite/v1/use-cases/loan-profile/query", json=env)
                assert r.status_code == 200, r.text
                body = r.json()
                assert body["message"]["sources"]["farmer"]["status"] == "ok"
                assert body["message"]["data"]["crops"]["total_area_sown_ha"] == 1.15
                r = await c.post("/composite/v1/use-cases/loan-profile/query", content=b"not json")
                assert r.status_code == 400, r.text
        asyncio.run(run())
        print("APP-OK")
        """
    )
    env = {
        **os.environ,
        "AGRI_COMPOSITE_USE_CASES_DIR": USE_CASES,
        "AGRI_COMPOSITE_CONSENT_MODE": "passthrough",  # no exchange CM in this test
        "AGRI_COMPOSITE_USE_CASES_RELOAD_SECONDS": "0",
        "AGRI_COMPOSITE_SIGNING_P12_PATH": composite_p12["path"],
        "AGRI_COMPOSITE_SIGNING_P12_PASSWORD": composite_p12["password"],
        "AGRI_COMPOSITE_PARTNER_MGMT_API_URL": "http://pm.test",
        "AGRI_COMPOSITE_REGISTRIES": json.dumps({
            "farmer-registry": {"url": "http://fr.test/dci/registry/sync/search"},
            "crop-sown-registry": {"url": "http://csr.test/dci/registry/sync/search"},
        }),
    }
    proc = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=120, cwd=tmp_path)
    assert "APP-OK" in proc.stdout, proc.stdout[-3000:] + proc.stderr[-5000:]
