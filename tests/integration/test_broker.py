"""I01/I02/I07 + V02/V03 against the fully simulated broker."""

from __future__ import annotations

import asyncio
import base64
import uuid

import aiohttp

from tests.integration.conftest import acquire, agent_session, browser_page


def rid() -> str:
    return str(uuid.uuid4())


async def test_i01_simulated_closed_loop(broker, base_url):
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{base_url}/healthz") as r:
            assert r.status == 200
            assert await r.json() == {"alive": True, "api_version": 1}

        agent = await agent_session(base_url, broker.credentials.agent_bootstrap)
        assert agent["instance_id"] == broker.manager.instance_id

        async with http.get(
            f"{base_url}/v1/status", headers={"Authorization": f"Bearer {agent['session_token']}"}
        ) as r:
            assert r.status == 200
            status = await r.json()
            assert status["state"]["source"] == "simulated"  # UI/state labelled simulated
            assert status["state"]["mode"] == "idle"

        # Snapshot before any control: metadata labels the simulated source.
        async with http.get(
            f"{base_url}/v1/frames/latest",
            headers={"Authorization": f"Bearer {agent['session_token']}"},
        ) as r:
            assert r.status == 200, await r.text()
            snapshot = await r.json()
        assert snapshot["metadata"]["source"] == "simulated"
        assert snapshot["metadata"]["image_width"] > 0
        jpeg = base64.b64decode(snapshot["data_base64"])
        assert jpeg[:2] == b"\xff\xd8"  # real JPEG magic, not a blank placeholder

        _, grant = await acquire(base_url, agent, rid())
        assert grant["lease"]["instance_id"] == broker.manager.instance_id

        basis_id = snapshot["metadata"]["basis_id"]
        command = {
            "api_version": 1,
            "request_id": rid(),
            "lease": grant["lease"],
            "action": {"kind": "mouse_move", "basis_id": basis_id, "x": 10, "y": 10},
        }
        async with http.post(
            f"{base_url}/v1/commands",
            json=command,
            headers={"Authorization": f"Bearer {agent['session_token']}"},
        ) as r:
            assert r.status == 202, await r.text()
            job = await r.json()
        assert job["status"] in ("accepted", "running")
        job_id = job["job_id"]
        for _ in range(50):
            async with http.get(
                f"{base_url}/v1/jobs/{job_id}",
                headers={"Authorization": f"Bearer {agent['session_token']}"},
            ) as r:
                job = await r.json()
            if job["terminal"]:
                break
            await asyncio.sleep(0.05)
        assert job["status"] == "written" and job["completion"] == "written"

        # The simulated serial transport received the encoded absolute-mouse
        # report (cmd 0x04) for the move command.
        serial = broker._serial_transport
        assert serial is not None
        hid_writes = [w for w in serial.writes if len(w) >= 6 and w[3] in (0x02, 0x04, 0x05)]
        assert any(w[3] == 0x04 for w in hid_writes)


async def test_i02_authorization_and_csrf_rejections(broker, base_url):
    agent = await agent_session(base_url, broker.credentials.agent_bootstrap)
    token = agent["session_token"]

    async with aiohttp.ClientSession() as http:
        # Agent cannot call takeover (human route).
        async with http.post(
            f"{base_url}/v1/control/takeover",
            json={"request_id": rid()},
            headers={"Authorization": f"Bearer {token}"},
        ) as r:
            assert r.status in (401, 403)

        # Human-only actions rejected for agents before any write.
        _, grant = await acquire(base_url, agent, rid())
        command = {
            "api_version": 1,
            "request_id": rid(),
            "lease": grant["lease"],
            "action": {"kind": "keyboard_state", "keys": ["KeyA"], "modifiers": []},
        }
        async with http.post(
            f"{base_url}/v1/commands",
            json=command,
            headers={"Authorization": f"Bearer {token}"},
        ) as r:
            assert r.status == 403
            body = await r.json()
            assert body["error"]["code"] == "FORBIDDEN"

        # Malformed Origin on the browser exchange.
        async with http.post(
            f"{base_url}/v1/sessions/browser",
            json={"bootstrap": broker.credentials.agent_bootstrap},
            headers={"Origin": "https://evil.example"},
        ) as r:
            assert r.status == 403

        # Page session without CSRF.
        import time as _t

        page = await browser_page(base_url, broker.sessions.issue_bootstrap(_t.monotonic()))
        async with http.post(
            f"{base_url}/v1/control/takeover",
            json={"request_id": rid()},
            headers={
                "Cookie": f"superuse_browser={page['cookie']}",
                "X-Superuse-Page": page["page_token"],
                "X-CSRF-Token": "wrong",
                "Origin": base_url,
            },
        ) as r:
            assert r.status == 403

        # Unknown fields rejected.
        async with http.post(
            f"{base_url}/v1/control/acquire",
            json={"request_id": rid(), "session_id": "forged"},
            headers={"Authorization": f"Bearer {token}"},
        ) as r:
            assert r.status == 400

        # No input side effects: every HID write is one of the two
        # neutralization packets from the acquire barrier.
        serial = broker._serial_transport
        assert serial is not None
        neutral = {
            bytes.fromhex("57 ab 00 02 08 00 00 00 00 00 00 00 00 0c"),
            bytes.fromhex("57 ab 00 05 05 01 00 00 00 00 0d"),
            # Absolute-mode button lift at the last position (fence).
            bytes.fromhex("57 ab 00 04 07 02 00 00 08 00 08 00 1f"),
        }
        hid_writes = [w for w in serial.writes if len(w) >= 6 and w[3] in (0x02, 0x04, 0x05)]
        assert set(hid_writes) <= neutral


async def test_i07_bounded_rejections(broker, base_url):
    agent = await agent_session(base_url, broker.credentials.agent_bootstrap)
    token = agent["session_token"]
    async with aiohttp.ClientSession() as http:
        # Oversized body rejected without unbounded allocation.
        async with http.post(
            f"{base_url}/v1/commands",
            json={"padding": "x" * 100_000, "request_id": rid()},
            headers={"Authorization": f"Bearer {token}"},
        ) as r:
            assert r.status in (400, 413)

        # Binary junk on the video WS: the server answers with an auth error
        # (no cookie here) and closes the socket without crashing.
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(f"{base_url}/v1/video", origin=base_url) as ws:
                await ws.send_bytes(b"\x00" * 10)
                message = await ws.receive(timeout=3)
                if message.type is aiohttp.WSMsgType.TEXT:
                    message = await ws.receive(timeout=3)
                assert message.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                )


async def test_v03_snapshot_metadata_consistency(broker, base_url):
    agent = await agent_session(base_url, broker.credentials.agent_bootstrap)
    async with aiohttp.ClientSession() as http:
        async with http.get(
            f"{base_url}/v1/frames/latest?max_width=640",
            headers={"Authorization": f"Bearer {agent['session_token']}"},
        ) as r:
            snapshot = await r.json()
        meta = snapshot["metadata"]
        assert meta["image_width"] <= 640
        assert meta["native_width"] >= meta["image_width"]
        jpeg = base64.b64decode(snapshot["data_base64"])
        assert jpeg[:2] == b"\xff\xd8" and jpeg[-2:] == b"\xff\xd9"

        # after_frame_id waits for a newer frame or times out.
        async with http.get(
            f"{base_url}/v1/frames/latest?after_frame_id={meta['frame_id']}&wait_ms=1500",
            headers={"Authorization": f"Bearer {agent['session_token']}"},
        ) as r:
            newer = await r.json()
        assert newer["metadata"]["frame_id"] > meta["frame_id"]
        assert newer["metadata"]["basis_id"]


async def test_human_takeover_flow_over_http(broker, base_url):
    agent = await agent_session(base_url, broker.credentials.agent_bootstrap)
    _, grant = await acquire(base_url, agent, rid())
    import time as _time

    page = await browser_page(base_url, broker.sessions.issue_bootstrap(_time.monotonic()))

    async with aiohttp.ClientSession() as http:
        async with http.post(
            f"{base_url}/v1/control/takeover",
            json={"request_id": rid()},
            headers={
                "Cookie": f"superuse_browser={page['cookie']}",
                "X-Superuse-Page": page["page_token"],
                "X-CSRF-Token": page["csrf"],
                "Origin": base_url,
            },
        ) as r:
            assert r.status == 200, await r.text()
            takeover = await r.json()
        assert takeover["state"]["mode"] == "human"
        assert takeover["state"]["owner"]["kind"] == "human"

        # Agent inputs now revoked (take the basis from a real snapshot).
        async with http.get(
            f"{base_url}/v1/frames/latest",
            headers={"Authorization": f"Bearer {agent['session_token']}"},
        ) as r:
            snapshot = await r.json()
        command = {
            "api_version": 1,
            "request_id": rid(),
            "lease": grant["lease"],
            "action": {
                "kind": "mouse_move",
                "basis_id": snapshot["metadata"]["basis_id"],
                "x": 1,
                "y": 1,
            },
        }
        async with http.post(
            f"{base_url}/v1/commands",
            json=command,
            headers={"Authorization": f"Bearer {agent['session_token']}"},
        ) as r:
            assert r.status == 409
            body = await r.json()
            assert body["error"]["code"] in ("CONTROL_REVOKED", "HUMAN_CONTROL")

        # Handback returns to idle.
        async with http.post(
            f"{base_url}/v1/control/handback",
            json={"request_id": rid()},
            headers={
                "Cookie": f"superuse_browser={page['cookie']}",
                "X-Superuse-Page": page["page_token"],
                "X-CSRF-Token": page["csrf"],
                "Origin": base_url,
            },
        ) as r:
            assert r.status == 200
            state = await r.json()
        assert state["mode"] == "idle" and state["human_latched"] is False


async def test_old_input_disconnect_after_same_page_retake_keeps_new_lease(
    broker, base_url, monkeypatch
):
    import time

    page = await browser_page(base_url, broker.sessions.issue_bootstrap(time.monotonic()))
    headers = {
        "Cookie": f"superuse_browser={page['cookie']}",
        "X-Superuse-Page": page["page_token"],
        "X-CSRF-Token": page["csrf"],
        "Origin": base_url,
    }
    cleanup_done = asyncio.Event()
    suspend = broker.manager.human_suspend

    async def observe_cleanup(session, reason, **kwargs):
        await suspend(session, reason, **kwargs)
        if reason == "human_input_closed":
            cleanup_done.set()

    monkeypatch.setattr(broker.manager, "human_suspend", observe_cleanup)
    async with aiohttp.ClientSession() as http:
        async with http.post(
            f"{base_url}/v1/control/takeover", json={"request_id": rid()}, headers=headers
        ) as response:
            old = await response.json()
        async with http.ws_connect(
            f"{base_url}/v1/input", headers={"Cookie": headers["Cookie"]}, origin=base_url
        ) as ws:
            await ws.send_json(
                {"type": "authenticate", "page_token": page["page_token"], "csrf": page["csrf"]}
            )
            await ws.send_json(
                {
                    "api_version": 1,
                    "sequence": 1,
                    "lease": old["lease"],
                    "action": {"kind": "mouse_relative_state", "dx": 0, "dy": 0, "buttons": 0},
                }
            )
            assert (await ws.receive_json(timeout=2))["status"] == "written"
            async with http.post(
                f"{base_url}/v1/control/handback", json={"request_id": rid()}, headers=headers
            ) as response:
                assert response.status == 200
            async with http.post(
                f"{base_url}/v1/control/takeover", json={"request_id": rid()}, headers=headers
            ) as response:
                new = await response.json()
            assert old["lease"] != new["lease"]
            await ws.close()
        await asyncio.wait_for(cleanup_done.wait(), 2)
        assert broker.manager.get_state().mode.value == "human"
        assert broker.manager.current_lease.lease.lease_id == new["lease"]["lease_id"]


async def test_web_assets_served_locally(broker, base_url):
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{base_url}/") as r:
            assert r.status == 200
            html = await r.text()
            assert 'src="/app.js"' in html
            assert "btnTakeover" in html
            assert r.headers["Content-Security-Policy"].startswith("default-src 'self'")
        async with http.get(f"{base_url}/app.js") as r:
            assert r.status == 200
            script = await r.text()
            assert "/v1/control/takeover" in script
            assert "http://" not in script.replace("http://127.0.0.1", "")  # no third-party URLs
        async with http.get(f"{base_url}/app.css") as r:
            assert r.status == 200


async def test_i09_two_tabs_independent_page_sessions(broker, base_url):
    import time as _time

    bootstrap = broker.sessions.issue_bootstrap(_time.monotonic())
    page1 = await browser_page(base_url, bootstrap)
    page2 = await browser_page(base_url, cookie=page1["cookie"], csrf=page1["csrf"])
    assert page1["session_id"] != page2["session_id"]  # per-tab actors

    headers1 = {
        "Cookie": f"superuse_browser={page1['cookie']}",
        "X-Superuse-Page": page1["page_token"],
        "X-CSRF-Token": page1["csrf"],
        "Origin": base_url,
    }
    async with aiohttp.ClientSession() as http:
        async with http.post(
            f"{base_url}/v1/control/takeover", json={"request_id": rid()}, headers=headers1
        ) as r:
            assert r.status == 200
            assert (await r.json())["state"]["owner"]["session_id"] == page1["session_id"]

        # Tab 1 closes; its page session is cleaned up and control parks in
        # human_hold — never handed to an agent automatically.
        async with http.delete(f"{base_url}/v1/session", headers=headers1) as r:
            assert r.status == 200
        await asyncio.sleep(0.3)
        async with http.get(
            f"{base_url}/v1/state",
            headers={
                "Cookie": f"superuse_browser={page2['cookie']}",
                "X-Superuse-Page": page2["page_token"],
            },
        ) as r:
            state = await r.json()
        assert state["mode"] == "human_hold"

        # Tab 2 (a different page session of the same browser) takes over.
        headers2 = {
            "Cookie": f"superuse_browser={page2['cookie']}",
            "X-Superuse-Page": page2["page_token"],
            "X-CSRF-Token": page2["csrf"],
            "Origin": base_url,
        }
        async with http.post(
            f"{base_url}/v1/control/takeover", json={"request_id": rid()}, headers=headers2
        ) as r:
            assert r.status == 200
            state = (await r.json())["state"]
        assert state["mode"] == "human"
        assert state["owner"]["session_id"] == page2["session_id"]
