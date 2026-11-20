import { useEffect, useRef, useState } from "react";
import { Camera, CheckCircle2, FolderOpen, Loader2, Play, Trash2, X } from "lucide-react";
import { PageHead } from "@/App";
import type { RobotInfo } from "@/components/RobotStrip";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { api, cn } from "@/lib";
import { ask } from "@/components/Confirm";

type Step = { force: number; set_ok: boolean; readback: Record<string, number | null>; match: boolean; photos: string[]; note: string; tau: number[] | null; finished: boolean };
type Test = { name: string; side: string; sides: string[]; default: number; plan: number[]; step: number; steps: Step[]; done: boolean; aborted?: boolean; restored?: Record<string, number | null>; time?: string };
type State = { robot: string; simulated: boolean; busy: string; error: string; test: Test | null; runs: Test[]; report_path: string };

const SIDE_CN: Record<string, string> = { left: "左", right: "右" };
const label = (t: Test, k: number) => (k === 0 ? `默认（${t.plan[0]}）` : String(t.plan[k]));

export default function Gripper({ robot, simulated }: { robot: RobotInfo | null; simulated: boolean }) {
  const [s, setS] = useState<State | null>(null);
  const [side, setSide] = useState("两侧");
  const [err, setErr] = useState("");

  useEffect(() => {
    const poll = () => api<State>("/gripper").then(setS).catch(() => undefined);
    poll();
    const id = setInterval(poll, 600);
    return () => clearInterval(id);
  }, [simulated, robot?.sn]);

  const call = async (p: string, body: unknown = {}, method?: string) => {
    setErr("");
    try {
      setS(await api<State>(p, method ? { method } : { body }));
    } catch (e) {
      setErr((e as Error).message);
    }
  };

  if (!s) return null;
  const t = s.test;
  const ready = simulated || !!robot?.sn;

  return (
    <>
      <PageHead title="夹爪夹持力" desc="软件依次把夹持力设成 默认、0、100，每一档你用特殊塑料杯夹一次、拍照，点“已完成”才切下一档。测完自动恢复默认值并出报告。">
        {s.report_path && (
          <Button variant="outline" onClick={() => api("/gripper/open", { body: {} })}>
            <FolderOpen />
            打开报告文件夹
          </Button>
        )}
      </PageHead>

      <section className="rounded-md border border-line bg-panel p-5">
        <h2 className="mb-3 text-[15px] font-semibold">开始前</h2>
        <ol className="mb-4 list-decimal space-y-1 pl-5 text-sm">
          <li>机器人装 LimX 二指夹爪，挂天车，遥控器在手边。</li>
          <li>Pico 进入 VR部分动捕并按住握把跟随，扳机能控制夹爪开合。</li>
          <li>准备好特殊塑料杯和手机（拍照传到电脑，或直接用电脑摄像头）。</li>
        </ol>
        <div className="flex items-end gap-3">
          <div>
            <div className="mb-1 text-xs text-ink-soft">测哪个夹爪</div>
            <div className="flex gap-1.5" role="radiogroup" aria-label="夹爪">
              {["左", "右", "两侧"].map((k) => (
                <button key={k} role="radio" aria-checked={side === k} onClick={() => setSide(k)} className={cn("h-11 w-16 rounded-[4px] border text-[15px]", side === k ? "border-ink bg-ink text-panel" : "border-line hover:bg-line-soft")}>
                  {k}
                </button>
              ))}
            </div>
          </div>
          <Button variant="primary" size="lg" className="h-11 flex-1 text-base" disabled={!ready || !!s.busy || (!!t && !t.done)} onClick={() => call("/gripper/start", { side })}>
            {s.busy === "读默认值" ? <Loader2 className="animate-spin" /> : <Play />}
            开始：读默认值，再依次测 默认 / 0 / 100
          </Button>
        </div>
        {!ready && <p className="mt-2 text-sm text-wait">先连上机器人热点（顶部显示 SN 后才能开始）。</p>}
        {(err || s.error) && !(t && !t.done) && <p role="alert" className="mt-2 text-sm text-fail">{err || s.error}</p>}
      </section>

      {s.runs.length > 0 && <Runs runs={[...s.runs].reverse()} />}

      <Dialog open={!!t && !t.done}>
        {t && !t.done && (
          <DialogContent title={`夹持力测试：${t.side}夹爪`} className="w-[min(720px,94vw)]" closable={false} autoFocus={false}>
            <StepView t={t} busy={s.busy} error={err || s.error} call={call} />
          </DialogContent>
        )}
      </Dialog>
    </>
  );
}

function StepView({ t, busy, error, call }: { t: Test; busy: string; error: string; call: (p: string, b?: unknown, m?: string) => Promise<void> }) {
  const st = t.steps[t.steps.length - 1];
  const [note, setNote] = useState("");
  const [uploading, setUploading] = useState(false);
  const file = useRef<HTMLInputElement>(null);
  useEffect(() => setNote(""), [t.step]);

  const upload = async (files: FileList | null) => {
    if (!files) return;
    setUploading(true);
    for (const f of Array.from(files)) {
      const data = await new Promise<string>((res) => {
        const r = new FileReader();
        r.onload = () => res(String(r.result));
        r.readAsDataURL(f);
      });
      await call("/gripper/photo", { filename: f.name, data });
    }
    setUploading(false);
    if (file.current) file.current.value = "";
  };

  if (!st) return <p className="text-center text-ink-soft">正在读默认值…</p>;
  const last = t.step === t.plan.length - 1;
  return (
    <div className="space-y-5">
      <ol className="flex gap-1">
        {t.plan.map((_, k) => (
          <li key={k} className={cn("flex-1 rounded-[3px] py-1.5 text-center text-sm", k < t.step ? "bg-pass text-white" : k === t.step ? "bg-ink text-panel" : "bg-line-soft text-ink-soft")}>
            {label(t, k)}
          </li>
        ))}
      </ol>
      <div className="rounded-[4px] bg-line-soft px-4 py-3 text-sm">
        夹持力已设为 <b className="num text-base">{st.force}</b>，回读{" "}
        {Object.entries(st.readback).map(([k, v]) => (
          <span key={k} className="num mr-2">
            {SIDE_CN[k]} {v ?? "—"}
          </span>
        ))}
        {st.match ? <span className="text-pass">一致</span> : <span className="text-fail">不一致</span>}
      </div>
      <ol className="list-decimal space-y-1.5 pl-5 text-[15px]">
        <li>把特殊塑料杯放到{t.side === "两侧" ? "左、右夹爪（一个一个来）" : `${t.side}夹爪`}中间。</li>
        <li>扳机按到底夹住，保持 3 秒，看杯子的形变。</li>
        <li>夹着的时候拍照，上传到下面（至少一张）。</li>
        <li>松开扳机，取下杯子，点“已完成”。</li>
      </ol>
      <div>
        <input ref={file} type="file" accept="image/*" multiple capture="environment" className="hidden" onChange={(e) => upload(e.target.files)} />
        <div className="flex flex-wrap gap-2">
          {st.photos.map((p) => (
            <div key={p} className="relative">
              <img src={`/api/gripper/img/${encodeURIComponent(p)}`} alt={p} className="h-28 w-28 rounded-[4px] border border-line object-cover" />
              <button className="absolute right-1 top-1 rounded-full bg-ink/70 p-0.5 text-white" aria-label="删掉这张" onClick={() => call(`/gripper/photo/${encodeURIComponent(p)}`, undefined, "DELETE")}>
                <X className="size-3.5" />
              </button>
            </div>
          ))}
          <button onClick={() => file.current?.click()} disabled={uploading} className="grid h-28 w-28 place-items-center rounded-[4px] border border-dashed border-line text-sm text-ink-soft hover:border-ink">
            {uploading ? <Loader2 className="animate-spin" /> : (
              <span className="flex flex-col items-center gap-1">
                <Camera className="size-6" />
                上传照片
              </span>
            )}
          </button>
        </div>
      </div>
      <textarea className="h-16 w-full rounded-[4px] border border-line bg-panel p-2 text-sm" placeholder="形变情况（选填），例如：杯壁凹进约 5 mm，没破" value={note} onChange={(e) => setNote(e.target.value)} />
      {error && <p role="alert" className="text-sm text-fail">{error}</p>}
      <div className="flex gap-2">
        <Button variant="danger" disabled={!!busy} onClick={async () => (await ask("放弃这一轮？软件会把夹持力恢复成默认值。", { ok: "放弃", danger: true })) && call("/gripper/abort")}>
          <Trash2 />
          放弃这一轮
        </Button>
        <Button variant="primary" size="lg" className="h-12 flex-1 text-base" disabled={!!busy || uploading || !st.photos.length} onClick={() => call("/gripper/finish", { note })}>
          {busy ? <Loader2 className="animate-spin" /> : <CheckCircle2 />}
          {busy ? `${busy}…` : !st.photos.length ? "先上传照片" : last ? "已完成，恢复默认值并出报告" : `已完成，切到 ${t.plan[t.step + 1]}`}
        </Button>
      </div>
    </div>
  );
}

function Runs({ runs }: { runs: Test[] }) {
  return (
    <section className="mt-8 space-y-6">
      <h2 className="text-[17px] font-semibold">结果</h2>
      {runs.map((r) => (
        <article key={r.name} className="rounded-md border border-line bg-panel p-5">
          <h3 className="mb-3 text-[15px] font-semibold">
            {r.time} {r.side}夹爪
            <span className="ml-2 text-sm font-normal text-ink-soft">
              默认 {r.default}，测完恢复回读 {r.restored ? Object.values(r.restored).join(" / ") : "—"}
              {r.aborted ? " · 中途放弃" : ""}
            </span>
          </h3>
          <table className="w-full text-sm">
            <thead className="text-ink-soft">
              <tr>
                {["档位", "回读", "夹爪指令 tau", "照片", "形变"].map((h) => (
                  <th key={h} className="py-1 text-left font-normal">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {r.steps.map((st, k) => (
                <tr key={k} className="border-t border-line-soft align-top">
                  <td className="num py-2">{label(r, k)}</td>
                  <td className={cn("num py-2", !st.match && "text-fail")}>{Object.values(st.readback).map((v) => v ?? "—").join(" / ")}</td>
                  <td className="num py-2">{st.tau ? st.tau.join(" / ") : "没读到"}</td>
                  <td className="py-2">
                    <div className="flex gap-1">
                      {st.photos.map((p) => (
                        <a key={p} href={`/api/gripper/img/${encodeURIComponent(p)}`} target="_blank" rel="noreferrer">
                          <img src={`/api/gripper/img/${encodeURIComponent(p)}`} alt={p} className="h-20 w-20 rounded-[3px] border border-line object-cover" />
                        </a>
                      ))}
                    </div>
                  </td>
                  <td className="py-2">{st.note || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </article>
      ))}
    </section>
  );
}
