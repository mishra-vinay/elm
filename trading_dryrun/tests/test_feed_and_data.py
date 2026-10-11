import json
import struct
from datetime import datetime

import pytest

from dryrun.candles import MinuteAggregator
from dryrun.dhan_feed import FeedTick, parse_packets, subscribe_messages
from dryrun.dhan_rest import DhanError, decode_times


def header(code, length, seg, sid):
    return struct.pack("<BHBi", code, length, seg, sid)


def test_parse_ticker_packet():
    pkt = header(2, 16, 1, 317) + struct.pack("<fi", 960.25, 1_790_000_000)
    [t] = parse_packets(pkt)
    assert isinstance(t, FeedTick) and (t.segment, t.security_id, t.ltp, t.ltt) == ("NSE_EQ", "317", 960.25, 1_790_000_000)


def test_parse_quote_packet_fields():
    body = struct.pack("<fhifiiiffff", 22800.5, 50, 1_790_000_100, 22790.0, 123456, 10, 20, 22700.0, 0.0, 22850.0, 22650.0)
    [t] = parse_packets(header(4, 8 + len(body), 0, 13) + body)
    assert (t.segment, t.security_id, t.ltp, t.volume, t.day_high, t.day_low) == ("IDX_I", "13", 22800.5, 123456, 22850.0, 22650.0)


def test_parse_prev_close_oi_and_disconnect():
    msgs = parse_packets(header(6, 16, 2, 48704) + struct.pack("<fi", 24000.0, 777)
                         + header(5, 12, 2, 48704) + struct.pack("<i", 5000)
                         + header(50, 10, 1, 0) + struct.pack("<h", 807))
    assert [(m.kind, m.value) for m in msgs] == [("prev_close", 24000.0), ("oi", 5000), ("disconnect", 807)]


def test_parse_two_ticks_in_one_message_and_garbage():
    two = header(2, 16, 1, 1) + struct.pack("<fi", 1.5, 1) + header(2, 16, 1, 2) + struct.pack("<fi", 2.5, 2)
    assert [t.ltp for t in parse_packets(two)] == [1.5, 2.5]
    assert parse_packets(b"\x02\x00") == []
    assert parse_packets(b"") == []
    parse_packets(header(4, 20, 1, 1) + b"\x00" * 3)         # truncated quote must not raise


def test_subscribe_messages_chunk_at_100():
    msgs = subscribe_messages([("NSE_EQ", str(i)) for i in range(250)], "quote")
    assert len(msgs) == 3
    first = json.loads(msgs[0])
    assert first["RequestCode"] == 17 and first["InstrumentCount"] == 100
    assert first["InstrumentList"][0] == {"ExchangeSegment": "NSE_EQ", "SecurityId": "0"}
    assert json.loads(subscribe_messages([("MCX_COMM", "1")], "ticker")[0])["RequestCode"] == 15


def test_minute_aggregator_ohlc_and_volume():
    agg = MinuteAggregator()
    t = datetime(2026, 10, 9, 10, 0, 5)
    assert agg.add(t, 100, 1000) is None
    agg.add(t.replace(second=20), 102, 1040)
    agg.add(t.replace(second=40), 99, 1100)
    bar = agg.add(t.replace(minute=1, second=1), 101, 1130)
    assert (bar.open, bar.high, bar.low, bar.close, bar.ts.minute) == (100, 102, 99, 99, 0)
    assert bar.volume == 100 and bar.ticks == 3
    assert agg.day_high == 102 and agg.day_low == 99


def _epoch_ist(dt):       # IST wall clock -> unix seconds
    from datetime import timezone, timedelta
    return int(dt.replace(tzinfo=timezone(timedelta(hours=5, minutes=30))).timestamp())


def test_decode_times_unix_utc_intraday():
    from datetime import timedelta
    stamps = [_epoch_ist(datetime(2026, 10, 9, 9, 15) + timedelta(minutes=5 * i)) for i in range(75)]
    out = decode_times(stamps, datetime(2026, 10, 9), datetime(2026, 10, 10), "NSE_EQ", daily=False)
    assert out[0] == datetime(2026, 10, 9, 9, 15)


def test_decode_times_detects_ist_wallclock_stamps():
    # a feed that encodes IST wall-clock as if it were UTC
    from datetime import timedelta, timezone
    day = datetime(2026, 10, 9, 9, 15)
    stamps = [int((day + timedelta(minutes=5 * i)).replace(tzinfo=timezone.utc).timestamp()) for i in range(75)]   # a full session
    out = decode_times(stamps, datetime(2026, 10, 9), datetime(2026, 10, 10), "NSE_EQ", daily=False)
    assert out[0] == datetime(2026, 10, 9, 9, 15) and out[-1] == datetime(2026, 10, 9, 15, 25)


def test_decode_times_1980_epoch():
    from datetime import timedelta
    base = datetime(1980, 1, 1)
    stamps = [int((datetime(2026, 10, 9, 9, 15) + timedelta(minutes=5 * i) - base).total_seconds()) for i in range(75)]
    assert decode_times(stamps, datetime(2026, 10, 9), datetime(2026, 10, 10), "NSE_EQ", daily=False)[0] == datetime(2026, 10, 9, 9, 15)


def test_decode_times_rejects_nonsense():
    with pytest.raises(DhanError):
        decode_times([5, 6, 7], datetime(2026, 10, 9), datetime(2026, 10, 10), "NSE_EQ", daily=False)
