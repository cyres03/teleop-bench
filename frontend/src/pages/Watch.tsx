import { useEffect, useState } from "react";
import { Flag, Hand, Pause, Play, Square, Zap } from "lucide-react";
import { PageHead } from "@/App";
import type { RobotInfo } from "@/components/RobotStrip";
import { Readout } from "@/components/Readout";
import { Verdict } from "@/components/Verdict";
import { Button } from "@/components/ui/button";
import { Check, Field, Input } from "@/components/ui/field";
import { api, clock, cn, duration } from "@/lib";

type Ev = { id: number; t: number; kind: string; detail: string; verdict: string };
type Session = {
  scene: string;
  simulated: boolean;
  state: "idle" | "running" | "paused" | "finished";
  elapsed_s: number;
  robot_status: string;
  last_tilt: [number, number] | null;
  imu_rate: number;
  use_imu: boolean;
  threshold_deg: number;
  events: Ev[];
  falls: number;
  issues: number;
  pending: number;
  hpf: { display: string; ratio: string };
  hpi: { display: string; ratio: string };
  saved_record_ids: number[];
  joint_rate: number;
  flat: boolean;
  f1: boolean;
  stream_error: string;
  sway: Sway;
  ts: { n: number; stable: number; timeout: number; median: number | null; max: number | null };
  ts_pending: boolean;
  ts_results: { t: number; ts: number | null; ok: boolean }[];
  saved_measure_ids: number[];
};

type Axis = { median: number; max: number; over: number; max_all: number };
type Sway = { n: number; n_total: number; basis: string; pass: boolean | null; target?: number; roll?: Axis; pitch?: Axis };

const SCENES = ["持续站立稳定性", "VR部分动捕持续跟随", "VR全身动捕持续跟随", "LBMotion 持续行走", "长时连续遥操"];
const VERDICT_TONE: Record<string, string> = { 摔倒: "text-fail", issue: "text-wait", 忽略: "text-ink-soft", 待确认: "text-live-deep" };

export default function Watch({ robot, simulated }: { robot: RobotInfo | null; simulated: boolean }) {
  const [s, setS] = useState<Session | null>(null);
  const [scene, setScene] = useState(SCENES[0]);
  const [useImu, setUseImu] = useState(true);
  const [flat, setFlat] = useState(true);
  const [threshold, setThreshold] = useState(60);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    const poll = () => api<Session>("/watch").then(setS).catch(() => undefined);
    poll();
    const id = setInterval(poll, 1000);
    return () => clearInterval(id);
  }, []);

  const call = async (path: string, body: unknown = {}) => {
    setError("");
    try {
      setS(await api<Session>(path, { body }));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const active = s?.state === "running" || s?.state === "paused";

  return (
    <>
      <PageHead
        title="长测看护"
        desc="持续站立、长时遥操这类按时长考核的场景。软件计时，盯机器人状态和姿态，异常先记为待确认，人确认后才计入 HPF / HPI。"
      />
      <div className="grid grid-cols-[340px_1fr] gap-6">
        <section className="flex flex-col gap-5 rounded-md border border-line bg-panel p-5">
          <Field label="场景" hint="同一个场景名会在台账里归到一起">
            <Input list="watch-scenes" value={scene} disabled={active} onChange={(e) => setScene(e.target.value)} />
            <datalist id="watch-scenes">
              {SCENES.map((x) => (
                <option key={x} value={x} />
              ))}
            </datalist>
          </Field>
          <Check checked={useImu} onChange={setUseImu} disabled={active}>
            SSH 读姿态和关节（IMU + /joint/state）：检测倾倒、算胸腔摆幅和稳定时间 Ts
          </Check>
          <Check checked={flat} onChange={setFlat} disabled={active}>
            平地无障碍：有摔倒记 F1，一票否决
          </Check>
          <Field label="倾倒阈值 °" hint="|roll| 或 |pitch| 超过它并持续 0.2 s 记一次疑似摔倒，默认同主控 FallDetector">
            <Input type="number" min={20} max={89} value={threshold} disabled={active} onChange={(e) => setThreshold(Number(e.target.value))} />
          </Field>

          {error && <p role="alert" className="text-sm text-fail">{error}</p>}

          {!active ? (
            <Button
              variant="primary"
              size="lg"
              disabled={!simulated && !robot?.sn}
              onClick={() => call("/watch/start", { scene, use_imu: useImu, threshold_deg: threshold, flat })}
            >
              <Play />
              开始计时
            </Button>
          ) : (
            <div className="flex flex-col gap-2">
              {s?.state === "running" ? (
                <Button variant="outline" onClick={() => call("/watch/pause")}>
                  <Pause />
                  暂停计时（换电池、调整场地）
                </Button>
              ) : (
                <Button variant="outline" onClick={() => call("/watch/resume")}>
                  <Play />
                  继续计时
                </Button>
              )}
              <Button
                variant="outline"
                disabled={s?.state !== "running" || s.ts_pending || !(s.joint_rate || s.simulated)}
                onClick={() => call("/watch/release")}
                title="操作员松开指令的同时点；腿部 12 个关节角速度都 <0.3 rad/s 并持续 1 s 即判稳定"
              >
                <Hand />
                {s?.ts_pending ? "等腿部静止…" : "松指令（测稳定时间 Ts）"}
              </Button>
              <div className="grid grid-cols-2 gap-2">
                <Button variant="outline" onClick={() => call("/watch/event", { detail: "现场记录", verdict: "issue" })}>
                  <Flag />
                  记一次 issue
                </Button>
                <Button variant="danger" onClick={() => call("/watch/event", { detail: "现场记录", verdict: "摔倒" })}>
                  记一次摔倒
                </Button>
              </div>
              {s?.simulated && (
                <Button variant="ghost" onClick={() => call("/watch/inject")}>
                  <Zap />
                  演练：注入一次倾倒
                </Button>
              )}
              <Field label="结束备注" className="mt-2">
                <Input value={note} onChange={(e) => setNote(e.target.value)} placeholder="场地、负载、操作员" />
              </Field>
              <Button variant="primary" disabled={!!s?.pending} onClick={() => call("/watch/finish", { note })}>
                <Square />
                结束并写入台账
              </Button>
              {!!s?.pending && <p className="text-xs text-live-deep">还有 {s.pending} 条待确认事件，确认后才能结束</p>}
            </div>
          )}
        </section>

        <section className="flex min-w-0 flex-col gap-5">
          {!s || s.state === "idle" ? (
            <div className="rounded-md border border-dashed border-line bg-panel px-8 py-14 text-ink-soft">
              选好场景后开始计时。HPF = 累计小时 ÷ 摔倒次数，HPI = 累计小时 ÷ issue 次数；0 次时写成“小时数+”。
            </div>
          ) : (
            <>
              <div className="rounded-md border border-line bg-panel p-5">
                <div className="flex items-baseline justify-between">
                  <div className="text-[15px] font-semibold">
                    {s.scene}
                    {s.simulated && <span className="ml-2 text-sm font-normal text-sim">演练</span>}
                  </div>
                  <span className={cn("text-sm", s.state === "running" ? "text-pass" : "text-ink-soft")}>
                    {{ running: "计时中", paused: "已暂停", finished: "已结束", idle: "" }[s.state]}
                  </span>
                </div>
                <div className="num mt-3 text-[72px] font-semibold leading-none">{duration(s.elapsed_s)}</div>
                <div className="mt-5 flex flex-wrap gap-x-12 gap-y-3">
                  <div>
                    <Readout metric="HPF" display={s.hpf.display} ratio={s.hpf.ratio} size="sm" tone={s.falls ? "fail" : "ink"} />
                    {s.f1 && (
                      <div className="mt-1">
                        <Verdict status="f1" text="平地摔倒 F1，一票否决" />
                      </div>
                    )}
                  </div>
                  <Readout metric="HPI" display={s.hpi.display} ratio={s.hpi.ratio} size="sm" tone={s.issues ? "fail" : "ink"} />
                </div>
                <div className="mt-5 grid grid-cols-3 gap-4 border-t border-line-soft pt-4 text-sm">
                  <div>
                    <div className="text-xs text-ink-soft">机器人状态</div>
                    <div>{s.robot_status || "—"}</div>
                  </div>
                  <div>
                    <div className="text-xs text-ink-soft">姿态 roll / pitch</div>
                    <div className="num text-base">{s.last_tilt ? `${s.last_tilt[0]}° / ${s.last_tilt[1]}°` : s.use_imu || s.simulated ? "等待数据" : "未开启"}</div>
                  </div>
                  <div>
                    <div className="text-xs text-ink-soft">采样 IMU / 关节</div>
                    <div className="num text-base">
                      {s.imu_rate ? `${s.imu_rate.toFixed(0)} Hz` : "—"} / {s.joint_rate ? `${s.joint_rate.toFixed(0)} Hz` : "—"}
                    </div>
                  </div>
                </div>
                {s.stream_error && <p role="alert" className="mt-2 text-sm text-fail">{s.stream_error}</p>}
                {(s.use_imu || s.simulated) && <SwayTs s={s} />}
                {s.state === "finished" && s.saved_record_ids.length > 0 && (
                  <p className="mt-4 text-sm">
                    已写入台账 {s.saved_record_ids.length} 条（HPF、HPI 各一条）
                    {s.saved_measure_ids.length > 0 && `，测量值 ${s.saved_measure_ids.length} 条（摆幅、Ts）`}。
                  </p>
                )}
              </div>

              <div className="rounded-md border border-line bg-panel">
                <div className="border-b border-line-soft px-5 py-2.5 text-[13px] text-ink-soft">事件（{s.events.length}）</div>
                {!s.events.length ? (
                  <p className="px-5 py-5 text-sm text-ink-soft">还没有事件。</p>
                ) : (
                  <ul>
                    {[...s.events].reverse().map((e) => (
                      <li key={e.id} className="flex items-center gap-4 border-t border-line-soft px-5 py-3 first:border-t-0">
                        <span className="num w-[64px] shrink-0 text-ink-soft">{clock(e.t)}</span>
                        <span className="w-[72px] shrink-0 text-sm">{e.kind}</span>
                        <span className="min-w-0 flex-1 text-sm text-ink-soft">{e.detail}</span>
                        {e.verdict === "待确认" && s.state !== "finished" ? (
                          <span className="flex shrink-0 gap-1.5">
                            <Button size="sm" variant="danger" onClick={() => call("/watch/judge", { event_id: e.id, verdict: "摔倒" })}>
                              算摔倒
                            </Button>
                            <Button size="sm" variant="outline" onClick={() => call("/watch/judge", { event_id: e.id, verdict: "issue" })}>
                              算 issue
                            </Button>
                            <Button size="sm" variant="ghost" onClick={() => call("/watch/judge", { event_id: e.id, verdict: "忽略" })}>
                              忽略
                            </Button>
                          </span>
                        ) : (
                          <span className={cn("shrink-0 text-sm font-medium", VERDICT_TONE[e.verdict])}>{e.verdict}</span>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </>
          )}
        </section>
      </div>
    </>
  );
}

function SwayTs({ s }: { s: Session }) {
  const sw = s.sway;
  const ax = (a?: Axis) =>
    a ? (
      <>
        <span className={cn("num text-[30px] font-semibold leading-none", a.median > (sw.target ?? 4) ? "text-fail" : "text-ink")}>{a.median}°</span>
        <span className="num ml-2 text-ink-soft">最大 {a.max}°</span>
        {a.over > 0 && <span className="ml-2 text-xs text-fail">超 {sw.target}° 的窗口 {a.over} 个</span>}
      </>
    ) : (
      <span className="text-ink-soft">—</span>
    );
  return (
    <div className="mt-4 grid grid-cols-[1fr_1fr_0.8fr] gap-4 border-t border-line-soft pt-4">
      <div>
        <div className="text-xs text-ink-soft">胸腔摆幅 roll（2 s 峰峰值中位）</div>
        <div className="mt-1">{ax(sw.roll)}</div>
      </div>
      <div>
        <div className="text-xs text-ink-soft">胸腔摆幅 pitch（2 s 峰峰值中位）</div>
        <div className="mt-1">{ax(sw.pitch)}</div>
      </div>
      <div>
        <div className="text-xs text-ink-soft">稳定时间 Ts（中位 / 最大）</div>
        <div className="mt-1">
          {s.ts.median !== null ? (
            <>
              <span className="num text-[30px] font-semibold leading-none">{s.ts.median} s</span>
              <span className="num ml-2 text-ink-soft">{s.ts.max} s</span>
            </>
          ) : (
            <span className="text-ink-soft">{s.ts_pending ? "等腿部静止…" : "点“松指令”开始测"}</span>
          )}
        </div>
        {s.ts.n > 0 && (
          <div className="mt-1 text-xs text-ink-soft">
            {s.ts.stable} 次稳定{s.ts.timeout > 0 && <span className="text-fail">，{s.ts.timeout} 次 10 s 内没稳定</span>}
          </div>
        )}
      </div>
      <p className="col-span-3 text-xs text-ink-soft">
        目标 ≤{sw.target ?? 4}°，按{sw.basis || "运动中窗口"}判定（{sw.n}/{sw.n_total} 个窗口）。胸腔 = 骨盆 IMU + 腰部三个关节换算；暂停和疑似摔倒的那段不计。
        {sw.pass !== null && (
          <span className="ml-1.5">
            <Verdict status={sw.pass ? "pass" : "fail"} text={sw.pass ? "摆幅达标" : "摆幅超标"} />
          </span>
        )}
      </p>
    </div>
  );
}
