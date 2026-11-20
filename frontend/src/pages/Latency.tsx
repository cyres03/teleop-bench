import { useEffect, useState } from "react";
import { Camera, MonitorPlay, Play, Trash2 } from "lucide-react";
import { LatencyBoard } from "@/components/LatencyBoard";
import { RobotVideo } from "@/components/RobotVideo";
import { LatencyReport } from "@/components/LatencyReport";
import { PageHead } from "@/App";
import { Button } from "@/components/ui/button";
import { Field, Input, Select } from "@/components/ui/field";
import { api, cn, day } from "@/lib";
import { ask } from "@/components/Confirm";

type Cam = { dev: string; name: string; usb: string };
type Result = { ok: boolean; mean?: number; ci95?: number; median?: number; p5?: number; p95?: number; pairs?: number; A?: { flips: number; fps: number }; B?: { flips: number; fps: number }; warning?: string; raw?: string };
type Job = { kind: string; name: string; phase: string; seconds: number; started_at: number; result: Result; error: string };
type RunRow = { id: number; created_at: number; kind: string; name: string; link: string; robot: string; version: string; calib_ms: number | null; result: Result; note: string };
type State = { job: Job; runs: RunRow[]; calib_ms: number | null };

const LINKS = ["近场", "远程 阿里云", "远程 腾讯云"];
const CODECS = ["H264", "H265", "JPEG"];

function sameHub(a?: Cam, b?: Cam) {
  if (!a || !b) return false;
  const parent = (u: string) => u.split(".").slice(0, -1).join(".");
  return parent(a.usb) !== "" && parent(a.usb) === parent(b.usb);
}

function nextName(n: string) {
  const m = n.match(/^(.*?)(\d+)$/);
  return m ? `${m[1]}${Number(m[2]) + 1}` : `${n}2`;
}

function Step({ n, title, children }: { n: number; title: string; children: React.ReactNode }) {
  return (
    <section className="grid grid-cols-[36px_1fr] gap-4 border-t border-line py-5 first:border-t-0 first:pt-0">
      <div className="num flex size-9 items-center justify-center rounded-full border border-ink text-lg font-semibold">{n}</div>
      <div>
        <h2 className="mb-3 text-[16px] font-semibold">{title}</h2>
        {children}
      </div>
    </section>
  );
}

export default function Latency() {
  const [cams, setCams] = useState<Cam[]>([]);
  const [a, setA] = useState("");
  const [b, setB] = useState("");
  const [shots, setShots] = useState<Record<string, string>>({});
  const [shotErr, setShotErr] = useState<Record<string, string>>({});
  const [st, setSt] = useState<State | null>(null);
  const [name, setName] = useState("tx1");
  const [link, setLink] = useState(LINKS[0]);
  const [codec, setCodec] = useState(CODECS[0]);
  const [seconds, setSeconds] = useState(30);
  const [error, setError] = useState("");
  const [skipCheck, setSkipCheck] = useState(false);

  const loadCams = () =>
    api<Cam[]>("/latency/cameras").then((c) => {
      setCams(c);
      const usb = c.filter((x) => x.name.toLowerCase().includes("usb camera"));
      setA((cur) => (c.some((x) => x.dev === cur) ? cur : usb[0]?.dev ?? ""));
      setB((cur) => (c.some((x) => x.dev === cur) ? cur : usb[1]?.dev ?? ""));
    });

  useEffect(() => {
    loadCams();
    const poll = () => api<State>("/latency").then(setSt).catch(() => undefined);
    poll();
    const id = setInterval(poll, 1000);
    return () => clearInterval(id);
  }, []);

  const shoot = async (dev: string) => {
    setShotErr((e) => ({ ...e, [dev]: "" }));
    const res = await fetch(`/api/latency/snapshot?dev=${encodeURIComponent(dev)}`);
    if (!res.ok) {
      const msg = (await res.json().catch(() => ({ detail: "截图失败" }))).detail;
      setShotErr((e) => ({ ...e, [dev]: msg }));
      return;
    }
    const url = URL.createObjectURL(await res.blob());
    setShots((s) => ({ ...s, [dev]: url }));
  };

  const start = async (kind: "calib" | "measure") => {
    setError("");
    try {
      await api("/latency/start", { body: { kind, name: kind === "calib" ? `calib_${Date.now() % 100000}` : name, seconds, cam_a: a, cam_b: b, link: kind === "calib" ? "" : `${link} ${codec}`, skip_robot_check: skipCheck } });
      if (kind === "measure") setName(nextName(name));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const job = st?.job;
  const busy = job?.phase === "recording" || job?.phase === "analyzing";
  const today = new Date().toDateString();
  const hasMeasures = !!st?.runs.some((r) => r.kind === "measure" && new Date(r.created_at * 1000).toDateString() === today);
  const camA = cams.find((c) => c.dev === a);
  const camB = cams.find((c) => c.dev === b);

  return (
    <>
      <PageHead title="端到端视频延迟" desc="屏幕闪烁块加两台相机：A 拍屏幕，B 拍 Pico 镜片，逐次翻转配对算延迟，结果以均值和 P5–P95 为准。" />
      <LatencyBoard
        runs={st?.runs ?? []}
        onPick={(l, c) => {
          setLink(l);
          setCodec(c);
        }}
      />
      <div className="mt-6 rounded-md border border-line bg-panel p-6">
        <Step n={1} title="打开闪烁页">
          <p className="mb-3 max-w-[70ch] text-ink-soft">在外接屏全屏打开，左右两半随机黑白翻转。机器人头部相机对准它。按 Alt+F4 退出。</p>
          <Button variant="outline" onClick={() => api("/latency/flash", { body: { monitor: 1 } })}>
            <MonitorPlay />
            在外接屏打开闪烁页
          </Button>
        </Step>

        <Step n={2} title="选相机并看一眼画面">
          <div className="grid grid-cols-2 gap-5">
            {[
              { label: "相机 A：拍屏幕", value: a, set: setA, cam: camA },
              { label: "相机 B：拍 Pico 镜片", value: b, set: setB, cam: camB },
            ].map((c) => (
              <div key={c.label} className="flex flex-col gap-2">
                <Field label={c.label}>
                  <Select value={c.value} onChange={(e) => c.set(e.target.value)} disabled={busy}>
                    <option value="">选择相机</option>
                    {cams.map((x) => (
                      <option key={x.dev} value={x.dev}>
                        {x.dev}（{x.name}，USB {x.usb}）
                      </option>
                    ))}
                  </Select>
                </Field>
                <div className="flex aspect-[32/9] items-center justify-center overflow-hidden rounded-[4px] border border-line-soft bg-paper">
                  {c.value && shots[c.value] ? <img src={shots[c.value]} alt={`${c.value} 画面`} className="h-full w-full object-cover" /> : <span className="text-xs text-ink-soft">还没截图</span>}
                </div>
                {c.value && shotErr[c.value] && <p role="alert" className="text-xs text-fail">{shotErr[c.value]}</p>}
                <Button variant="ghost" size="sm" className="self-start" disabled={!c.value || busy} onClick={() => shoot(c.value)}>
                  <Camera />
                  截一张
                </Button>
              </div>
            ))}
          </div>
          <Button variant="ghost" size="sm" className="mt-3" disabled={busy} onClick={loadCams}>
            刷新相机列表
          </Button>
          {sameHub(camA, camB) && <p className="mt-3 text-sm text-wait">两台相机接在同一个 USB HUB 上，同时打开可能报带宽不够。把其中一台直插电脑。</p>}
        </Step>

        <Step n={3} title="校准">
          <p className="mb-3 max-w-[70ch] text-ink-soft">两台相机都对着屏幕录 30 s，得到两台相机自身的时间差，正式测时自动扣掉。换了相机或 USB 口要重新校准。</p>
          <div className="flex items-center gap-5">
            <Button variant="outline" disabled={busy || !a || !b} onClick={() => start("calib")}>
              <Play />
              录 {seconds} s 校准
            </Button>
            <span className="text-sm">
              当前校准值{" "}
              <span className="num text-xl font-semibold">{st?.calib_ms != null ? `${st.calib_ms.toFixed(1)} ms` : "无"}</span>
            </span>
          </div>
        </Step>

        <Step n={4} title="测量">
          <p className="mb-3 max-w-[70ch] text-ink-soft">B 放回 Pico 镜片前，Pico 进入遥操、画面稳定后开始。链路和编码要和机器人上实际的配置一致，每格至少测 2 次。</p>
          <RobotVideo
            link={link}
            codec={codec}
            disabled={busy}
            onAdopt={(l, c) => {
              setLink(l);
              setCodec(c);
            }}
          />
          <div className="grid max-w-[820px] grid-cols-[1fr_1.2fr_100px_100px_auto] items-end gap-3">
            <Field label="名称">
              <Input value={name} onChange={(e) => setName(e.target.value)} disabled={busy} />
            </Field>
            <Field label="链路">
              <Select value={link} onChange={(e) => setLink(e.target.value)} disabled={busy}>
                {LINKS.map((l) => (
                  <option key={l}>{l}</option>
                ))}
              </Select>
            </Field>
            <Field label="编码">
              <Select value={codec} onChange={(e) => setCodec(e.target.value)} disabled={busy}>
                {CODECS.map((c) => (
                  <option key={c}>{c}</option>
                ))}
              </Select>
            </Field>
            <Field label="录像 s">
              <Input type="number" min={10} max={120} value={seconds} onChange={(e) => setSeconds(Number(e.target.value))} disabled={busy} />
            </Field>
            <Button variant="primary" disabled={busy || !a || !b || st?.calib_ms == null} onClick={() => start("measure")}>
              <Play />
              开始测量
            </Button>
          </div>
          <label className="mt-2 flex items-center gap-2 text-xs text-ink-soft">
            <input type="checkbox" checked={skipCheck} onChange={(e) => setSkipCheck(e.target.checked)} disabled={busy} />
            跳过机器人配置核对（只在读不到配置时用，这次结果会标“配置未核实”）
          </label>
          {error && <p role="alert" className="mt-3 text-sm text-fail">{error}</p>}
          {job && job.phase !== "idle" && (
            <div className={cn("mt-4 rounded-[4px] border px-4 py-3 text-sm", job.phase === "failed" ? "border-fail" : "border-line-soft")}>
              {job.phase === "recording" && `正在录像：${job.name}，${job.seconds} s`}
              {job.phase === "analyzing" && `正在分析：${job.name}`}
              {job.phase === "failed" && (
                <>
                  <span className="text-fail">{job.error}</span>
                  {job.result?.raw && (
                    <details className="mt-2">
                      <summary className="cursor-pointer text-xs text-ink-soft">原始输出</summary>
                      <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-paper p-2 text-xs">{job.result.raw}</pre>
                    </details>
                  )}
                </>
              )}
              {job.phase === "done" && job.result.ok && (
                <span>
                  {job.name}：均值 <span className="num text-xl font-semibold">{job.result.mean} ms</span>，P5–P95 {job.result.p5}–{job.result.p95} ms，配上 {job.result.pairs} 次翻转
                  {job.result.warning && <span className="ml-2 text-wait">{job.result.warning}</span>}
                </span>
              )}
            </div>
          )}
        </Step>
      </div>

      <h2 className="mb-3 mt-8 text-[16px] font-semibold">历次结果</h2>
      {!st?.runs.length ? (
        <p className="text-ink-soft">还没有结果。</p>
      ) : (
        <div className="overflow-hidden rounded-md border border-line bg-panel">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-line text-[13px] text-ink-soft">
                {["日期", "名称", "链路与编码", "均值 ms", "P5–P95 ms", "配对 / 翻转", "校准 ms", ""].map((h) => (
                  <th key={h} className="px-4 py-2.5 font-normal">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {st.runs.map((r) => (
                <tr key={r.id} className="border-t border-line-soft">
                  <td className="px-4 py-2.5">{day(r.created_at)}</td>
                  <td className="px-4 py-2.5">{r.kind === "calib" ? "校准" : r.name}</td>
                  <td className="px-4 py-2.5">{r.link || "—"}</td>
                  <td className="num px-4 py-2.5 text-lg font-semibold">{r.result.mean}</td>
                  <td className="num px-4 py-2.5 text-base">
                    {r.result.p5}–{r.result.p95}
                  </td>
                  <td className="num px-4 py-2.5 text-base">
                    {r.result.pairs} / {Math.min(r.result.A?.flips ?? 0, r.result.B?.flips ?? 0)}
                  </td>
                  <td className="num px-4 py-2.5 text-base">{r.calib_ms ?? "—"}</td>
                  <td className="px-2 py-2.5">
                    <button
                      className="rounded p-1 text-ink-soft hover:text-fail"
                      aria-label="删除这次结果"
                      onClick={async () => {
                        if (!(await ask(`删除“${r.kind === "calib" ? "校准" : r.name}”这次结果？无效数据删掉，不进对比。`, { ok: "删除", danger: true }))) return;
                        await api(`/latency/runs/${r.id}`, { method: "DELETE" });
                      }}
                    >
                      <Trash2 className="size-4" />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <LatencyReport hasMeasures={hasMeasures} busy={busy} />
    </>
  );
}
