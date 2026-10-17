"""Live wire outputs validated against schemas/contracts.schema.json.

check_design.py validates the authored examples; these tests validate what
the running broker actually emits (State, StatusResponse, Snapshot, Job,
ControlGrant) so implementation drift cannot silently diverge from the
contract.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import aiohttp
from jsonschema import Draft202012Validator

from tests.integration.conftest import acquire, agent_session

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = __import__("json").loads((ROOT / "schemas" / "contracts.schema.json").read_text("utf-8"))


def validator(definition: str) -> Draft202012Validator:
    return Draft202012Validator(
        {
            "$schema": SCHEMA["$schema"],
            "$ref": f"#/$defs/{definition}",
            "$defs": SCHEMA["$defs"],
        }
    )


def rid() -> str:
    return str(uuid.uuid4())


async def test_live_state_and_status_conform(broker, base_url):
    agent = await agent_session(base_url, broker.credentials.agent_bootstrap)
    async with aiohttp.ClientSession() as http:
        async with http.get(
            f"{base_url}/v1/state", headers={"Authorization": f"Bearer {agent['session_token']}"}
        ) as r:
            assert r.status == 200
            state = await r.json()
        errors = list(validator("State").iter_errors(state))
        assert not errors, errors[0].message

        async with http.get(
            f"{base_url}/v1/status", headers={"Authorization": f"Bearer {agent['session_token']}"}
        ) as r:
            status = await r.json()
        assert not list(validator("StatusResponse").iter_errors(status))


async def test_live_snapshot_and_grant_and_job_conform(broker, base_url):
    agent = await agent_session(base_url, broker.credentials.agent_bootstrap)
    token = agent["session_token"]
    async with aiohttp.ClientSession() as http:
        async with http.get(
            f"{base_url}/v1/frames/latest", headers={"Authorization": f"Bearer {token}"}
        ) as r:
            snapshot = await r.json()
        assert not list(validator("Snapshot").iter_errors(snapshot)), "Snapshot contract drift"

        response, grant = await acquire(base_url, agent, rid())
        assert response.status == 200
        assert not list(validator("ControlGrant").iter_errors(grant)), "ControlGrant drift"

        basis_id = snapshot["metadata"]["basis_id"]
        command = {
            "api_version": 1,
            "request_id": rid(),
            "lease": grant["lease"],
            "action": {"kind": "mouse_move", "basis_id": basis_id, "x": 3, "y": 4},
        }
        async with http.post(
            f"{base_url}/v1/commands", json=command, headers={"Authorization": f"Bearer {token}"}
        ) as r:
            assert r.status == 202
            submitted = await r.json()
        for _ in range(50):
            async with http.get(
                f"{base_url}/v1/jobs/{submitted['job_id']}",
                headers={"Authorization": f"Bearer {token}"},
            ) as r:
                job = await r.json()
            if job.get("terminal"):
                break
            await asyncio.sleep(0.05)
        assert not list(validator("Job").iter_errors(job)), "Job contract drift"
        assert job["status"] == "written"

        # Error envelopes also follow the contract shape.
        async with http.post(
            f"{base_url}/v1/control/acquire",
            json={"request_id": rid(), "ttl_ms": 100},  # out of range -> 400
            headers={"Authorization": f"Bearer {token}"},
        ) as r:
            error = await r.json()
        assert not list(validator("ErrorEnvelope").iter_errors(error)), "ErrorEnvelope drift"
