"""Dhan live market feed v2 (WebSocket, binary, little-endian).

Layout and request codes are from https://dhanhq.co/docs/v2/live-market-feed/ and the Annexure:
  URL  wss://api-feed.dhan.co?version=2&token=<access token>&clientId=<client id>&authType=2
  Subscribe (JSON): {"RequestCode": 15|17|21, "InstrumentCount": n, "InstrumentList": [{"ExchangeSegment","SecurityId"}]}
       15 = Ticker, 17 = Quote, 21 = Full; max 100 instruments per message, 5,000 per connection, 5 connections per user.
  Server pings every 10 s (the `websockets` library answers automatically); no reply for 40 s closes the socket.
  Packets: 8-byte header (code u8, length u16, segment u8, security id i32) + body. Codes: 2 ticker, 4 quote,
  5 OI, 6 prev close, 8 full, 50 disconnect.

NOTE: this is the broker's WebSocket feed (last-traded-price updates), not the exchange's complete tick-by-tick
order-book feed. Measure its latency and completeness during the dry run before relying on it.
Verify the first live session with scripts/check_dhan.py (I could not connect to Dhan from my build environment).
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable

from .config import IST

log = logging.getLogger("dryrun.feed")

SEGMENT_BY_CODE = {0: "IDX_I", 1: "NSE_EQ", 2: "NSE_FNO", 3: "NSE_CURRENCY", 4: "BSE_EQ", 5: "MCX_COMM",
                   7: "BSE_CURRENCY", 8: "BSE_FNO"}
REQUEST_CODE = {"ticker": 15, "quote": 17, "full": 21}
CODE_TICKER, CODE_QUOTE, CODE_OI, CODE_PREV_CLOSE, CODE_FULL, CODE_DISCONNECT = 2, 4, 5, 6, 8, 50
FATAL_DISCONNECT = {806, 807, 808, 809, 810}     # data plan missing / token expired or invalid: retrying will not help


@dataclass
class FeedTick:
    segment: str
    security_id: str
    ltp: float
    ltt: int | None = None            # exchange last-trade-time, raw (epoch base not verified; not used for logic)
    volume: int | None = None
    day_high: float | None = None
    day_low: float | None = None
    recv_ts: datetime | None = None   # local IST arrival time: what the strategy and candles use


@dataclass
class FeedNote:
    kind: str                         # prev_close | oi | disconnect
    segment: str
    security_id: str
    value: float | int | None = None


def _seg(code: int) -> str:
    return SEGMENT_BY_CODE.get(code, f"SEG{code}")


def parse_packets(data: bytes, recv_ts: datetime | None = None) -> list[FeedTick | FeedNote]:
    """Parse one WebSocket message (may hold several packets) into ticks and notes. Never raises on bad input."""
    out: list[FeedTick | FeedNote] = []
    off, n = 0, len(data)
    recv_ts = recv_ts or datetime.now(IST).replace(tzinfo=None)
    while off + 8 <= n:
        code = data[off]
        length = struct.unpack_from("<H", data, off + 1)[0]
        seg = _seg(data[off + 3])
        sid = str(struct.unpack_from("<i", data, off + 4)[0])
        end = off + length if 8 <= length <= n - off else n      # tolerate a wrong length field
        pkt = data[off:end]
        try:
            if code == CODE_TICKER and len(pkt) >= 16:
                ltp, ltt = struct.unpack_from("<fi", pkt, 8)
                out.append(FeedTick(seg, sid, round(ltp, 4), ltt, recv_ts=recv_ts))
            elif code == CODE_QUOTE and len(pkt) >= 50:
                ltp, _ltq, ltt, _atp, vol = struct.unpack_from("<fhifi", pkt, 8)
                day_high, day_low = struct.unpack_from("<ff", pkt, 42)
                out.append(FeedTick(seg, sid, round(ltp, 4), ltt, vol, round(day_high, 4) or None,
                                    round(day_low, 4) or None, recv_ts))
            elif code == CODE_FULL and len(pkt) >= 62:
                ltp, _ltq, ltt, _atp, vol = struct.unpack_from("<fhifi", pkt, 8)
                day_high, day_low = struct.unpack_from("<ff", pkt, 54)
                out.append(FeedTick(seg, sid, round(ltp, 4), ltt, vol, round(day_high, 4) or None,
                                    round(day_low, 4) or None, recv_ts))
            elif code == CODE_PREV_CLOSE and len(pkt) >= 16:
                pc, oi = struct.unpack_from("<fi", pkt, 8)
                out.append(FeedNote("prev_close", seg, sid, round(pc, 4)))
            elif code == CODE_OI and len(pkt) >= 12:
                out.append(FeedNote("oi", seg, sid, struct.unpack_from("<i", pkt, 8)[0]))
            elif code == CODE_DISCONNECT and len(pkt) >= 10:
                out.append(FeedNote("disconnect", seg, sid, struct.unpack_from("<h", pkt, 8)[0]))
        except struct.error:
            log.warning("could not parse packet code=%s len=%s", code, len(pkt))
        if end <= off:
            break
        off = end
    return out


def subscribe_messages(instruments: Iterable[tuple[str, str]], packet: str = "quote") -> list[str]:
    items = [{"ExchangeSegment": s, "SecurityId": str(i)} for s, i in instruments]
    code = REQUEST_CODE[packet]
    return [json.dumps({"RequestCode": code, "InstrumentCount": len(chunk), "InstrumentList": chunk})
            for chunk in (items[i:i + 100] for i in range(0, len(items), 100))]


class DhanFeed:
    """Connects, subscribes, reconnects with backoff, and hands ticks to `on_tick`."""

    def __init__(self, client_id: str, token: str, on_tick: Callable[[FeedTick], None],
                 on_note: Callable[[FeedNote], None] | None = None,
                 on_status: Callable[[str, str], None] | None = None, packet: str = "quote",
                 url: str = "wss://api-feed.dhan.co", ping_interval: float | None = 15.0, ping_timeout: float | None = 20.0):
        self.client_id, self.token, self.packet, self.url = client_id, token, packet, url
        # Client keep-alive: Dhan pings us every 10 s, but a half-dead TCP connection (no packets, no close) would never be
        # noticed without our own ping. No pong within ping_timeout closes the socket and the loop below reconnects.
        self.ping_interval, self.ping_timeout = ping_interval, ping_timeout
        self.rtt_ms: float | None = None          # last websocket ping round trip to Dhan, when the library reports it
        self.on_tick, self.on_note, self.on_status = on_tick, on_note, on_status
        self.subscribed: set[tuple[str, str]] = set()
        self.connected = False
        self.fatal: str | None = None
        self.disconnects = 0
        self._ws = None
        self._task: asyncio.Task | None = None
        self._stop = False
        self.last_message_at: datetime | None = None

    # --- API used by the runner -------------------------------------------------------------
    async def start(self):
        self._stop = False
        self._task = asyncio.create_task(self._run(), name="dhan-feed")

    async def stop(self):
        self._stop = True
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"RequestCode": 12}))
                await self._ws.close()
            except Exception:       # noqa: BLE001
                pass
        if self._task:
            self._task.cancel()

    async def subscribe(self, instruments: Iterable[tuple[str, str]]):
        new = [i for i in instruments if i not in self.subscribed]
        self.subscribed.update(new)
        if self._ws is not None and self.connected and new:
            for m in subscribe_messages(new, self.packet):
                await self._ws.send(m)

    def update_token(self, token: str):
        """Tokens last 24 h. A new one is used on the next (re)connect; call `reconnect` to apply it now."""
        self.token = token
        self.fatal = None

    async def reconnect(self):
        if self._ws is not None:
            await self._ws.close()

    # --- internals -------------------------------------------------------------------------
    def _status(self, level: str, msg: str):
        log.log(logging.ERROR if level == "error" else logging.INFO, msg)
        if self.on_status:
            self.on_status(level, msg)

    async def _run(self):
        import websockets
        backoff = 1.0
        while not self._stop:
            if self.fatal:
                await asyncio.sleep(5)       # wait for update_token()
                continue
            uri = f"{self.url}?version=2&token={self.token}&clientId={self.client_id}&authType=2"
            try:
                async with websockets.connect(uri, ping_interval=self.ping_interval, ping_timeout=self.ping_timeout,
                                              max_size=2 ** 20, open_timeout=15) as ws:
                    self._ws, self.connected, backoff = ws, True, 1.0
                    self._status("info", "feed connected")
                    for m in subscribe_messages(self.subscribed, self.packet):
                        await ws.send(m)
                    async for msg in ws:
                        lat = getattr(ws, "latency", None)
                        self.rtt_ms = round(lat * 1000, 1) if lat else self.rtt_ms
                        self.last_message_at = datetime.now(IST).replace(tzinfo=None)
                        if isinstance(msg, str):
                            continue
                        for item in parse_packets(msg, self.last_message_at):
                            if isinstance(item, FeedTick):
                                self.on_tick(item)
                            else:
                                if item.kind == "disconnect" and item.value in FATAL_DISCONNECT:
                                    self.fatal = f"Dhan disconnected the feed with code {item.value} (token/data plan problem)"
                                    self._status("error", self.fatal)
                                if self.on_note:
                                    self.on_note(item)
            except asyncio.CancelledError:
                raise
            except Exception as e:      # noqa: BLE001
                self._status("warn", f"feed error: {type(e).__name__}: {e}")
            finally:
                self.connected, self._ws = False, None
            if self._stop:
                break
            self.disconnects += 1
            self._status("warn", f"feed disconnected, reconnecting in {backoff:.0f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
