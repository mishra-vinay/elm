"""DhanFeed against a local fake WebSocket server (the real endpoint is not reachable from the build environment)."""
import asyncio
import json
import struct

import websockets

from dryrun.dhan_feed import DhanFeed, FeedTick


def ticker(seg, sid, ltp, ltt=1_790_000_000):
    return struct.pack("<BHBi", 2, 16, seg, sid) + struct.pack("<fi", ltp, ltt)


def disconnect(code):
    return struct.pack("<BHBi", 50, 10, 1, 0) + struct.pack("<h", code)


async def run_case(handler, wait=3.0, subscribe=(("NSE_EQ", "317"),)):
    got = {"ticks": [], "notes": [], "status": [], "subs": [], "paths": []}

    async def h(ws):
        got["paths"].append(ws.request.path)
        await handler(ws, got)

    async with websockets.serve(h, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        feed = DhanFeed("CID", "TOKEN123", got["ticks"].append, got["notes"].append,
                        lambda lvl, msg: got["status"].append((lvl, msg)), url=f"ws://127.0.0.1:{port}")
        await feed.subscribe(subscribe)
        await feed.start()
        await asyncio.sleep(wait)
        await feed.stop()
        return feed, got


def test_connects_with_documented_url_subscribes_and_parses_ticks():
    async def handler(ws, got):
        msg = json.loads(await ws.recv())
        got["subs"].append(msg)
        await ws.send(ticker(1, 317, 960.5))
        await ws.send(ticker(1, 317, 961.0))
        await asyncio.sleep(1.5)

    feed, got = asyncio.run(run_case(handler, wait=1.0))
    path = got["paths"][0]
    assert "version=2" in path and "token=TOKEN123" in path and "clientId=CID" in path and "authType=2" in path
    assert got["subs"][0] == {"RequestCode": 17, "InstrumentCount": 1,
                              "InstrumentList": [{"ExchangeSegment": "NSE_EQ", "SecurityId": "317"}]}
    assert [t.ltp for t in got["ticks"]] == [960.5, 961.0] and all(isinstance(t, FeedTick) for t in got["ticks"])
    assert got["ticks"][0].security_id == "317" and got["ticks"][0].recv_ts is not None


def test_reconnects_and_resubscribes_after_server_drops():
    async def handler(ws, got):
        got["subs"].append(json.loads(await ws.recv()))
        await ws.send(ticker(1, 317, 100.0 + len(got["subs"])))
        await ws.close()                                   # server drops the connection every time

    feed, got = asyncio.run(run_case(handler, wait=3.5))
    assert len(got["subs"]) >= 2                            # it came back and subscribed again
    assert feed.disconnects >= 1 and len(got["ticks"]) >= 2
    assert any("reconnecting" in m for _l, m in got["status"])


def test_fatal_disconnect_code_stops_retrying():
    async def handler(ws, got):
        got["subs"].append(json.loads(await ws.recv()))
        await ws.send(disconnect(807))                      # access token expired
        await asyncio.sleep(1.5)

    feed, got = asyncio.run(run_case(handler, wait=1.0))
    assert feed.fatal and "807" in feed.fatal
    assert any(n.kind == "disconnect" and n.value == 807 for n in got["notes"])
    assert any(l == "error" for l, _m in got["status"])


def test_token_update_clears_fatal_state():
    feed = DhanFeed("c", "old", lambda t: None)
    feed.fatal = "boom"
    feed.update_token("new-token-value-123456789")
    assert feed.fatal is None and feed.token == "new-token-value-123456789"
