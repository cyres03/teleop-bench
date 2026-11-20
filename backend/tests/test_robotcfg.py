"""视频配置（1.3）：解析、改写、核对，以及用假 1.3（SSH + SFTP + sudo + mrosnode）把切换流程端到端跑一遍。"""
import hashlib
import io
import re
from pathlib import Path

import pytest

from bench import robotcfg

FIX = (Path(__file__).parent / "fixtures" / "oli_edu_mros_26.8.2.yaml").read_text()
PATH = "/opt/limx/install/etc/livekit/configs/oli_edu_mros.yaml"
PW = "right-pw"


# ---------------- 纯函数 ----------------
def test_parse_real_26_8_2_config():
    s = robotcfg.parse_cfg(FIX)
    assert (s["local_codec"], s["remote"], s["remote_name"], s["remote_codec"]) == ("jpeg", "tencent", "腾讯云", "h265")
    assert s["livekit_url"] == "ws://rtc.limxdynamics.com:7880" and s["remote_error"] == ""


def test_edit_changes_only_target_lines_and_keeps_comments():
    out = robotcfg.edit_cfg(FIX, remote="aliyun", remote_codec="h264")
    diff = [(a, b) for a, b in zip(FIX.splitlines(), out.splitlines()) if a != b]
    assert len(out.splitlines()) == len(FIX.splitlines())
    assert diff == [("    enabled: true", "    enabled: false"), ("    enabled: false", "    enabled: true"),
                    ("    codec: h265", "    codec: h264")]
    s = robotcfg.parse_cfg(out)
    assert (s["remote_name"], s["remote_codec"], s["trro"]["codec"]) == ("阿里云", "h264", "h265")
    out2 = robotcfg.edit_cfg(out, local_codec="h265")
    line = [x for x in out2.splitlines() if x.startswith("  codec:")][0]
    assert line.startswith("  codec: h265 #") and "头显不支持" in line


def test_edit_rejects_bad_values_and_changed_format():
    with pytest.raises(ValueError):
        robotcfg.edit_cfg(FIX, local_codec="av1")
    with pytest.raises(ValueError):
        robotcfg.edit_cfg(FIX, remote="aws")
    with pytest.raises(ValueError):
        robotcfg.edit_cfg(FIX, remote="aliyun", remote_codec="jpeg")
    with pytest.raises(ValueError, match="找不到要改的行"):
        robotcfg.edit_cfg(FIX.replace("remote:", "remote_video:"), remote="aliyun")


def test_conflict_and_check_match():
    both = FIX.replace("  livekit:\n    enabled: false", "  livekit:\n    enabled: true")
    assert "同时" in robotcfg.parse_cfg(both)["remote_error"]
    s = robotcfg.parse_cfg(FIX)
    assert robotcfg.check_match(s, "近场", "JPEG") == ""
    assert "H264" in robotcfg.check_match(s, "近场", "H264")
    assert "腾讯云" in robotcfg.check_match(s, "远程 阿里云", "H264")
    assert "H265" in robotcfg.check_match(s, "远程 腾讯云", "H264")
    assert robotcfg.check_match(s, "远程 腾讯云", "H265") == ""


# ---------------- 假 1.3 ----------------
class _Chan:
    def __init__(self, code):
        self.code = code

    def recv_exit_status(self):
        return self.code

    def shutdown_write(self):
        pass


class _Out(io.BytesIO):
    def __init__(self, data, code):
        super().__init__(data.encode())
        self.channel = _Chan(code)


class _In(io.StringIO):
    def __init__(self):
        super().__init__()
        self.channel = _Chan(0)


class Fake13:
    def __init__(self, sn="HU_D04_01_060", restart_ok=True):
        self.fs = {PATH: FIX, robotcfg.TRRO_JSON: TRRO_FIX.replace('"codec": 0', '"codec": 1')}
        self.sn = sn
        self.restart_ok = restart_ok
        self.start = "2026-11-10 10:11:27"
        self.n = 0
        self.log = []

    # paramiko 接口
    def exec_command(self, cmd, timeout=None):
        stdin = _In()
        self._pending = (cmd, stdin)
        return stdin, _Lazy(self, cmd, stdin, 0), _Lazy(self, cmd, stdin, 1)

    def open_sftp(self):
        fake = self

        class S:
            def open(self, p, mode):
                class W(io.BytesIO):
                    def __exit__(s, *a):
                        fake.fs[p] = s.getvalue().decode()
                return W()

            def close(self):
                pass
        return S()

    def close(self):
        pass

    # 命令解释
    def run(self, cmd, stdin):
        self.log.append(cmd)
        if "CONFIG_FILE" in cmd:
            return "oli_edu_mros.yaml\n", "", 0
        if "sn.yaml" in cmd:
            return self.sn + "\n", "", 0
        if cmd.startswith("sudo -n install"):
            m = re.search(r"install -m 644 -o root -g root (\S+) (\S+)", cmd)
            self.fs[m.group(2)] = self.fs[m.group(1)]
            return "", "", 0
        if cmd.startswith("sudo -S"):
            if stdin.strip() != PW:
                return "", "Sorry, try again.\nsudo: 1 incorrect password attempt", 1
            m = re.search(r"install -m 644 -o root -g root (\S+) (\S+)", cmd)
            if m:
                self.fs[m.group(2)] = self.fs[m.group(1)]
            return "", "", 0
        if cmd.startswith("cat "):
            soft = cmd.endswith("|| true")
            p = cmd[4:].replace(" 2>/dev/null || true", "").strip().strip("'")
            p = p.replace("$(basename " + PATH + ")", "oli_edu_mros.yaml")
            return (self.fs.get(p, "") if soft else self.fs[p]), "", 0
        if "backup_video_*" in cmd:
            ks = sorted(k.rsplit("/", 1)[0] for k in self.fs if "backup_video_" in k)
            return "\n".join(dict.fromkeys(ks)) + "\n", "", 0
        if "cp -p" in cmd:
            d = re.search(r"mkdir -p (\S+);", cmd).group(1)
            src = re.search(r"cp -p (\S+) ", cmd).group(1)
            self.fs.setdefault(f"{d}/{src.rsplit('/', 1)[1]}", self.fs[src])
            return "", "", 0
        if cmd.startswith("md5sum"):
            p = cmd.split()[1]
            return hashlib.md5(self.fs[p].encode()).hexdigest() + "  " + p, "", 0
        if "mrosnode restart" in cmd:
            if self.restart_ok:
                self.n += 1
            return "", "", 0
        if "pgrep -f" in cmd:
            return f"{5591 + self.n} Tue Nov 10 10:2{self.n}:00 2026\n", "", 0
        if cmd.startswith(("rm -f", "mkdir -p")):
            self.fs = {k: v for k, v in self.fs.items() if not (cmd.startswith("rm -f") and k == cmd.split()[-1])}
            return "", "", 0
        raise AssertionError(f"假 1.3 不认识的命令：{cmd}")


class _Lazy:
    """stdout / stderr：第一次读时才执行命令（这时 stdin 已经写完）。"""
    def __init__(self, fake, cmd, stdin, idx):
        self.fake, self.cmd, self.stdin, self.idx = fake, cmd, stdin, idx
        self._res = None
        self.channel = self

    def _get(self):
        key = (self.cmd, id(self.stdin))
        cache = self.fake.__dict__.setdefault("_cache", {})
        if key not in cache:
            cache[key] = self.fake.run(self.cmd, self.stdin.getvalue())
        return cache[key]

    def read(self):
        return self._get()[self.idx].encode()

    def recv_exit_status(self):
        return self._get()[2]


@pytest.fixture()
def fake(monkeypatch):
    f = Fake13()
    monkeypatch.setattr(robotcfg, "_client", lambda: f)
    monkeypatch.setattr(robotcfg, "_writer", lambda: f)
    monkeypatch.setattr(robotcfg.time, "sleep", lambda s: None)
    monkeypatch.setattr(robotcfg, "RESTART_WAIT_S", 0.2)
    robotcfg.forget_sudo_password()
    yield f
    robotcfg.forget_sudo_password()


def test_switch_remote_end_to_end(fake):
    r = robotcfg.set_remote("aliyun", "h264", expected_sn="HU_D04_01_060")
    assert (r["remote_name"], r["remote_codec"]) == ("阿里云", "h264")
    assert "阿里云 H264" in r["message"] and "重启" in r["message"]
    assert any("backup_video_" in k for k in fake.fs)  # 改前备份了原文件
    # 已经是这个配置：不改、不重启
    n = fake.n
    r = robotcfg.set_remote("aliyun", "h264", expected_sn="HU_D04_01_060")
    assert "没有改动" in r["message"] and fake.n == n
    # 还原：回到腾讯云 H265
    r = robotcfg.restore(expected_sn="HU_D04_01_060")
    assert (r["remote_name"], r["remote_codec"]) == ("腾讯云", "h265") and fake.fs[robotcfg.CFG_DIR + "/oli_edu_mros.yaml"] == FIX


def test_switch_fails_loudly(fake):
    with pytest.raises(ValueError, match="不是界面上识别的"):
        robotcfg.set_local_codec("h264", expected_sn="HU_D04_01_660")
    fake.restart_ok = False
    with pytest.raises(RuntimeError, match="没有重启成功"):
        robotcfg.set_local_codec("h264", expected_sn="HU_D04_01_060")


def test_measure_refuses_when_robot_config_differs(fake, monkeypatch, tmp_path):
    """正式测量前核对：机器人是腾讯云 H265，选“远程 阿里云 H264”时不让测；跳过核对时记进备注。"""
    import asyncio
    monkeypatch.setenv("TELEOP_BENCH_HOME", str(tmp_path))
    from bench import app as appmod
    with pytest.raises(ValueError, match="腾讯云"):
        asyncio.run(appmod._robot_video_note("远程 阿里云 H264", False))
    note = asyncio.run(appmod._robot_video_note("远程 阿里云 H264", True))
    assert "手动跳过核对" in note
    ok = asyncio.run(appmod._robot_video_note("远程 腾讯云 H265", False))
    assert ok.startswith("机器人配置") and "腾讯云 H265" in ok


TRRO_FIX = """{
  "cloud_mode": "public",
  "device_streams": 3,
  "streams_config": [
    {
      "protocol": "outside",
      "fps": 30,
      "kbps": 4000,
      "codec": 0
    },
    {
      "protocol": "outside",
      "kbps": 500,
      "codec": 0
    },
    {
      "protocol": "outside",
      "kbps": 500,
      "codec": 0
    }
  ]
}
"""


def test_trro_codec_and_edit():
    assert robotcfg.trro_codec(TRRO_FIX) == "h264"
    out = robotcfg.edit_trro_json(TRRO_FIX, "h265")
    assert robotcfg.trro_codec(out) == "h265"
    # 只改 codec 的数字，其他内容和格式不变
    assert out.replace('"codec": 1', '"codec": 0') == TRRO_FIX
    assert robotcfg.edit_trro_json(out, "h264") == TRRO_FIX
    mixed = TRRO_FIX.replace('"codec": 0\n    },\n    {\n      "protocol": "outside",\n      "kbps": 500,\n      "codec": 0',
                             '"codec": 1\n    },\n    {\n      "protocol": "outside",\n      "kbps": 500,\n      "codec": 0', 1)
    assert robotcfg.trro_codec(mixed) == "mixed"
    with pytest.raises(ValueError):
        robotcfg.edit_trro_json(TRRO_FIX, "jpeg")
    with pytest.raises(RuntimeError):
        robotcfg.edit_trro_json(TRRO_FIX.replace('"kbps": 4000,', '"kbps": 4000, "codec_note": 0, "codec": 0,'), "h265")


def test_switch_tencent_codec_edits_trro_json(fake):
    orig = fake.fs[robotcfg.TRRO_JSON]
    r = robotcfg.read_state()
    assert (r["remote_name"], r["remote_codec"], r["trro_json_codec"]) == ("腾讯云", "h265", "h265")
    r = robotcfg.set_remote("tencent", "h264", expected_sn="HU_D04_01_060")
    assert robotcfg.trro_codec(fake.fs[robotcfg.TRRO_JSON]) == "h264"
    assert (r["remote_codec"], r["trro_json_codec"]) == ("h264", "h264") and "腾讯云 H264" in r["message"]
    assert any(k.endswith("/config.json") and "backup_video_" in k for k in fake.fs)
    r = robotcfg.restore(expected_sn="HU_D04_01_060")
    assert fake.fs[robotcfg.TRRO_JSON] == orig and r["remote_codec"] == "h265"
