import { useEffect, useState } from "react";
import { Play, Square, TriangleAlert } from "lucide-react";
import { PageHead } from "@/App";
import type { RobotInfo } from "@/components/RobotStrip";
import { Readout } from "@/components/Readout";
import { TrialStrip } from "@/components/TrialStrip";
import { Verdict } from "@/components/Verdict";
import { Button } from "@/components/ui/button";
import { Check, Field, Input } from "@/components/ui/field";
import { api, clock, cn } from "@/lib";
import { ask } from "@/components/Confirm";

type Seq = { key: string; label: string; hanging_ok: boolean; settle_s: number; steps: { key: string; label: string; scene: string }[] };
type StepSummary = { step: string; label: string; scene: string; total: number; fails: number; display: string; ratio: string; outcomes: string; median_ms: number | null; max_ms: number | null };
type Run = {
  sequence: string;
  cycles: number;
  running: boolean;
  stop_requested: boolean;
  simulated: boolean;
  current: string;
  end_reason: string;
  done_steps: number;
  total_steps: number;
  summary: StepSummary[];
  log: { t: number; text: string; level: string }[];
  saved_record_ids: number[];
  blocks: { range: string; cycles: number; steps: number; fails: number; falls: number; median_ms: number | null; max_ms: number | null; ok: boolean }[];
  block_size: number;
  falls: { t: number; cycle: number; at: string; detail: string }[];
  cycles_done: number;
  f1: boolean;
};

export default function Stress({ robot, simulated }: { robot: RobotInfo | null; simulated: boolean }) {
  const [seqs, setSeqs] = useState<Seq[]>([]);
  const [seq, setSeq] = useState("engine");
  const [cycles, setCycles] = useState(20);
  const [settle, setSettle] = useState("");
  const [hanging, setHanging] = useState(true);
  const [site, setSite] = useState(false);
  const [cont, setCont] = useState(false);
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api<Seq[]>("/stress/sequences").then(setSeqs);
    const poll = () => api<Run>("/stress").then(setRun).catch(() => undefined);
    poll();
    const id = setInterval(poll, 700);
    return () => clearInterval(id);
  }, []);

  const current = seqs.find((s) => s.key === seq);
  const blockedByHanging = !!current && hanging && !current.hanging_ok;
  const noRobot = !simulated && !robot?.sn;
  const canStart = !run?.running && !blockedByHanging && !noRobot && (simulated || site) && cycles >= 1;

  const start = async () => {
    setError("");
    try {
      const r = await api<Run>("/stress/start", {
        body: { sequence: seq, cycles, settle_s: settle === "" ? null : Number(settle), hanging, site_confirmed: site, continue_on_fail: cont },
      });
      setRun(r);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const showRun = run && run.sequence;

  return (
    <>
      <PageHead
        title="模式切换压测"
        desc="按序列循环下发切换指令，每一步等机器人返回成功再走下一步，统计每种切换的 CPI 和耗时。真机结果会写进台账。"
      />
      <div className="grid grid-cols-[340px_1fr] gap-6">
        <section className="flex flex-col gap-5 rounded-md border border-line bg-panel p-5">
          <fieldset className="flex flex-col gap-2" disabled={run?.running}>
            <legend className="mb-2 text-[13px] text-ink-soft">切换序列</legend>
            {seqs.map((s) => {
              const off = hanging && !s.hanging_ok;
              return (
                <label
                  key={s.key}
                  className={cn(
                    "flex cursor-pointer flex-col rounded-[4px] border px-3 py-2.5",
                    seq === s.key ? "border-ink bg-paper/70" : "border-line-soft hover:border-line",
                    off && "opacity-50",
                  )}
                >
                  <span className="flex items-center gap-2">
                    <input type="radio" name="seq" className="accent-[var(--color-ink)]" checked={seq === s.key} onChange={() => setSeq(s.key)} />
                    <span className="font-medium">{s.label}</span>
                  </span>
                  <span className="ml-6 mt-0.5 text-xs text-ink-soft">
                    {s.steps.map((st) => st.label).join("，然后")}；每步后等 {s.settle_s} s
                    {off && "；吊装时不能跑"}
                  </span>
                </label>
              );
            })}
          </fieldset>

          <div className="grid grid-cols-2 gap-3">
            <Field label="循环次数" hint="每 20 次出一段统计，建议 20 的倍数">
              <Input type="number" min={1} max={500} value={cycles} disabled={run?.running} onChange={(e) => setCycles(Number(e.target.value))} />
            </Field>
            <Field label="每步后等待 s" hint={current ? `默认 ${current.settle_s}` : ""}>
              <Input type="number" min={0} value={settle} placeholder={current ? String(current.settle_s) : ""} disabled={run?.running} onChange={(e) => setSettle(e.target.value)} />
            </Field>
          </div>

          <div className="flex flex-col gap-2.5">
            <Check checked={hanging} onChange={setHanging} disabled={run?.running}>
              吊装保护中（机器人挂在天车上，禁止坐下、躺下类序列）
            </Check>
            <Check checked={cont} onChange={setCont} disabled={run?.running}>
              失败后继续（默认失败一次就停，不再下发）
            </Check>
            {!simulated && (
              <Check checked={site} onChange={setSite} disabled={run?.running}>
                已确认现场：天车跟随、急停可达、遥控器在旁、周围无人
              </Check>
            )}
          </div>

          {noRobot && <p className="text-sm text-fail">没有识别到机器人。连上机器人热点后点顶部“重新识别”，或打开演练模式。</p>}
          {error && <p role="alert" className="text-sm text-fail">{error}</p>}

          {run?.running ? (
            <>
              <Button variant="danger" size="lg" onClick={() => api<Run>("/stress/stop", { body: {} }).then(setRun)} disabled={run.stop_requested}>
                <Square />
                {run.stop_requested ? "当前一步完成后停止" : "停止后续动作"}
              </Button>
              <Button
                variant="outline"
                className="border-fail text-fail"
                onClick={async () => {
                  if (await ask("现场看到摔倒了？记一次摔倒并停止压测（平地摔倒记 F1）。", { ok: "记摔倒并停止", danger: true })) api<Run>("/stress/fall", { body: { detail: "" } }).then(setRun);
                }}
              >
                <TriangleAlert />
                记一次摔倒并停止
              </Button>
            </>
          ) : (
            <Button variant={simulated ? "primary" : "live"} size="lg" onClick={start} disabled={!canStart}>
              <Play />
              {simulated ? "开始演练" : "开始压测"}
            </Button>
          )}
          <p className="-mt-2 text-xs text-ink-soft">停止只停后续调度，不会打断机器人正在执行的动作。紧急情况用遥控器或急停。</p>
        </section>

        <section className="flex min-w-0 flex-col gap-5">
          {!showRun ? (
            <div className="rounded-md border border-dashed border-line bg-panel px-8 py-14 text-ink-soft">
              选好序列和次数后开始。每一次切换会在这里画成一格：青绿是成功，红色是失败。
            </div>
          ) : (
            <>
              <div className="rounded-md border border-line bg-panel p-5">
                <div className="mb-4 flex items-baseline justify-between">
                  <div className="text-[15px] font-semibold">
                    {seqs.find((s) => s.key === run.sequence)?.label}
                    {run.simulated && <span className="ml-2 text-sm font-normal text-sim">演练</span>}
                  </div>
                  <div className="num text-lg text-ink-soft">
                    {run.done_steps} / {run.total_steps} 步
                  </div>
                </div>
                <div className="flex flex-col gap-5">
                  {run.summary.map((s) => (
                    <div key={s.step} className="grid grid-cols-[minmax(220px,auto)_1fr] items-center gap-6">
                      <div>
                        <div className="mb-1 text-sm">{s.scene}</div>
                        <Readout metric="CPI" display={s.display} ratio={s.ratio} tone={s.fails ? "fail" : "pass"} />
                      </div>
                      <div>
                        <TrialStrip outcomes={s.outcomes} pending={run.running ? Math.max(0, run.cycles - s.total) : 0} />
                        <div className="mt-2 text-xs text-ink-soft">
                          耗时中位 <span className="num text-sm text-ink">{s.median_ms ?? "—"}</span> ms，最长{" "}
                          <span className="num text-sm text-ink">{s.max_ms ?? "—"}</span> ms
                        </div>
                      </div>
                    </div>
                  ))}
                  {!run.summary.length && <p className="text-ink-soft">第一步执行中…</p>}
                </div>
                {run.blocks.length > 0 && <Blocks run={run} />}
                {!run.running && run.end_reason && (
                  <p className="mt-5 border-t border-line-soft pt-3 text-sm">
                    结束：{run.end_reason}
                    {run.saved_record_ids.length > 0 && `，已写入台账 ${run.saved_record_ids.length} 条`}
                    {run.f1 && (
                      <span className="ml-2">
                        <Verdict status="f1" text="F1 一票否决" />
                      </span>
                    )}
                  </p>
                )}
              </div>

              <div className="rounded-md border border-line bg-panel">
                <div className="border-b border-line-soft px-5 py-2.5 text-[13px] text-ink-soft">过程记录</div>
                <ol className="max-h-[300px] overflow-y-auto px-5 py-2 text-[13px]">
                  {[...run.log].reverse().map((l, i) => (
                    <li key={i} className={cn("flex gap-3 py-1", l.level === "error" && "text-fail", l.level === "warn" && "text-wait")}>
                      <span className="num shrink-0 text-ink-soft">{clock(l.t)}</span>
                      <span>{l.text}</span>
                    </li>
                  ))}
                </ol>
              </div>
            </>
          )}
        </section>
      </div>
    </>
  );
}

function Blocks({ run }: { run: Run }) {
  return (
    <div className="mt-5 border-t border-line-soft pt-3">
      <div className="mb-1.5 text-[13px] text-ink-soft">每 {run.block_size} 次一段：判据“平滑落地、无摔倒失控”，看耗时是否随次数变长</div>
      <table className="w-full text-left text-sm">
        <thead>
          <tr className="text-xs text-ink-soft">
            <th className="py-1 font-normal">循环</th>
            <th className="py-1 font-normal">失败</th>
            <th className="py-1 font-normal">摔倒</th>
            <th className="py-1 font-normal">耗时中位 / 最长 ms</th>
            <th className="py-1 font-normal">本段</th>
          </tr>
        </thead>
        <tbody>
          {run.blocks.map((b) => (
            <tr key={b.range} className="border-t border-line-soft">
              <td className="num py-1.5">{b.range}</td>
              <td className={cn("num py-1.5", b.fails && "text-fail")}>{b.fails}</td>
              <td className={cn("num py-1.5", b.falls && "font-semibold text-fail")}>{b.falls}</td>
              <td className="num py-1.5">
                {b.median_ms ?? "—"} / {b.max_ms ?? "—"}
              </td>
              <td className="py-1.5">{b.ok ? <span className="text-pass">通过</span> : <span className="text-fail">不通过</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
