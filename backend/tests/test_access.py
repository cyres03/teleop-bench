"""软件内授权机器人：记主机指纹、把公钥加进 authorized_keys、密码错给中文提示。"""
import paramiko
import pytest

from bench import access, config


class FakeChan:
    def recv_exit_status(self):
        return 0


class FakeOut:
    channel = FakeChan()

    def read(self):
        return b""


class FakeClient:
    sent: list = []
    password = "right"

    def set_missing_host_key_policy(self, p):
        pass

    def connect(self, host, username, password, **kw):
        if password != self.password:
            raise paramiko.AuthenticationException()

    def get_transport(self):
        key = paramiko.Ed25519Key.from_private_key_file(str(access._key()))
        return type("T", (), {"get_remote_server_key": lambda s: key})()

    def exec_command(self, cmd, timeout=None):
        FakeClient.sent.append(cmd)
        return None, FakeOut(), FakeOut()

    def close(self):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEOP_BENCH_HOME", str(tmp_path / "bench"))
    monkeypatch.setattr(access, "KH", tmp_path / "kh")
    monkeypatch.setattr(config, "DEFAULTS", {**config.DEFAULTS, "ssh_key": str(tmp_path / "k")})
    monkeypatch.setattr(paramiko, "SSHClient", FakeClient)
    monkeypatch.setattr(access, "key_ok", lambda h, u: True)
    FakeClient.sent = []
    return tmp_path


def test_authorize_records_host_and_adds_key(env):
    access.authorize("10.192.1.2", "limx", "right")
    pub = (env / "k.pub").read_text().strip()
    assert (env / "kh").read_text().startswith("10.192.1.2 ssh-ed25519 ")
    assert pub in FakeClient.sent[0] and "authorized_keys" in FakeClient.sent[0]
    access.authorize("10.192.1.2", "limx", "right")
    assert len((env / "kh").read_text().splitlines()) == 1


def test_wrong_password_message(env):
    with pytest.raises(ValueError, match="limx 的密码不对"):
        access.authorize("10.192.1.2", "limx", "wrong")
