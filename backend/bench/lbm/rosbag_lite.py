#!/usr/bin/env python3
"""不依赖 ROS 的 ROS1 bag v2.0 读取器（仅支持未压缩 chunk），按消息定义解码指定话题。

示例：
  python3 scripts/rosbag_lite.py info  xxx.bag
  python3 scripts/rosbag_lite.py dump  xxx.bag /ImuData --limit 3
"""

import argparse
import mmap
import json
import re
import struct
import sys

OP_MSG, OP_BAG_HEADER, OP_INDEX, OP_CHUNK, OP_CHUNK_INFO, OP_CONNECTION = 2, 3, 4, 5, 6, 7

PRIMITIVES = {
    "bool": "?", "int8": "b", "uint8": "B", "byte": "b", "char": "B",
    "int16": "h", "uint16": "H", "int32": "i", "uint32": "I",
    "int64": "q", "uint64": "Q", "float32": "f", "float64": "d",
}


def _fields(buffer):
    result, i = {}, 0
    while i < len(buffer):
        (length,) = struct.unpack_from("<I", buffer, i)
        key, _, value = bytes(buffer[i + 4:i + 4 + length]).partition(b"=")
        result[key.decode()] = value
        i += 4 + length
    return result


def _records(data, offset=0, end=None):
    end = len(data) if end is None else end
    while offset + 4 <= end:
        (header_len,) = struct.unpack_from("<I", data, offset)
        header = _fields(data[offset + 4:offset + 4 + header_len])
        offset += 4 + header_len
        (data_len,) = struct.unpack_from("<I", data, offset)
        offset += 4
        yield header, offset, data_len
        offset += data_len


class MessageType:
    """把 ROS1 message_definition 解析为可解码的结构。"""

    def __init__(self, type_name, definition):
        self.types = {}
        sections = re.split(r"^=+\s*$", definition, flags=re.M)
        main_pkg = type_name.split("/")[0]
        self.types[type_name] = self._parse(sections[0], main_pkg)
        for section in sections[1:]:
            match = re.search(r"^MSG:\s*(\S+)", section, flags=re.M)
            if match:
                name = match.group(1)
                self.types[name] = self._parse(section[match.end():], name.split("/")[0])
        self.root = type_name

    def _parse(self, text, package):
        fields = []
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2 or "=" in parts[1] or (len(parts) > 2 and parts[2].startswith("=")):
                continue  # 常量
            ftype, fname = parts[0], parts[1]
            array = None
            match = re.match(r"^(.+)\[(\d*)\]$", ftype)
            if match:
                ftype, size = match.group(1), match.group(2)
                array = int(size) if size else -1
            if ftype == "Header":
                ftype = "std_msgs/Header"
            elif ftype not in PRIMITIVES and ftype not in ("string", "time", "duration") and "/" not in ftype:
                ftype = f"{package}/{ftype}"
            fields.append((fname, ftype, array))
        return fields

    def _resolve(self, name):
        if name in self.types:
            return self.types[name]
        short = name.split("/")[-1]
        for key, value in self.types.items():
            if key.split("/")[-1] == short:
                return value
        raise KeyError(f"消息定义中缺少 {name}")

    def decode(self, buffer):
        value, _ = self._decode(self.root, buffer, 0)
        return value

    def _decode_one(self, ftype, buffer, offset):
        if ftype in PRIMITIVES:
            code = "<" + PRIMITIVES[ftype]
            return struct.unpack_from(code, buffer, offset)[0], offset + struct.calcsize(code)
        if ftype == "string":
            (length,) = struct.unpack_from("<I", buffer, offset)
            return bytes(buffer[offset + 4:offset + 4 + length]).decode("utf-8", "replace"), offset + 4 + length
        if ftype in ("time", "duration"):
            sec, nsec = struct.unpack_from("<Ii" if ftype == "duration" else "<II", buffer, offset)
            return sec + nsec * 1e-9, offset + 8
        return self._decode(ftype, buffer, offset)

    def _decode(self, type_name, buffer, offset):
        result = {}
        for fname, ftype, array in self._resolve(type_name):
            if array is None:
                result[fname], offset = self._decode_one(ftype, buffer, offset)
                continue
            if array < 0:
                (count,) = struct.unpack_from("<I", buffer, offset)
                offset += 4
            else:
                count = array
            if ftype in PRIMITIVES and ftype not in ("bool",):
                code = f"<{count}{PRIMITIVES[ftype]}"
                if ftype in ("uint8", "char", "byte"):
                    result[fname] = bytes(buffer[offset:offset + count])
                else:
                    result[fname] = list(struct.unpack_from(code, buffer, offset))
                offset += struct.calcsize(code)
            else:
                items = []
                for _ in range(count):
                    item, offset = self._decode_one(ftype, buffer, offset)
                    items.append(item)
                result[fname] = items
        return result, offset


class Bag:
    def __init__(self, path):
        with open(path, "rb") as handle:
            head = handle.readline()
            if head != b"#ROSBAG V2.0\n":
                raise ValueError(f"{path} 不是 ROS bag v2.0")
            # mmap 按需读盘，几 GB 的全录包也不会占满内存
            self._mm = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
        self.data = memoryview(self._mm)[len(head):]
        self.connections = {}

    def _connection(self, buffer, header, start, length):
        conn = struct.unpack("<I", header["conn"])[0]
        if conn not in self.connections:
            info = _fields(buffer[start:start + length])
            self.connections[conn] = {
                "topic": header["topic"].decode(),
                "type": info["type"].decode(),
                "definition": info.get("message_definition", b"").decode(),
                "decoder": None,
            }
        return conn

    def read(self, topics=None):
        """按文件顺序产出 (topic, 记录时间秒, 解码后的 dict)。"""
        topics = set(topics) if topics else None
        for header, start, length in _records(self.data):
            op = header["op"][0]
            if op == OP_CONNECTION:
                self._connection(self.data, header, start, length)
            elif op == OP_CHUNK:
                compression = header.get("compression", b"none")
                if compression != b"none":
                    raise ValueError(f"不支持压缩格式 {compression.decode()}")
                chunk = self.data[start:start + length]
                for sub, sub_start, sub_len in _records(chunk):
                    sub_op = sub["op"][0]
                    if sub_op == OP_CONNECTION:
                        self._connection(chunk, sub, sub_start, sub_len)
                    elif sub_op == OP_MSG:
                        conn = self.connections[struct.unpack("<I", sub["conn"])[0]]
                        if topics is not None and conn["topic"] not in topics:
                            continue
                        if conn["decoder"] is None:
                            conn["decoder"] = MessageType(conn["type"], conn["definition"])
                        sec, nsec = struct.unpack("<II", sub["time"])
                        yield conn["topic"], sec + nsec * 1e-9, conn["decoder"].decode(chunk[sub_start:sub_start + sub_len])

    def summary(self):
        counts, first, last = {}, {}, {}
        for header, start, length in _records(self.data):
            if header["op"][0] != OP_CHUNK:
                if header["op"][0] == OP_CONNECTION:
                    self._connection(self.data, header, start, length)
                continue
            chunk = self.data[start:start + length]
            for sub, sub_start, sub_len in _records(chunk):
                if sub["op"][0] == OP_CONNECTION:
                    self._connection(chunk, sub, sub_start, sub_len)
                elif sub["op"][0] == OP_MSG:
                    topic = self.connections[struct.unpack("<I", sub["conn"])[0]]["topic"]
                    sec, nsec = struct.unpack("<II", sub["time"])
                    t = sec + nsec * 1e-9
                    counts[topic] = counts.get(topic, 0) + 1
                    first.setdefault(topic, t)
                    last[topic] = t
        return counts, first, last


def main(argv=None):
    parser = argparse.ArgumentParser(description="ROS1 bag v2.0 轻量读取")
    sub = parser.add_subparsers(dest="cmd", required=True)
    info = sub.add_parser("info")
    info.add_argument("bag")
    dump = sub.add_parser("dump")
    dump.add_argument("bag")
    dump.add_argument("topic")
    dump.add_argument("--limit", type=int, default=5)
    args = parser.parse_args(argv)
    bag = Bag(args.bag)
    if args.cmd == "info":
        counts, first, last = bag.summary()
        for topic in sorted(counts):
            span = last[topic] - first[topic]
            rate = (counts[topic] - 1) / span if span > 0 else 0
            ctype = next(c["type"] for c in bag.connections.values() if c["topic"] == topic)
            print(f"{topic}\t{ctype}\t{counts[topic]}\t{first[topic]:.3f}-{last[topic]:.3f}\t{rate:.1f} Hz")
    else:
        for i, (topic, t, msg) in enumerate(bag.read([args.topic])):
            if i >= args.limit:
                break
            print(f"{t:.6f}", json.dumps(msg, ensure_ascii=False, default=lambda b: b.hex()[:64])[:1500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
