import { useEffect, useState } from "react";
import { KeyRound, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/field";
import { api } from "@/lib";

type Access = { sn: string; main: boolean; video: boolean };

// 第一次连某台机器人：在软件里输一次密码，代替终端里的 robot-access.sh
export function AccessBar({ sn, simulated }: { sn: string; simulated: boolean }) {
  const [a, setA] = useState<Access | null>(null);
  const [limx, setLimx] = useState("");
  const [guest, setGuest] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    setA(null);
    setMsg(null);
    if (!sn || simulated) return;
    api<Access>("/robot/access").then(setA).catch(() => undefined);
  }, [sn, simulated]);

  if (msg?.ok) return <div className="bg-pass px-5 py-1.5 text-sm text-white">{msg.text}</div>;
  if (!a || a.main) return null;

  const go = async () => {
    setBusy(true);
    setMsg(null);
    try {
      const r = await api<{ text: string }>("/robot/access", { body: { limx, guest } });
      setMsg({ ok: true, text: r.text });
      setLimx("");
      setGuest("");
    } catch (e) {
      setMsg({ ok: false, text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-wrap items-center gap-3 border-b border-wait/40 bg-[#fff6e0] px-5 py-2 text-sm">
      <KeyRound className="size-4 text-wait" />
      <span className="font-medium">{a.sn} 第一次用，输一次机器人密码授权（问 Cyres），以后不用再输：</span>
      <Input type="password" className="h-8 w-[150px]" placeholder="limx 密码" value={limx} onChange={(e) => setLimx(e.target.value)} onKeyDown={(e) => e.key === "Enter" && limx && go()} />
      <Input type="password" className="h-8 w-[170px]" placeholder="guest 密码（可不填）" value={guest} onChange={(e) => setGuest(e.target.value)} />
      <Button size="sm" variant="primary" disabled={!limx || busy} onClick={go}>
        {busy && <Loader2 className="animate-spin" />}
        授权
      </Button>
      {msg && !msg.ok && (
        <span role="alert" className="text-fail">
          {msg.text}
        </span>
      )}
    </div>
  );
}
