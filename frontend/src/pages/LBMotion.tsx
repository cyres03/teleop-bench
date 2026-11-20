import { useEffect, useRef, useState } from "react";
import { Check as CheckIcon, ClipboardCopy, Download, FileSearch, FolderOpen, Loader2, Play, RefreshCw, Square, Trash2, Wifi, X } from "lucide-react";
import { PageHead } from "@/App";
import type { RobotInfo } from "@/components/RobotStrip";
import { choose } from "@/components/Confirm";
import { StageProgress } from "@/components/StageProgress";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Field, Input } from "@/components/ui/field";
import { api, clock, cn } from "@/lib";

type Item = { key: string; stick: string; space: string; steps: string[] };
type Stat = { median: number; min: number; max: number; n: number } | null;
type Row = { kind: string; unit: string; n: number; n_steady: number; n_not_stopped: number } & Record<string, Stat | number | string>;
type Seg = Record<string, number | string | boolean | null>;
type Trial = { id: number; take: number; item: string; name: string; started: number; excluded?: boolean; exclude_note?: string; aborted?: boolean; ask?: boolean; main: Seg };
type Take = { id: number; item: string; reps: number; name: string; started: number; bag: string; md5: string; size: number; status: string; note: string; n_found?: number; local_bag?: string };
type Check = { name: string; ok: boolean | null; detail: string };
type Live = {
  item: string;
  reps: number;
  name: string;
  elapsed: number;
  pushes: number[];
  wrong: number;
  phase: string;
  text: string;
  pushing_s: number;
  steps: string[];
  max_s: number;
};
type Stage = { name: string; state: "wait" | "run" | "ok" | "fail"; detail?: string };
type Finish = {
  running: boolean;
  auto?: boolean;
  elapsed?: number;
  total?: number;
  item: string;
  name: string;
  stages: Stage[];
  result: { found: number; reps: number; summary: Row[] } | null;
  report: string;
  error: string;
};
type Script = { key: string; what: string; space: string; kinds: string[]; fixed_s: number; per_rep: number };
type State = {
  session: string;
  scripts: Script[];
  simulated: boolean;
  robot: string;
  items: Item[];
  push_s: number;
  prep: { running: boolean; ok: boolean; checks: Check[] };
  live: Live | null;
  finish: Finish | null;
  error: string;
  fetching: string;
  trials: Trial[];
  takes: Take[];
  summary: Row[];
  report_path: string;
};
type Hotspots = { device: string; usb: boolean; current: string; list: { ssid: string; signal: number; saved: boolean; in_use: boolean }[]; error: string };

function beep(freq: number, ms: number) {
  try {
    const ctx = new AudioContext();
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.frequency.value = freq;
    g.gain.value = 0.2;
    o.connect(g).connect(ctx.destination);
    o.start();
    o.stop(ctx.currentTime + ms / 1000);
    o.onended = () => ctx.close();
  } catch {
    /* 没有声卡就不响 */
  }
}

const num = (v: Stat | undefined, d = 2) => (v ? v.median.toFixed(d) : "—");
const rng = (v: Stat | undefined, d = 2) => (v && v.n > 1 ? `${v.min.toFixed(d)} – ${v.max.toFixed(d)}` : "");
const pct = (v: Stat | undefined) => (v ? `${(v.median * 100).toFixed(1)}%` : "—");
const pctRng = (v: Stat | undefined) => (v && v.n > 1 ? `${(v.min * 100).toFixed(1)} – ${(v.max * 100).toFixed(1)}%` : "");
const mmss = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

export default function LBMotion({ robot, simulated }: { robot: RobotInfo | null; simulated: boolean }) {
  const [s, setS] = useState<State | null>(null);
  const [item, setItem] = useState("前进");
  const [reps, setReps] = useState("5");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const poll = () => api<State>("/lbm").then(setS).catch(() => undefined);
    poll();
    const id = setInterval(poll, 400);
    return () => clearInterval(id);
  }, [simulated, robot?.sn]);

  const call = async (p: string, body: unknown = {}, method?: string) => {
    setError("");
    setBusy(true);
    try {
      setS(await api<State>(p, method ? { method } : { body }));
      return true;
    } catch (e) {
      setError((e as Error).message);
      return false;
    } finally {
      setBusy(false);
    }
  };

  const asked = useRef(new Set<number>());
  const pending = s && !s.live && !s.finish ? s.trials.find((t) => t.ask && !asked.current.has(t.take)) : undefined;
  useEffect(() => {
    if (!pending || !s) return;
    asked.current.add(pending.take);
    const ts = s.trials.filter((t) => t.take === pending.take);
    const when = clock(pending.started);
    choose(`${when} 那次自动推杆中途停止了，停止前做完 ${ts.length} 次推杆（${ts.map((t) => t.item).join("、")}）。\n保存：计入报告。\n不保存：不进报告，录包和原始记录照样留着。`, {
      title: "中途停止的这次要保存吗",
      yes: "保存",
      no: "不保存",
    }).then((v) => v !== null && call("/lbm/keep", { id: pending.take, keep: v }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pending?.take]);

  if (!s) return null;
  const sel = s.items.find((i) => i.key === item)!;
  const done = (k: string) => s.trials.filter((t) => t.item === k && !t.excluded).length;
  const dialogOpen = !!s.live || !!s.finish;

  return (
    <>
      <PageHead title="VR部分动捕" desc="测 VR部分动捕下摇杆指令（/vr_cmd）的跟随：指令、LBMotion 内部、机器人实际各是多少，松手后多久停住。一键测由软件代替手柄推杆；也可以人拿 Pico 手动推：选测试项点“开始录包”，按弹窗提示推杆，做完点“结束并出报告”。">
        {s.report_path && (
          <Button variant="outline" onClick={() => api("/lbm/open", { body: {} })}>
            <FolderOpen />
            打开报告文件夹
          </Button>
        )}
      </PageHead>

      <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1.25fr)] gap-6">
        <RobotCard s={s} onRecheck={() => call("/lbm/prepare")} onConnected={(st) => setS(st)} />

        <section className="rounded-md border border-line bg-panel p-5">
          <AutoCard s={s} busy={busy || dialogOpen} call={call} />
          {(error || s.error) && !dialogOpen && <p role="alert" className="mt-2 text-sm text-fail">{error || s.error}</p>}
          <details className="mt-5 border-t border-line-soft pt-4">
          <summary className="cursor-pointer text-sm text-ink-soft">手动：人拿 Pico 推杆</summary>
          <h2 className="mb-3 mt-3 text-[15px] font-semibold">选测试项</h2>
          <div className="grid grid-cols-3 gap-1.5" role="radiogroup" aria-label="测试项">
            {s.items.map((i) => (
              <button
                key={i.key}
                role="radio"
                aria-checked={item === i.key}
                onClick={() => setItem(i.key)}
                className={cn("rounded-[4px] border px-3 py-2.5 text-left", item === i.key ? "border-ink bg-ink text-panel" : "border-line hover:bg-line-soft")}
              >
                <div className="flex items-baseline justify-between text-[15px] font-medium">
                  {i.key}
                  <span className={cn("num text-xs font-normal", item === i.key ? "text-panel/70" : "text-ink-soft")}>已有 {done(i.key)} 次</span>
                </div>
                <div className={cn("mt-0.5 text-xs", item === i.key ? "text-panel/80" : "text-ink-soft")}>{i.stick}</div>
              </button>
            ))}
          </div>
          <div className="mt-4 flex items-end gap-3">
            <Field label="做几次" className="w-[96px]">
              <Input className="num h-11 text-center text-base" inputMode="numeric" value={reps} onChange={(e) => setReps(e.target.value.replace(/\D/g, ""))} />
            </Field>
            <Button
              variant="primary"
              size="lg"
              className="h-11 flex-1 text-base"
              disabled={!s.prep.ok || busy || dialogOpen}
              onClick={() => call("/lbm/begin", { item, reps: Number(reps) || 5 })}
            >
              <Play />
              开始录包：{item} × {Number(reps) || 5}
            </Button>
          </div>
          <ol className="mt-3 list-decimal space-y-1 pl-5 text-sm text-ink-soft">
            {sel.steps.map((t) => (
              <li key={t}>{t.replace("一共 5 次", `一共 ${Number(reps) || 5} 次`)}</li>
            ))}
          </ol>
          </details>
        </section>
      </div>

      {s.summary.length > 0 && <Results s={s} call={call} />}

      <Dialog open={dialogOpen} onOpenChange={(o) => !o && !s.live && !s.finish?.running && call("/lbm/close")}>
        {dialogOpen && (
          <DialogContent autoFocus={false} title={s.live ? `正在录包：${s.live.item}` : s.finish!.auto ? `自动推杆：${s.finish!.item}` : `出报告：${s.finish!.item}`} className="w-[min(720px,94vw)]" closable={!s.live && !s.finish?.running}>
            {s.live ? <Recording live={s.live} error={error || s.error} busy={busy} call={call} /> : <Finishing f={s.finish!} call={call} />}
          </DialogContent>
        )}
      </Dialog>
    </>
  );
}

function AutoCard({ s, busy, call }: { s: State; busy: boolean; call: (p: string, body?: unknown) => Promise<boolean> }) {
  const [key, setKey] = useState(s.scripts[0]?.key ?? "");
  const [reps, setReps] = useState(3);
  const sel = s.scripts.find((x) => x.key === key) ?? s.scripts[0];
  if (!sel) return null;
  const secs = sel.fixed_s + sel.per_rep * reps;
  return (
    <div>
      <h2 className="text-[15px] font-semibold">一键测（软件推杆，不用人拿 Pico）</h2>
      <p className="mb-3 text-xs text-ink-soft">软件替手柄发固定的摇杆指令，每个版本输入完全一样。前后、左右成对来回，机器人走完基本回到原地。</p>
      <div className="grid grid-cols-3 gap-1.5" role="radiogroup" aria-label="自动脚本">
        {s.scripts.map((x) => (
          <button key={x.key} role="radio" aria-checked={key === x.key} onClick={() => setKey(x.key)} className={cn("rounded-[4px] border px-3 py-2.5 text-left", key === x.key ? "border-ink bg-ink text-panel" : "border-line hover:bg-line-soft")}>
            <div className="text-[15px] font-medium">{x.key}</div>
            <div className={cn("mt-0.5 text-xs", key === x.key ? "text-panel/80" : "text-ink-soft")}>{x.what}</div>
          </button>
        ))}
      </div>
      <ol className="mt-3 list-decimal space-y-1 pl-5 text-sm">
        <li>机器人挂好天车，{sel.space}。天车要有人跟着推。</li>
        <li>遥控器拿在手上，随时准备急停。</li>
        <li>关掉 Pico 或退出遥操 App。</li>
        <li>机器人在 Walk 状态站稳。</li>
        <li>点“开始”。机器人会自己进 VR部分动捕、按脚本走，最后自己回 Walk，结果自动出来。</li>
      </ol>
      <div className="mt-3 flex items-end gap-3">
        <div>
          <div className="mb-1 text-xs text-ink-soft">来回几次</div>
          <div className="flex gap-1">
            {[1, 2, 3, 5].map((n) => (
              <button key={n} onClick={() => setReps(n)} className={cn("num h-11 w-11 rounded-[4px] border text-base", reps === n ? "border-ink bg-ink text-panel" : "border-line hover:bg-line-soft")}>
                {n}
              </button>
            ))}
          </div>
        </div>
        <Button variant="primary" size="lg" className="h-11 flex-1 text-base" disabled={!s.prep.ok || busy} onClick={() => call("/lbm/auto", { script: key, reps })}>
          <Play />
          开始：{key} × {reps}（约 {Math.ceil((secs + 40) / 60)} 分钟）
        </Button>
      </div>
      {!s.prep.ok && <p className="mt-2 text-sm text-wait">左边检查清单全部打勾后才能开始。</p>}
    </div>
  );
}

export function RobotCard({ s, onRecheck, onConnected }: { s: Pick<State, "prep" | "simulated" | "robot">; onRecheck: () => void; onConnected: (st: State) => void }) {
  const [wifi, setWifi] = useState<Hotspots | null>(null);
  const [scan, setScan] = useState(false);
  const [pw, setPw] = useState<Record<string, string>>({});
  const [joining, setJoining] = useState("");
  const [err, setErr] = useState("");
  const noRobot = !s.simulated && !s.robot;

  const load = async () => {
    setScan(true);
    setErr("");
    try {
      setWifi(await api<Hotspots>("/wifi"));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setScan(false);
    }
  };
  useEffect(() => {
    if (noRobot && !wifi) load();
  }, [noRobot]); // eslint-disable-line react-hooks/exhaustive-deps

  const join = async (ssid: string) => {
    setJoining(ssid);
    setErr("");
    try {
      const r = await api<{ lbm: State }>("/wifi/connect", { body: { ssid, password: pw[ssid] ?? "" } });
      onConnected(r.lbm);
      setWifi(null);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setJoining("");
    }
  };

  return (
    <section className="rounded-md border border-line bg-panel p-5">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-[15px] font-semibold">机器人</h2>
        <Button size="sm" variant="ghost" disabled={s.prep.running} onClick={onRecheck}>
          <RefreshCw className={cn(s.prep.running && "animate-spin")} />
          重新检查
        </Button>
      </div>
      <ul className="space-y-2">
        {s.prep.checks.map((c) => (
          <li key={c.name} className="flex gap-2.5 text-sm">
            <span className={cn("mt-0.5 grid size-5 shrink-0 place-items-center rounded-full", c.ok === true ? "bg-pass text-white" : c.ok === false ? "bg-fail text-white" : "bg-line-soft")}>
              {c.ok === true ? <CheckIcon className="size-3.5" /> : c.ok === false ? <X className="size-3.5" /> : <Loader2 className="size-3.5 animate-spin" />}
            </span>
            <span>
              {c.name}
              <span className="block text-xs text-ink-soft">{c.detail}</span>
            </span>
          </li>
        ))}
        {!s.prep.checks.length && <li className="text-sm text-ink-soft">检查中…</li>}
      </ul>
      {s.prep.ok && <p className="mt-3 text-sm text-pass">这台机器人可以测了。换机器人时会自动重新检查、自动传分析脚本。</p>}

      {noRobot && (
        <div className="mt-4 border-t border-line-soft pt-4">
          <div className="mb-2 flex items-center justify-between">
            <div className="text-sm font-medium">连接机器人热点</div>
            <Button size="sm" variant="ghost" disabled={scan} onClick={load}>
              <Wifi />
              {scan ? "搜索中…" : "重新搜索"}
            </Button>
          </div>
          {wifi && !wifi.usb && wifi.device && <p className="mb-2 text-xs text-wait">这台电脑没插 USB 无线网卡，连机器人热点后电脑会断外网。</p>}
          {wifi?.error && <p role="alert" className="text-sm text-fail">{wifi.error}</p>}
          <ul className="max-h-[220px] space-y-1 overflow-y-auto">
            {wifi?.list.map((h) => (
              <li key={h.ssid} className="flex items-center gap-2 rounded-[4px] border border-line-soft px-2.5 py-1.5 text-sm">
                <span className="min-w-0 flex-1 truncate">{h.ssid}</span>
                <span className="num text-xs text-ink-soft">{h.signal}%</span>
                {!h.saved && (
                  <Input type="password" className="h-7 w-[110px] text-xs" placeholder="热点密码" value={pw[h.ssid] ?? ""} onChange={(e) => setPw({ ...pw, [h.ssid]: e.target.value })} />
                )}
                <Button size="sm" variant={h.in_use ? "ghost" : "outline"} disabled={!!joining || (!h.saved && !pw[h.ssid])} onClick={() => join(h.ssid)}>
                  {joining === h.ssid ? <Loader2 className="animate-spin" /> : null}
                  {h.in_use ? "重连" : "连接"}
                </Button>
              </li>
            ))}
            {wifi && !wifi.list.length && <li className="text-sm text-ink-soft">附近没有搜到机器人热点（HU_ 开头），确认机器人已开机。</li>}
          </ul>
          {err && <p role="alert" className="mt-2 text-sm text-fail">{err}</p>}
        </div>
      )}
    </section>
  );
}

function Recording({ live, error, busy, call }: { live: Live; error: string; busy: boolean; call: (p: string) => Promise<boolean> }) {
  const last = useRef({ phase: "", n: 0 });
  const [confirmDrop, setConfirmDrop] = useState(false);
  useEffect(() => {
    if (live.phase !== last.current.phase) {
      if (live.phase === "可以推杆") beep(900, 150);
      if (live.phase === "方向不对" || live.phase === "断流") beep(300, 500);
    }
    if (live.pushes.length > last.current.n) beep(1300, 250);
    if (live.phase === "推杆" && live.pushing_s >= 5 && last.current.phase === "推杆" && live.pushing_s < 5.5) beep(600, 400);
    last.current = { phase: live.phase, n: live.pushes.length };
  });
  const n = live.pushes.length;
  const enough = n >= live.reps;
  const stepNow = live.phase === "准备" ? 2 : enough ? 6 : live.phase === "推杆" ? 3 : live.phase === "等停" ? 4 : 3;
  const tone =
    live.phase === "推杆" ? "bg-live text-white" : live.phase === "可以推杆" ? "bg-pass text-white" : live.phase === "方向不对" || live.phase === "断流" ? "bg-fail text-white" : "bg-line-soft";

  return (
    <div>
      <div className={cn("flex items-center justify-between rounded-md px-5 py-4", enough ? "bg-pass text-white" : tone)} aria-live="assertive">
        <div className="text-[26px] font-semibold leading-tight">{enough ? `做够 ${live.reps} 次了，点“结束并出报告”` : live.text}</div>
        {live.phase === "推杆" && <div className="num text-[34px] leading-none">{live.pushing_s.toFixed(1)}</div>}
      </div>

      <div className="mt-4 flex items-center gap-3">
        <div className="num text-[15px]">
          第 <span className="text-[22px] font-semibold">{n}</span> / {live.reps} 次
        </div>
        <div className="flex flex-wrap gap-1">
          {Array.from({ length: Math.max(live.reps, n) }).map((_, i) => (
            <span key={i} className={cn("num grid h-7 min-w-9 place-items-center rounded-[3px] px-1 text-xs", i < n ? "bg-ink text-panel" : "border border-dashed border-line text-ink-soft")}>
              {i < n ? `${live.pushes[i]}s` : i + 1}
            </span>
          ))}
        </div>
        <div className="num ml-auto text-sm text-ink-soft">已录 {mmss(live.elapsed)}</div>
      </div>
      {live.wrong > 0 && <p className="mt-2 text-sm text-wait">有 {live.wrong} 次推的是别的方向，不计数，报告里也不算。</p>}
      {live.pushes.some((p) => p < 4) && <p className="mt-1 text-sm text-wait">有推杆不到 4 秒，速度比只用推满 4 秒以上的次数算，建议补做。</p>}

      <ol className="mt-5 space-y-1.5 border-t border-line-soft pt-4 text-sm">
        {live.steps.map((t, i) => (
          <li key={t} className={cn("flex gap-2.5", i === stepNow ? "font-medium text-ink" : "text-ink-soft")}>
            <span className={cn("num grid size-5 shrink-0 place-items-center rounded-full text-xs", i === stepNow ? "bg-ink text-panel" : "bg-line-soft")}>{i + 1}</span>
            {t}
          </li>
        ))}
      </ol>

      {error && <p role="alert" className="mt-3 text-sm text-fail">{error}</p>}
      <div className="mt-5 flex items-center gap-2 border-t border-line-soft pt-4">
        <Button variant="primary" size="lg" className="flex-1" disabled={busy} onClick={() => call("/lbm/end")}>
          <Square />
          结束并出报告
        </Button>
        {confirmDrop ? (
          <>
            <span className="text-sm text-fail">录包会删掉</span>
            <Button variant="danger" size="lg" disabled={busy} onClick={() => call("/lbm/discard")}>
              确认放弃
            </Button>
            <Button variant="ghost" size="lg" onClick={() => setConfirmDrop(false)}>
              不放弃
            </Button>
          </>
        ) : (
          <Button variant="ghost" size="lg" disabled={busy} onClick={() => setConfirmDrop(true)}>
            放弃这次
          </Button>
        )}
      </div>
      {!enough && n > 0 && <p className="mt-2 text-xs text-ink-soft">还差 {live.reps - n} 次，也可以现在结束，按已做的出报告。</p>}
    </div>
  );
}

function Finishing({ f, call }: { f: Finish; call: (p: string) => Promise<boolean> }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    await navigator.clipboard.writeText(await api<string>("/lbm/markdown"));
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };
  return (
    <div>
      {f.auto ? (
        <AutoProgress f={f} onStop={() => call("/lbm/auto/stop")} />
      ) : (
      <ul className="space-y-2">
        {f.stages.map((st) => (
          <li key={st.name} className="flex gap-2.5 text-sm">
            <span className={cn("mt-0.5 grid size-5 shrink-0 place-items-center rounded-full", st.state === "ok" ? "bg-pass text-white" : st.state === "fail" ? "bg-fail text-white" : "bg-line-soft")}>
              {st.state === "ok" ? <CheckIcon className="size-3.5" /> : st.state === "fail" ? <X className="size-3.5" /> : st.state === "run" ? <Loader2 className="size-3.5 animate-spin" /> : null}
            </span>
            <span className={cn(st.state === "wait" && "text-ink-soft")}>
              {st.name}
              {st.detail && <span className="block text-xs text-ink-soft">{st.detail}</span>}
            </span>
          </li>
        ))}
      </ul>
      )}
      {f.error && <p role="alert" className="mt-3 text-sm text-fail">{f.error}</p>}

      {f.result && (
        <div className="mt-5 border-t border-line-soft pt-4">
          <div className="text-[15px]">
            找到 <span className="num text-[22px] font-semibold">{f.result.found}</span> 次{f.auto ? "推杆" : f.item}（计划 {f.result.reps} 次）
          </div>
          {f.result.summary.map((r) => {
            const g = (k: string) => r[k] as Stat;
            const turn = r.unit === "°/s";
            return (
            <div key={r.kind} className="mt-3">
            {f.result!.summary.length > 1 && <div className="mb-1 text-sm font-medium">{r.kind}（{r.n} 次）</div>}
            <div className="grid grid-cols-3 gap-3">
              <Big label={`指令 → 内部 → 实际（${r.unit}）`} value={`${num(g("cmd"))} → ${num(g("lbm"))} → ${num(g("actual"))}`} />
              <Big label="实际 / 指令" value={pct(g("actual_over_cmd"))} sub={pctRng(g("actual_over_cmd"))} strong />
              <Big label={turn ? "推杆中朝向变化 °" : "侧向偏移率（左正）"} value={turn ? num(g("yaw_push_deg"), 1) : pct(g("side_rate"))} sub={turn ? rng(g("yaw_push_deg"), 1) : pctRng(g("side_rate"))} />
              <Big label="松手到停下 s" value={num(g("stop_s"))} sub={rng(g("stop_s"))} />
              <Big label={turn ? "松手后多转 °" : "松手后多走 m"} value={num(g("coast_main"), turn ? 1 : 2)} sub={rng(g("coast_main"), turn ? 1 : 2)} />
              <Big label="未停次数" value={String(r.n_not_stopped)} />
            </div>
            </div>
            );
          })}
          <p className="mt-3 break-all text-xs text-ink-soft">报告已存：{f.report}（本场所有记录合在一份里，每次结束自动更新）</p>
        </div>
      )}

      {!f.running && (
        <div className="mt-5 flex gap-2 border-t border-line-soft pt-4">
          {f.result && (
            <>
              <Button variant="outline" onClick={copy}>
                <ClipboardCopy />
                {copied ? "已复制" : "复制报告"}
              </Button>
              <Button variant="outline" onClick={() => api("/lbm/open", { body: {} })}>
                <FolderOpen />
                打开报告文件夹
              </Button>
            </>
          )}
          <Button variant="primary" className="ml-auto" onClick={() => call("/lbm/close")}>
            完成
          </Button>
        </div>
      )}
    </div>
  );
}

function AutoProgress({ f, onStop }: { f: Finish; onStop: () => void }) {
  const names = f.stages.map((st) => st.name);
  const run = f.stages.findIndex((st) => st.state === "run");
  const idx = run >= 0 ? run : f.stages.map((st) => st.state !== "wait").lastIndexOf(true);
  const play = names.indexOf("自动推杆");
  const st = f.stages[idx];
  return (
    <StageProgress
      names={names}
      idx={idx}
      states={f.stages.map((x) => x.state)}
      text={!f.running ? (f.error ? "已结束" : "完成") : st ? st.detail || `${st.name}…` : ""}
      playing={idx === play}
      done={idx > play}
      elapsed={f.elapsed ?? 0}
      total={f.total ?? 0}
      onStop={f.running ? onStop : undefined}
      stopDisabled={idx > play}
    />
  );
}

function Big({ label, value, sub, strong }: { label: string; value: string; sub?: string; strong?: boolean }) {
  return (
    <div className="rounded-[4px] border border-line-soft px-3 py-2">
      <div className="text-xs text-ink-soft">{label}</div>
      <div className={cn("num mt-0.5 text-[17px]", strong && "font-semibold")}>{value}</div>
      {sub && <div className="num text-xs text-ink-soft">{sub}</div>}
    </div>
  );
}

function Results({ s, call }: { s: State; call: (p: string, body?: unknown, method?: string) => Promise<boolean> }) {
  const [path, setPath] = useState("");
  const [copied, setCopied] = useState(false);
  return (
    <section className="mt-8">
      <div className="mb-3 flex items-end justify-between">
        <h2 className="text-[17px] font-semibold">本场结果</h2>
        <Button
          variant="outline"
          size="sm"
          onClick={async () => {
            await navigator.clipboard.writeText(await api<string>("/lbm/markdown"));
            setCopied(true);
            setTimeout(() => setCopied(false), 2000);
          }}
        >
          <ClipboardCopy />
          {copied ? "已复制" : "复制报告"}
        </Button>
      </div>
      <Chain rows={s.summary} />
      <After rows={s.summary} />

      <div className="mt-6 rounded-md border border-line bg-panel">
        <div className="border-b border-line-soft px-5 py-2.5 text-[13px] text-ink-soft">逐次（{s.trials.length}）：推杆中途松手、被人扶的写原因排除</div>
        <table className="w-full border-collapse text-left text-sm">
          <thead>
            <tr className="text-xs text-ink-soft">
              <th className="px-5 py-2 font-normal">时间</th>
              <th className="px-2 py-2 font-normal">测试项</th>
              <th className="px-2 py-2 font-normal">推杆 s</th>
              <th className="px-2 py-2 font-normal">指令 → 内部 → 实际</th>
              <th className="px-2 py-2 font-normal">实际/指令</th>
              <th className="px-2 py-2 font-normal">侧偏 m / 朝向 °</th>
              <th className="px-2 py-2 font-normal">停下 s / 多走</th>
              <th className="px-2 py-2 font-normal" />
            </tr>
          </thead>
          <tbody>
            {[...s.trials].reverse().map((t) => (
              <TrialRow key={t.id} t={t} call={call} />
            ))}
          </tbody>
        </table>
      </div>

      <div className="mt-6 grid grid-cols-2 gap-6">
        <div className="rounded-md border border-line bg-panel p-5">
          <div className="mb-2 flex items-center justify-between">
            <div className="text-[13px] text-ink-soft">录包（{s.takes.length}），默认留在主控 ~/Cyres/lbm/bag/</div>
            <Button size="sm" variant="outline" disabled={!!s.fetching || s.simulated || !s.takes.some((k) => k.bag.startsWith("~/") && !k.local_bag)} onClick={() => call("/lbm/fetch")}>
              <Download />
              {s.fetching ? `拷回中 ${s.fetching}` : "拷回本机"}
            </Button>
          </div>
          <ul className="max-h-[200px] space-y-1 overflow-y-auto text-sm">
            {[...s.takes].reverse().map((k) => (
              <li key={k.id} className="flex gap-3">
                <span className="num w-[64px] shrink-0 text-ink-soft">{clock(k.started)}</span>
                <span className="w-[56px] shrink-0">{k.item}</span>
                <span className="num w-[72px] shrink-0">{k.n_found ?? "—"} / {k.reps || "—"} 次</span>
                <span className={cn("min-w-0 flex-1 truncate", k.status !== "完成" && "text-wait")} title={k.bag || k.note}>
                  {k.status !== "完成" ? k.note || k.status : k.local_bag ? "已拷回" : k.name}
                </span>
              </li>
            ))}
          </ul>
        </div>
        <div className="rounded-md border border-line bg-panel p-5">
          <Field label="分析本机已有录包" hint="填 .bag 路径，录包里每一次推杆都算">
            <Input className="h-8 text-sm" value={path} onChange={(e) => setPath(e.target.value)} placeholder="~/xxx/fwd_1.bag" />
          </Field>
          <Button className="mt-2" size="sm" variant="outline" disabled={!path.trim()} onClick={() => call("/lbm/analyze", { path: path.trim() }).then((ok) => ok && setPath(""))}>
            <FileSearch />
            分析
          </Button>
        </div>
      </div>
    </section>
  );
}

function Chain({ rows }: { rows: Row[] }) {
  return (
    <div className="overflow-hidden rounded-md border border-line bg-panel">
      <div className="border-b border-line-soft px-5 py-2.5 text-[13px] text-ink-soft">链路逐级（推满后的稳态，中位数；小字为范围）</div>
      <table className="w-full border-collapse text-left text-sm">
        <thead>
          <tr className="text-xs text-ink-soft">
            <th className="px-5 py-2 font-normal">测试项</th>
            <th className="px-2 py-2 font-normal">指令</th>
            <th className="px-2 py-2 font-normal">LBMotion 内部</th>
            <th className="px-2 py-2 font-normal">实际</th>
            <th className="px-2 py-2 font-normal">内部/指令</th>
            <th className="px-2 py-2 font-normal">实际/内部</th>
            <th className="px-2 py-2 font-normal">实际/指令</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const g = (k: string) => r[k] as Stat;
            return (
              <tr key={r.kind} className="border-t border-line-soft align-top">
                <td className="px-5 py-2.5">
                  {r.kind}
                  <div className="text-xs text-ink-soft">
                    {r.n_steady}/{r.n} 次，{r.unit}
                  </div>
                </td>
                {["cmd", "lbm", "actual"].map((k) => (
                  <td key={k} className="num px-2 py-2.5">
                    {num(g(k))}
                    <div className="text-xs text-ink-soft">{rng(g(k))}</div>
                  </td>
                ))}
                {["lbm_over_cmd", "actual_over_lbm", "actual_over_cmd"].map((k) => (
                  <td key={k} className={cn("num px-2 py-2.5", k === "actual_over_cmd" && "font-semibold")}>
                    {pct(g(k))}
                    <div className="text-xs font-normal text-ink-soft">{pctRng(g(k))}</div>
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="border-t border-line-soft px-5 py-2 text-xs text-ink-soft">横移在 /lbmotion_states 里没有对应字段，内部一级记“—”。</p>
    </div>
  );
}

function After({ rows }: { rows: Row[] }) {
  return (
    <div className="mt-6 overflow-hidden rounded-md border border-line bg-panel">
      <div className="border-b border-line-soft px-5 py-2.5 text-[13px] text-ink-soft">推杆中的偏移、松手后的滑行（中位数）</div>
      <table className="w-full border-collapse text-left text-sm">
        <thead>
          <tr className="text-xs text-ink-soft">
            <th className="px-5 py-2 font-normal">测试项</th>
            <th className="px-2 py-2 font-normal">推杆行程 m</th>
            <th className="px-2 py-2 font-normal">侧向偏移 m（左正）</th>
            <th className="px-2 py-2 font-normal">偏移率</th>
            <th className="px-2 py-2 font-normal">朝向变化 °</th>
            <th className="px-2 py-2 font-normal">松手到停下 s</th>
            <th className="px-2 py-2 font-normal">多走 m / 多转 °</th>
            <th className="px-2 py-2 font-normal">抬脚</th>
            <th className="px-2 py-2 font-normal">未停</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const g = (k: string) => r[k] as Stat;
            const d = r.unit === "°/s" ? 1 : 2;
            return (
              <tr key={r.kind} className="border-t border-line-soft align-top">
                <td className="px-5 py-2.5">{r.kind}</td>
                {(["travel_m", "side_m"] as const).map((k) => (
                  <td key={k} className="num px-2 py-2.5">
                    {num(g(k))}
                    <div className="text-xs text-ink-soft">{rng(g(k))}</div>
                  </td>
                ))}
                <td className="num px-2 py-2.5">
                  {pct(g("side_rate"))}
                  <div className="text-xs text-ink-soft">{pctRng(g("side_rate"))}</div>
                </td>
                <td className="num px-2 py-2.5">
                  {num(g("yaw_push_deg"), 1)}
                  <div className="text-xs text-ink-soft">{rng(g("yaw_push_deg"), 1)}</div>
                </td>
                <td className="num px-2 py-2.5">
                  {num(g("stop_s"))}
                  <div className="text-xs text-ink-soft">{rng(g("stop_s"))}</div>
                </td>
                <td className="num px-2 py-2.5">
                  {num(g("coast_main"), d)}
                  <div className="text-xs text-ink-soft">{rng(g("coast_main"), d)}</div>
                </td>
                <td className="num px-2 py-2.5">{num(g("coast_steps"), 0)}</td>
                <td className={cn("num px-2 py-2.5", r.n_not_stopped > 0 && "font-semibold text-fail")}>{r.n_not_stopped}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function TrialRow({ t, call }: { t: Trial; call: (p: string, body?: unknown, method?: string) => Promise<boolean> }) {
  const [why, setWhy] = useState("");
  const m = t.main;
  const n = (k: string, d = 2) => (m[k] !== null && m[k] !== undefined ? Number(m[k]).toFixed(d) : "—");
  const turn = m.unit === "°/s";
  return (
    <tr className={cn("border-t border-line-soft align-top", t.excluded && "text-ink-soft line-through decoration-ink-soft/40")}>
      <td className="num px-5 py-2">{clock(t.started)}</td>
      <td className="px-2 py-2">{t.item}</td>
      <td className="num px-2 py-2">{n("push_s")}</td>
      <td className="num px-2 py-2">
        {n("cmd")} → {n("lbm")} → {n("actual")}
        <div className="text-xs text-ink-soft">{m.unit as string}</div>
      </td>
      <td className="num px-2 py-2 font-semibold">{m.actual_over_cmd != null ? `${(Number(m.actual_over_cmd) * 100).toFixed(1)}%` : "—"}</td>
      <td className="num px-2 py-2">{turn ? `— / ${n("yaw_push_deg", 1)}` : `${n("side_m")} / ${n("yaw_push_deg", 1)}`}</td>
      <td className="num px-2 py-2">
        {m.released && m.stop_s === null ? <span className="text-fail">未停</span> : n("stop_s")} / {n("coast_main", turn ? 1 : 2)}
      </td>
      <td className="px-2 py-2">
        <div className="flex items-center justify-end gap-1">
          {t.excluded ? (
            <Button size="sm" variant="ghost" title={t.exclude_note} onClick={() => call("/lbm/exclude", { id: t.id, excluded: false })}>
              恢复
            </Button>
          ) : (
            <>
              <Input className="h-7 w-[110px] text-xs" value={why} onChange={(e) => setWhy(e.target.value)} placeholder="排除原因" />
              <Button size="sm" variant="ghost" disabled={!why.trim()} onClick={() => call("/lbm/exclude", { id: t.id, excluded: true, note: why.trim() })}>
                排除
              </Button>
            </>
          )}
          <button className="rounded p-1 text-ink-soft hover:text-fail" aria-label="删除这次" onClick={() => call(`/lbm/trials/${t.id}`, undefined, "DELETE")}>
            <Trash2 className="size-4" />
          </button>
        </div>
      </td>
    </tr>
  );
}
