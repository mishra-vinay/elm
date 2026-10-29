import sqlite3
import struct

from dryrun.depth import FULL_LEN, insert_rows, log_event, open_db, parse_full, sanity, subscribe_full


def full_packet(sid=14434, ltp=290.4, vol=1500, bids=None, asks=None):
    bids = bids or [(290.3, 12, 3), (290.2, 8, 2), (290.1, 20, 5), (290.0, 15, 4), (289.9, 9, 2)]
    asks = asks or [(290.5, 7, 2), (290.6, 14, 3), (290.7, 11, 3), (290.8, 6, 1), (290.9, 30, 6)]
    body = struct.pack("<fhifiiiiiiffff", ltp, 2, 1_790_000_000, 290.2, vol, 500, 700, 4200, 5000, 3000, 289.0, 288.0, 291.0, 287.5)
    for (bp, bq, bo), (ap, aq, ao) in zip(bids, asks):
        body += struct.pack("<iihhff", bq, aq, bo, ao, bp, ap)
    pkt = struct.pack("<BHBi", 8, 8 + len(body), 5, sid) + body
    assert len(pkt) == FULL_LEN
    return pkt


def test_parse_full_levels_and_totals():
    [r] = parse_full(full_packet(), 1_790_000_000_123)
    assert (r.segment, r.security_id, r.ltp, r.volume, r.total_sell, r.total_buy, r.oi) == ("MCX_COMM", "14434", 290.4, 1500, 500, 700, 4200)
    assert (r.day_open, r.day_close, r.day_high, r.day_low) == (289.0, 288.0, 291.0, 287.5)
    assert r.bids[0] == (290.3, 12, 3) and r.bids[4] == (289.9, 9, 2)
    assert r.asks[0] == (290.5, 7, 2) and r.asks[4] == (290.9, 30, 6)
    assert sanity(r) == []


def test_two_packets_in_one_message_and_garbage_tolerated():
    rows = parse_full(full_packet(vol=1) + full_packet(vol=2) + b"\x01\x02")
    assert [r.volume for r in rows] == [1, 2]
    assert parse_full(b"") == [] and parse_full(b"\x08\x05\x00") == []


def test_sanity_flags_crossed_book():
    bad = full_packet(bids=[(291.0, 5, 1)] + [(290.0, 1, 1)] * 4)
    [r] = parse_full(bad)
    assert "crossed book" in sanity(r)


def test_store_and_view(tmp_path):
    con = open_db(str(tmp_path / "d.db"))
    rows = parse_full(full_packet(vol=100), 1_790_000_000_000) + parse_full(full_packet(vol=130), 1_790_000_000_500)
    insert_rows(con, rows)
    log_event(con, "start", "x")
    v = con.execute("SELECT vol_delta, bid5_qty, ask5_qty, imbalance5, spread FROM depth_view ORDER BY id").fetchall()
    assert v[0][0] is None and v[1][0] == 30
    assert v[1][1] == 64 and v[1][2] == 68 and v[1][3] == round((64 - 68) / 132, 4) and v[1][4] == 0.2
    assert con.execute("SELECT length(raw) FROM depth LIMIT 1").fetchone()[0] == FULL_LEN
    assert con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_subscribe_message():
    assert '"RequestCode": 21' in subscribe_full("MCX_COMM", "14434") and '"SecurityId": "14434"' in subscribe_full("MCX_COMM", 14434)
