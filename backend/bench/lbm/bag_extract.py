#!/usr/bin/env python3
"""从 ROS1 bag v2.0 里按话题、时间段抽出一份小包（原始字节照抄，不重新序列化），mrosplay 可直接播放。

用法：python3 bag_extract.py <源包> <输出包> --topics /vr_cmd [--start 秒] [--end 秒] [--tf-keep 正则]
--start / --end 是相对源包第一条消息的秒数。
--tf-keep：/tf 只保留 child_frame_id 匹配正则的变换（重新序列化），整条消息都不匹配就丢掉。
回放全身动捕时用它只留人体坐标系，机器人自己的 /tf 不能放进回放包。
  Pico 全身动捕：--tf-keep '^(pico_|/vr_body/)'；Xsens：按录包里的人体坐标系名写。
"""
import argparse
import re
import struct
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rosbag_lite as r  # noqa: E402

OP_MSG, OP_HEADER, OP_INDEX, OP_CHUNK, OP_CHUNK_INFO, OP_CONN = 0x02, 0x03, 0x04, 0x05, 0x06, 0x07
PER_CHUNK = 1000


def field(name, value):
    b = name.encode() + b"=" + value
    return struct.pack("<I", len(b)) + b


def record(fields, data):
    h = b"".join(field(k, v) for k, v in fields)
    return struct.pack("<I", len(h)) + h + struct.pack("<I", len(data)) + data


def u32(x):
    return struct.pack("<I", x)


def tm(sec, nsec):
    return struct.pack("<II", sec, nsec)


def _tf_filter(data, keep):
    """tf/tfMessage 原始字节 -> 只含匹配变换的新字节；一条都不剩返回 None。"""
    (n,) = struct.unpack_from("<I", data, 0)
    off, kept = 4, []
    for _ in range(n):
        start = off
        off += 12                                 # header.seq + header.stamp
        (ln,) = struct.unpack_from("<I", data, off)
        off += 4 + ln                             # header.frame_id
        (ln,) = struct.unpack_from("<I", data, off)
        child = bytes(data[off + 4:off + 4 + ln]).decode()
        off += 4 + ln + 56                        # child_frame_id + translation(3) + rotation(4) float64
        if keep.search(child):
            kept.append(bytes(data[start:off]))
    if off != len(data):
        raise SystemExit(f"/tf 消息长度不符（解析到 {off}，实际 {len(data)}），不是 tf/tfMessage？")
    return u32(len(kept)) + b"".join(kept) if kept else None


def read_raw(src, topics, start=0.0, end=1e12, keep=None, absolute=False):
    """按话题和时间段取原始消息。返回 (conns, msgs)：conns = [(新 id, topic bytes, 连接记录数据)]，
    msgs = [(新 id, sec, nsec, 原始数据)]，按时间排好。absolute=True 时 start/end 是绝对时间（录包接收时间）。"""
    bag = r.Bag(src)
    want = set(topics)
    conns = {}      # 源 conn id -> (新 id, topic, 原始连接数据)
    msgs = []
    t_first = None
    for header, s0_, length in r._records(bag.data):
        op = header["op"][0]
        if op == OP_CONN:
            bag._connection(bag.data, header, s0_, length)
            continue
        if op != OP_CHUNK:
            continue
        if header.get("compression", b"none") != b"none":
            raise SystemExit("不支持压缩包")
        chunk = bag.data[s0_:s0_ + length]
        for sub, s0, ln in r._records(chunk):
            sop = sub["op"][0]
            if sop == OP_CONN:
                cid = bag._connection(chunk, sub, s0, ln)
                if sub["topic"].decode() in want and cid not in conns:
                    conns[cid] = (len(conns), sub["topic"], bytes(chunk[s0:s0 + ln]))
            elif sop == OP_MSG:
                cid = struct.unpack("<I", sub["conn"])[0]
                sec, nsec = struct.unpack("<II", sub["time"])
                t = sec + nsec * 1e-9
                t_first = t if t_first is None else t_first
                rel = t if absolute else t - t_first
                if cid in conns and start <= rel <= end:
                    data = bytes(chunk[s0:s0 + ln])
                    if keep is not None and conns[cid][1] == b"/tf":
                        data = _tf_filter(data, keep)
                        if data is None:
                            continue
                    msgs.append((conns[cid][0], sec, nsec, data))
    msgs.sort(key=lambda m: (m[1], m[2]))
    return list(conns.values()), msgs


def write_bag(dst, conns, msgs):
    conn_records = {nid: record([("op", bytes([OP_CONN])), ("conn", u32(nid)), ("topic", topic)], data)
                    for nid, topic, data in conns}
    out = bytearray(b"#ROSBAG V2.0\n")
    header_pos = len(out)
    out += b"\0" * 4096   # 文件头占位，写完再回填
    chunk_infos = []
    for i in range(0, len(msgs), PER_CHUNK):
        part = msgs[i:i + PER_CHUNK]
        body = bytearray()
        index = {}
        for nid in sorted({m[0] for m in part}):
            body += conn_records[nid]
        for nid, sec, nsec, data in part:
            index.setdefault(nid, []).append((sec, nsec, len(body)))
            body += record([("op", bytes([OP_MSG])), ("conn", u32(nid)), ("time", tm(sec, nsec))], data)
        chunk_pos = len(out)
        out += record([("op", bytes([OP_CHUNK])), ("compression", b"none"), ("size", u32(len(body)))], bytes(body))
        for nid, entries in index.items():
            data = b"".join(tm(s, n) + u32(off) for s, n, off in entries)
            out += record([("op", bytes([OP_INDEX])), ("ver", u32(1)), ("conn", u32(nid)), ("count", u32(len(entries)))], data)
        st, en = part[0], part[-1]
        chunk_infos.append((chunk_pos, st[1], st[2], en[1], en[2], {k: len(v) for k, v in index.items()}))
    index_pos = len(out)
    for nid in sorted(conn_records):
        out += conn_records[nid]
    for pos, s1, n1, s2, n2, counts in chunk_infos:
        data = b"".join(u32(k) + u32(v) for k, v in sorted(counts.items()))
        out += record([("op", bytes([OP_CHUNK_INFO])), ("ver", u32(1)), ("chunk_pos", struct.pack("<Q", pos)),
                       ("start_time", tm(s1, n1)), ("end_time", tm(s2, n2)), ("count", u32(len(counts)))], data)
    hfields = [("op", bytes([OP_HEADER])), ("index_pos", struct.pack("<Q", index_pos)),
               ("conn_count", u32(len(conn_records))), ("chunk_count", u32(len(chunk_infos)))]
    hlen = len(b"".join(field(k, v) for k, v in hfields))
    hdr = record(hfields, b" " * (4096 - 8 - hlen))
    assert len(hdr) == 4096
    out[header_pos:header_pos + 4096] = hdr
    with open(dst, "wb") as f:
        f.write(out)
    return len(out), len(chunk_infos)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--topics", nargs="+", required=True)
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--end", type=float, default=1e12)
    ap.add_argument("--append-zero-sec", type=float, default=0.0,
                    help="仅 geometry_msgs/Twist：末尾按 50 Hz 补这么多秒的全零速度，保证回放结束时停下")
    ap.add_argument("--tf-keep", help="/tf 只保留 child_frame_id 匹配这个正则的变换")
    a = ap.parse_args()
    keep = re.compile(a.tf_keep) if a.tf_keep else None
    conns, msgs = read_raw(a.src, a.topics, a.start, a.end, keep)
    if not msgs:
        raise SystemExit("没有符合条件的消息")
    if a.append_zero_sec > 0:
        nid, sec, nsec, data = msgs[-1]
        if len(data) != 48:
            raise SystemExit("--append-zero-sec 只支持 geometry_msgs/Twist（48 字节）")
        t = sec * 1_000_000_000 + nsec
        for _ in range(int(round(a.append_zero_sec * 50))):
            t += 20_000_000
            msgs.append((nid, t // 1_000_000_000, t % 1_000_000_000, bytes(48)))
    size, nchunk = write_bag(a.dst, conns, msgs)
    span = (msgs[-1][1] + msgs[-1][2] * 1e-9) - (msgs[0][1] + msgs[0][2] * 1e-9)
    print(f"写出 {a.dst}：{len(msgs)} 条，{nchunk} 块，{span:.1f} s，{size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
