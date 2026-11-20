import { useEffect, useState } from "react";
import { Cloud, KeyRound, RefreshCw, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/field";
import { api, cn } from "@/lib";
import { ask } from "@/components/Confirm";

export type VideoCfg = {
  host: string;
  path: string;
  sn: string;
  local_codec: string;
  remote: "aliyun" | "tencent" | null;
  remote_name: string;
  remote_codec: string | null;
  remote_error: string;
  node: { state: string; start_time: string; raw: string };
  backups: string[];
  sudo_ready: boolean;
  message?: string;
};

const PROVIDER: Record<string, "aliyun" | "tencent"> = { "远程 阿里云": "aliyun", "远程 腾讯云": "tencent" };

/** 要测的链路、编码和机器人实际配置一致时返回空串，否则返回原因（和后端 check_match 同一规则）。 */
export function mismatch(cfg: VideoCfg, link: string, codec: string): string {
  const c = codec.toLowerCase();
  if (link === "近场") return cfg.local_codec === c ? "" : `机器人近场是 ${cfg.local_codec.toUpperCase()}`;
  if (cfg.remote_error) return cfg.remote_error;
  const want = link.replace("远程", "").trim();
  if (cfg.remote_name !== want) return `机器人远程用的是 ${cfg.remote_name}`;
  if (cfg.remote_codec !== c) return `机器人${want}编码是 ${String(cfg.remote_codec).toUpperCase()}`;
  return "";
}

/** 机器人视频配置（1.3 上的 pico_teleop_server）：近场编码、远程用阿里云还是腾讯云及其编码。
 *  选的链路变了自动重新读取；不一致时给出一键切换，切换前要人确认并输入 1.3 的 sudo 密码（只存在本次运行内存里）。 */
export function RobotVideo({ link, codec, disabled, onAdopt, onState }: {
  link: string;
  codec: string;
  disabled?: boolean;
  onAdopt: (link: string, codec: string) => void;
  onState?: (cfg: VideoCfg | null) => void;
}) {
  const [cfg, setCfgRaw] = useState<VideoCfg | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [pw, setPw] = useState("");

  const setCfg = (c: VideoCfg | null) => {
    setCfgRaw(c);
    onState?.(c);
  };

  const run = async (fn: () => Promise<VideoCfg>, keepMsg = false) => {
    setBusy(true);
    setErr("");
    if (!keepMsg) setMsg("");
    try {
      const r = await fn();
      setCfg(r);
      if (r.message) setMsg(r.message);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const read = () => run(() => api<VideoCfg>("/robotcfg"), true);

  useEffect(() => {
    read();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [link]);

  const unlock = async () => {
    setBusy(true);
    setErr("");
    try {
      await api("/robotcfg/sudo", { body: { password: pw } });
      setPw("");
      if (cfg) setCfg({ ...cfg, sudo_ready: true });
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const remote = link !== "近场";
  const target = codec.toLowerCase();
  const why = cfg ? mismatch(cfg, link, codec) : "";
  const badRemoteCodec = remote && target === "jpeg";

  const switchNow = async () => {
    if (!cfg) return;
    if (remote) {
      const p = PROVIDER[link];
      const from = `${cfg.remote_name} ${String(cfg.remote_codec ?? "").toUpperCase()}`;
      if (!(await ask(`把 ${cfg.sn} 的远程从 ${from} 切到 ${link.replace("远程 ", "")} ${codec}，并重启视频服务？\n会改 1.3 上的 ${cfg.path}，改之前自动备份。Pico 需要重新连接。`, { ok: "切换并重启" }))) return;
      run(() => api<VideoCfg>("/robotcfg/remote", { body: { provider: p, codec: target, confirmed: true } }));
    } else {
      if (!(await ask(`把 ${cfg.sn} 的近场编码从 ${cfg.local_codec} 改成 ${target}，并重启视频服务？\n会改 1.3 上的 ${cfg.path}，改之前自动备份。Pico 需要重新连接。`, { ok: "切换并重启" }))) return;
      run(() => api<VideoCfg>("/robotcfg/codec", { body: { codec: target, confirmed: true } }));
    }
  };

  const adopt = () => {
    if (!cfg) return;
    if (remote && cfg.remote && cfg.remote_codec) onAdopt(`远程 ${cfg.remote_name}`, cfg.remote_codec.toUpperCase());
    else onAdopt("近场", cfg.local_codec.toUpperCase());
  };

  return (
    <div className="mb-4 rounded-[4px] border border-line-soft bg-paper/50 px-4 py-3">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
        <span className="text-ink-soft">机器人视频配置</span>
        <span className={cn(!remote && "font-semibold")}>
          近场 <span className="num text-lg">{cfg ? cfg.local_codec.toUpperCase() : "—"}</span>
        </span>
        <span className={cn("flex items-center gap-1.5", remote && "font-semibold")}>
          <Cloud className="size-4 text-ink-soft" />
          远程 {cfg ? cfg.remote_name : "—"} <span className="num text-lg">{cfg?.remote_codec?.toUpperCase() ?? ""}</span>
        </span>
        {cfg && (
          <span className="text-xs text-ink-soft">
            视频服务 {cfg.node.state || "?"} {cfg.node.start_time && `· ${cfg.node.start_time} 启动`}
          </span>
        )}
        <span className="ml-auto flex gap-2">
          <Button size="sm" variant="ghost" disabled={busy || disabled} onClick={read}>
            <RefreshCw />
            读取
          </Button>
          {cfg && cfg.backups.length > 0 && (
            <Button
              size="sm"
              variant="outline"
              disabled={busy || disabled || !cfg.sudo_ready}
              onClick={async () => {
                if (!(await ask("用当天最早的备份还原视频配置，并重启视频服务？", { ok: "还原并重启" }))) return;
                run(() => api<VideoCfg>("/robotcfg/restore", { body: { confirmed: true } }));
              }}
            >
              <RotateCcw />
              还原原配置
            </Button>
          )}
        </span>
      </div>

      {cfg && (
        <div className="mt-2 flex flex-wrap items-center gap-3 text-sm">
          {why ? (
            <>
              <span className="text-wait">
                要测 {link} {codec}，{why}
              </span>
              <Button size="sm" variant="ghost" disabled={busy || disabled} onClick={adopt}>
                按机器人现在的配置测
              </Button>
              {badRemoteCodec ? (
                <span className="text-xs text-fail">远程只能是 H264 或 H265</span>
              ) : (
                <Button size="sm" variant="live" disabled={busy || disabled || !cfg.sudo_ready} onClick={switchNow}>
                  {remote ? `切到${link.replace("远程 ", "")} ${codec} 并重启视频服务` : `切到近场 ${codec} 并重启视频服务`}
                </Button>
              )}
            </>
          ) : (
            <span className="text-pass">和要测的一致</span>
          )}
        </div>
      )}

      {cfg && why && !cfg.sudo_ready && !badRemoteCodec && (
        <form
          className="mt-2 flex flex-wrap items-center gap-2 text-sm"
          onSubmit={(e) => {
            e.preventDefault();
            unlock();
          }}
        >
          <KeyRound className="size-4 text-ink-soft" />
          <span className="text-ink-soft">改配置要 1.3 的 sudo 密码（guest 账号），只存在本次运行的内存里，关掉软件就没了：</span>
          <Input type="password" autoComplete="off" className="h-8 w-44" value={pw} onChange={(e) => setPw(e.target.value)} />
          <Button size="sm" variant="outline" type="submit" disabled={busy || !pw}>
            确认密码
          </Button>
        </form>
      )}

      {busy && <p className="mt-2 text-xs text-ink-soft">正在通过 SSH 操作机器人 1.3…（切换含重启，约需 10–25 s）</p>}
      {msg && <p className="mt-2 text-xs text-pass">{msg}</p>}
      {err && <p role="alert" className="mt-2 text-xs text-fail">{err}</p>}
      {cfg?.remote_error && <p role="alert" className="mt-2 text-xs text-fail">{cfg.remote_error}</p>}
      <p className="mt-1 text-xs text-ink-soft">
        读的是 1.3（视频服务所在的板子）上的 {cfg?.path ?? "配置文件"}。开始测量前软件会再核对一次，不一致不让测。近场配置写的是“头显不支持该编码时自动退回 JPEG”，切换后在 Pico 上确认画面正常再测。
      </p>
    </div>
  );
}
