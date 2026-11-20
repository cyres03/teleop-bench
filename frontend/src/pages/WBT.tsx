import { useEffect, useRef, useState } from "react";
import { FileSearch, FolderOpen, Loader2, Play, Trash2 } from "lucide-react";
import { PageHead } from "@/App";
import type { RobotInfo } from "@/components/RobotStrip";
import { choose } from "@/components/Confirm";
import { StageProgress } from "@/components/StageProgress";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Input } from "@/components/ui/field";
import { RobotCard } from "@/pages/LBMotion";
import { api, cn } from "@/lib";

type Item = { key: string; what: string; space: string; steps: string[]; seconds: number };
type Check = { name: string; ok: boolean | null; detail: string };
type Issue = { 环节: string; 项: string; 前级: string; 后级: string; 差距: string; 严重度: number; 时刻s: number | null };
type Disp = { 前进: number; 侧移: number };
type Turn = { 先转: number; 回转: number };
type Result = {
  follow_s: number;
  控制器?: string;
  "人→重定向": { 位移: { 人: Disp; 重定向: Disp }; 偏航: { 人净: number; 重定向净: number; 人转动?: Turn; 重定向转动?: Turn } };
  "重定向→机器人": { 位移?: { 机器人: Disp }; 偏航?: { 机器人净: number; 机器人转动?: Turn }; 关节?: { 关节: string; 均方根deg: number; 滞后s: number }[] };
  差距清单: Issue[];
};
type Run = { id: number; item: string; name: string; time: string; aborted: boolean; ask?: boolean; excluded?: boolean; note: string; result: Result };
type Live = { active: boolean; item: string; stage: string; text: string; elapsed: number; total: number };
type Hist = {
  item: string;
  version: string;
  robot: string;
  date: string;
  主方向: string;
  重定向: number | null;
  机器人: number | null;
  单位: string;
  完成率: number | null;
  偏航净差: number | null;
  关节误差中位: number | null;
  关节误差最大: number | null;
  差距数: number;
};
type State = {
  session: string;
  baseline: string;
  history: Hist[];
  simulated: boolean;
  robot: string;
  items: Item[];
  prep: { running: boolean; ok: boolean; checks: Check[] };
  run: Live | null;
  error: string;
  runs: Run[];
  report_path: string;
};

const STAGES = ["检查", "录包", "回放", "停止录包", "拷回", "分析", "完成"];

export default function WBT({ robot, simulated }: { robot: RobotInfo | null; simulated: boolean }) {
  const [s, setS] = useState<State | null>(null);
  const [item, setItem] = useState("前进");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [path, setPath] = useState("");

  useEffect(() => {
    const poll = () => api<State>("/wbt").then(setS).catch(() => undefined);
    poll();
    const id = setInterval(poll, 500);
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
  const pending = s && !s.run?.active ? s.runs.find((r) => r.ask && !asked.current.has(r.id)) : undefined;
  useEffect(() => {
    if (!pending) return;
    asked.current.add(pending.id);
    choose(`#${pending.id} ${pending.item} 回放中途停止了。\n保存：计入报告。\n不保存：不进报告，录包和原始记录照样留着。`, {
      title: "中途停止的这次要保存吗",
      yes: "保存",
      no: "不保存",
    }).then((v) => v !== null && call("/wbt/keep", { id: pending.id, keep: v }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pending?.id]);

  if (!s) return null;
  const sel = s.items.find((i) => i.key === item) ?? s.items[0];
  const running = !!s.run?.active;
  const count = (k: string) => s.runs.filter((r) => r.item === k).length;

  return (
    <>
      <PageHead title="全身遥操回放" desc="不用人遥操。软件把固定的基线动作包播给机器人，算出 人 → 重定向 → 机器人 每一级差多少，和以前的版本对比，生成给研发的报告。连好机器人，选一个包，点开始。">
        {s.report_path && (
          <Button variant="outline" onClick={() => api("/wbt/open", { body: {} })}>
            <FolderOpen />
            打开报告文件夹
          </Button>
        )}
      </PageHead>

      <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1.25fr)] gap-6">
        <RobotCard s={s} onRecheck={() => call("/wbt/prepare")} onConnected={() => call("/wbt/prepare")} />

        <section className="rounded-md border border-line bg-panel p-5">
          <h2 className="text-[15px] font-semibold">选基线包</h2>
          <p className="mb-3 text-xs text-ink-soft">{s.baseline}。每个版本都播这几个包。</p>
          <div className="grid grid-cols-3 gap-1.5" role="radiogroup" aria-label="基线包">
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
                  <span className={cn("num text-xs font-normal", item === i.key ? "text-panel/70" : "text-ink-soft")}>已做 {count(i.key)} 次</span>
                </div>
                <div className={cn("mt-0.5 text-xs", item === i.key ? "text-panel/80" : "text-ink-soft")}>
                  {i.what} · 播 {i.seconds.toFixed(0)} 秒
                </div>
              </button>
            ))}
          </div>
          <ol className="mt-4 list-decimal space-y-1 pl-5 text-sm">
            {sel.steps.map((t) => (
              <li key={t}>{t}</li>
            ))}
          </ol>
          <Button variant="primary" size="lg" className="mt-4 h-12 w-full text-base" disabled={!s.prep.ok || busy || running} onClick={() => call("/wbt/begin", { item })}>
            <Play />
            开始：{item}（约 {Math.ceil(sel.seconds + 10)} 秒）
          </Button>
          {!s.prep.ok && <p className="mt-2 text-sm text-wait">左边检查清单全部打勾后才能开始。</p>}
          {(error || s.error) && !running && <p role="alert" className="mt-2 text-sm text-fail">{error || s.error}</p>}
          <details className="mt-4 text-sm">
            <summary className="cursor-pointer text-ink-soft">分析已有录包（不连机器人）</summary>
            <div className="mt-2 flex gap-2">
              <Input className="h-9 flex-1 text-sm" placeholder="录包完整路径，例如 /home/limx/xxx.bag" value={path} onChange={(e) => setPath(e.target.value)} />
              <Button variant="outline" disabled={!path || busy} onClick={() => call("/wbt/analyze", { path })}>
                {busy ? <Loader2 className="animate-spin" /> : <FileSearch />}
                分析
              </Button>
            </div>
            <p className="mt-1 text-xs text-ink-soft">要求：Pico 全身动捕录的包，含 /tf、/teleop/wb_cmd、/teleop/state、/ImuData、/joint/state。</p>
          </details>
        </section>
      </div>

      {s.history.length > 0 && <History rows={s.history} />}
      {s.runs.length > 0 && <Results s={s} call={call} />}

      <Dialog open={running}>
        {running && s.run && (
          <DialogContent title={`回放：${s.run.item}`} className="w-[min(640px,94vw)]" closable={false} autoFocus={false}>
            <Progress run={s.run} busy={busy} onStop={() => call("/wbt/stop")} />
          </DialogContent>
        )}
      </Dialog>
    </>
  );
}

function Progress({ run, busy, onStop }: { run: Live; busy: boolean; onStop: () => void }) {
  const idx = STAGES.indexOf(run.stage);
  return (
    <StageProgress
      names={STAGES.slice(0, -1)}
      idx={idx}
      text={run.text}
      playing={run.stage === "回放"}
      done={idx > STAGES.indexOf("回放")}
      elapsed={run.elapsed}
      total={run.total}
      onStop={onStop}
      stopDisabled={busy || !["检查", "录包", "回放"].includes(run.stage)}
    />
  );
}

function History({ rows }: { rows: Hist[] }) {
  const f = (v: number | null, d = 2) => (v === null ? "—" : v.toFixed(d));
  return (
    <section className="mt-8">
      <h2 className="mb-1 text-[17px] font-semibold">版本对比</h2>
      <p className="mb-3 text-sm text-ink-soft">所有真机测试里播同一批基线包的结果，每个动作、版本、机器人取最新一次。完成率 = 机器人 ÷ 重定向（主方向；转身看右转最深的角度），越接近 1 越好；关节误差越小越好。</p>
      <div className="overflow-x-auto rounded-md border border-line bg-panel">
        <table className="w-full text-sm">
          <thead className="text-ink-soft">
            <tr className="border-b border-line">
              {["动作", "版本", "机器人", "日期", "主方向", "重定向", "机器人", "完成率", "偏航净差 °", "关节误差 中位 / 最大 °", "差距条数"].map((h, k) => (
                <th key={k} className={cn("px-3 py-2 font-normal", k < 5 ? "text-left" : "text-right")}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="num">
            {rows.map((h, k) => (
              <tr key={k} className="border-t border-line-soft">
                <td className="px-3 py-1.5">{h.item}</td>
                <td className="max-w-[260px] truncate px-3 py-1.5" title={h.version}>
                  {h.version}
                </td>
                <td className="px-3 py-1.5">{h.robot}</td>
                <td className="px-3 py-1.5">{h.date}</td>
                <td className="px-3 py-1.5">{h.主方向}</td>
                <td className="px-3 py-1.5 text-right">
                  {f(h.重定向)} {h.单位}
                </td>
                <td className="px-3 py-1.5 text-right">
                  {f(h.机器人)} {h.单位}
                </td>
                <td className="px-3 py-1.5 text-right">{f(h.完成率)}</td>
                <td className="px-3 py-1.5 text-right">{f(h.偏航净差, 1)}</td>
                <td className="px-3 py-1.5 text-right">
                  {f(h.关节误差中位, 1)} / {f(h.关节误差最大, 1)}
                </td>
                <td className="px-3 py-1.5 text-right">{h.差距数}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Results({ s, call }: { s: State; call: (p: string, body?: unknown, method?: string) => Promise<boolean> }) {
  const runs = [...s.runs].reverse();
  return (
    <section className="mt-8">
      <h2 className="mb-1 text-[17px] font-semibold">结果</h2>
      <p className="mb-4 text-sm text-ink-soft">
        每次一张卡片，最新的在最上面。“差距”按严重度排序：<b>重定向→机器人</b> 是全身控制器没做到，<b>人→重定向</b> 是重定向没算对。完整报告点右上角“打开报告文件夹”。
      </p>
      <div className="space-y-6">
        {runs.map((r) => (
          <RunCard key={r.id} r={r} onDelete={() => call(`/wbt/runs/${r.id}`, undefined, "DELETE")} onKeep={(keep) => call("/wbt/keep", { id: r.id, keep })} />
        ))}
      </div>
    </section>
  );
}

function RunCard({ r, onDelete, onKeep }: { r: Run; onDelete: () => void; onKeep: (keep: boolean) => void }) {
  const x = r.result;
  const a = x["人→重定向"];
  const b = x["重定向→机器人"];
  const f = (v: number | undefined) => (v === undefined ? "—" : v.toFixed(2));
  const tf = (t: Turn | undefined) => (t ? `${t.先转.toFixed(0)} / ${t.回转.toFixed(0)}` : "—");
  const top = x.差距清单.slice(0, 6);
  return (
    <article className="rounded-md border border-line bg-panel p-5">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-[15px] font-semibold">
          #{r.id} {r.item}
          <span className="ml-2 text-sm font-normal text-ink-soft">
            {r.time} · {x.控制器 || "控制器未知"} · 跟随 {x.follow_s} s{r.aborted ? (r.excluded ? " · 中途停止，不计入报告" : " · 中途停止") : ""}
            {r.note ? ` · ${r.note}` : ""}
          </span>
        </h3>
        <div className="flex gap-1">
          {r.aborted && (
            <Button size="sm" variant="ghost" onClick={() => onKeep(!!r.excluded)}>
              {r.excluded ? "改为计入报告" : "改为不计入报告"}
            </Button>
          )}
          <Button size="sm" variant="ghost" onClick={onDelete}>
            <Trash2 />
            删除
          </Button>
        </div>
      </div>
      <table className="mb-4 w-full text-sm">
        <thead className="text-ink-soft">
          <tr>
            <th className="text-left font-normal" />
            <th className="text-right font-normal">人</th>
            <th className="text-right font-normal">重定向</th>
            <th className="text-right font-normal">机器人</th>
          </tr>
        </thead>
        <tbody className="num">
          <tr>
            <td>前进位移 m</td>
            <td className="text-right">{f(a.位移.人.前进)}</td>
            <td className="text-right">{f(a.位移.重定向.前进)}</td>
            <td className="text-right">{f(b.位移?.机器人.前进)}</td>
          </tr>
          <tr>
            <td>侧移位移 m（左 +）</td>
            <td className="text-right">{f(a.位移.人.侧移)}</td>
            <td className="text-right">{f(a.位移.重定向.侧移)}</td>
            <td className="text-right">{f(b.位移?.机器人.侧移)}</td>
          </tr>
          <tr>
            <td>转角 °（左转 +）</td>
            <td className="text-right">{a.偏航.人净.toFixed(1)}</td>
            <td className="text-right">{a.偏航.重定向净.toFixed(1)}</td>
            <td className="text-right">{b.偏航 ? b.偏航.机器人净.toFixed(1) : "—"}</td>
          </tr>
          {a.偏航.人转动 && Math.abs(a.偏航.人转动.先转) >= 90 && (
            <tr>
              <td>先转 / 回转 °</td>
              <td className="text-right">{tf(a.偏航.人转动)}</td>
              <td className="text-right">{tf(a.偏航.重定向转动)}</td>
              <td className="text-right">{tf(b.偏航?.机器人转动)}</td>
            </tr>
          )}
        </tbody>
      </table>
      <h4 className="mb-1 text-sm font-medium">差距最大的 {top.length} 项（共 {x.差距清单.length} 项）</h4>
      <table className="mb-4 w-full text-sm">
        <tbody>
          {top.map((i, k) => (
            <tr key={k} className="border-t border-line-soft">
              <td className={cn("py-1 pr-2 text-xs", i.环节 === "重定向→机器人" ? "text-fail" : "text-wait")}>{i.环节}</td>
              <td className="py-1 pr-2">{i.项}</td>
              <td className="py-1 pr-2 text-ink-soft">{[i.前级, i.后级].filter(Boolean).join(" → ")}</td>
              <td className="py-1 pr-2">{i.差距}</td>
              <td className="num py-1 text-right text-ink-soft">{i.时刻s !== null ? `${i.时刻s.toFixed(1)} s` : ""}</td>
            </tr>
          ))}
          {!top.length && (
            <tr>
              <td className="text-pass">没有超过门限的差距</td>
            </tr>
          )}
        </tbody>
      </table>
      <img src={`/api/wbt/img/${r.id}?k=${encodeURIComponent(r.name)}`} alt={`${r.item} 对比图`} className="w-full rounded-[4px] border border-line-soft" loading="lazy" />
    </article>
  );
}
