import { Fragment, useEffect, useState } from "react";
import { Play, Save, Trash2 } from "lucide-react";
import { PageHead } from "@/App";
import type { RobotInfo } from "@/components/RobotStrip";
import { Verdict } from "@/components/Verdict";
import { Button } from "@/components/ui/button";
import { Check, Input } from "@/components/ui/field";
import { api, clock, cn } from "@/lib";

type Action = { key: string; how: string; watch: string; floor: string };
type Trial = { id: number; created_at: number; action: string; result: string; boundary: string; note: string; sway: string; controller: string };
type Row = {
  controller: string;
  action: string;
  n: number;
  n_完成: number;
  n_饱和: number;
  n_未跟随: number;
  n_摔倒: number;
  completion: number;
  boundaries: string[];
  cpif: string;
  cpif_ratio: string;
  f1: boolean;
  enough: boolean;
  sway_max: number | null;
};
type State = {
  session: string;
  controller: string;
  controllers: string[];
  actions: Action[];
  results: string[];
  simulated: boolean;
  active: string;
  active_since: number;
  stream_on: boolean;
  stream_error: string;
  live_sway: { roll?: { max: number }; pitch?: { max: number } } | null;
  tilt_peak: number;
  rates: [number, number];
  trials: Trial[];
  summary: Row[];
  min_trials: number;
  saved_record_ids: number[];
};

const CHIP: Record<string, string> = {
  完成: "bg-pass",
  饱和: "bg-wait",
  未跟随: "border-2 border-fail bg-panel",
  摔倒: "bg-fail",
};

export default function Boundary({ robot, simulated }: { robot: RobotInfo | null; simulated: boolean }) {
  const [s, setS] = useState<State | null>(null);
  const [text, setText] = useState<Record<string, string>>({});
  const [error, setError] = useState("");

  useEffect(() => {
    const poll = () => api<State>("/boundary").then(setS).catch(() => undefined);
    poll();
    const id = setInterval(poll, 1000);
    return () => clearInterval(id);
  }, [simulated, robot?.sn]);

  const call = async (path: string, body: unknown = {}, method?: string) => {
    setError("");
    try {
      setS(await api<State>(path, method ? { method } : { body }));
      return true;
    } catch (e) {
      setError((e as Error).message);
      return false;
    }
  };

  const record = async (action: string, result: string) => {
    const t = (text[action] ?? "").trim();
    const ok = await call("/boundary/result", { action, result, boundary: result === "饱和" ? t : "", note: result === "饱和" ? "" : t });
    if (ok) setText({ ...text, [action]: "" });
  };

  if (!s) return null;
  const noRobot = !simulated && !robot?.sn;
  const mine = (a: string) => s.trials.filter((t) => t.action === a && t.controller === s.controller);

  return (
    <>
      <PageHead
        title="边界探测（L4）"
        desc="《Oli遥操-脚本v4》全部演示动作，VR 全身动捕下逐项探边界，天车保护。每个动作至少 3 次；饱和可以接受，但要写清边界；平地摔倒记 F1，一票否决。"
      />
      <div className="grid grid-cols-[300px_1fr] gap-6">
        <section className="flex flex-col gap-4 self-start rounded-md border border-line bg-panel p-5">
          <div>
            <div className="mb-1.5 text-[13px] text-ink-soft">控制器</div>
            <div className="flex overflow-hidden rounded-[4px] border border-line" role="radiogroup" aria-label="控制器">
              {s.controllers.map((c) => (
                <button
                  key={c}
                  role="radio"
                  aria-checked={s.controller === c}
                  onClick={() => call("/boundary/controller", { controller: c })}
                  className={cn("flex-1 px-3 py-2 text-sm", s.controller === c ? "bg-ink text-panel" : "hover:bg-line-soft")}
                >
                  {c}
                </button>
              ))}
            </div>
            <p className="mt-1.5 text-xs text-ink-soft">同一天同一台机器两种控制器都测，下面自动出对比表。</p>
          </div>
          <Check checked={s.stream_on} disabled={noRobot} onChange={(v) => call("/boundary/stream", { on: v })}>
            读姿态：每次算胸腔摆幅峰值（先点“开始这次”）
          </Check>
          {s.stream_on && (
            <p className="-mt-2 text-xs text-ink-soft">
              IMU {s.rates[0].toFixed(0)} Hz / 关节 {s.rates[1].toFixed(0)} Hz
              {s.stream_error && <span className="text-fail">，{s.stream_error}</span>}
            </p>
          )}
          <div className="border-t border-line-soft pt-3 text-xs leading-relaxed text-ink-soft">
            <div className="mb-1 text-[13px] text-ink">结果怎么选</div>
            <p>
              <Chip r="完成" /> 完整跟随　<Chip r="饱和" /> 到边界不再跟随（写边界）
            </p>
            <p className="mt-1">
              <Chip r="未跟随" /> 范围内没跟上，算 issue　<Chip r="摔倒" /> F1
            </p>
          </div>
          {error && <p role="alert" className="text-sm text-fail">{error}</p>}
          <Button variant="primary" disabled={s.simulated || !s.trials.length} onClick={() => call("/boundary/save")}>
            <Save />
            {s.saved_record_ids.length ? "更新台账" : "写入台账"}
          </Button>
          <p className="-mt-2 text-xs text-ink-soft">
            每个（控制器，动作）写 CPI-F 和 CPI 各一条，层级 L4。{s.simulated && "演练数据不写台账。"}
            {s.saved_record_ids.length > 0 && ` 已写 ${s.saved_record_ids.length} 条。`}
          </p>
          <p className="text-xs text-ink-soft">本场：{s.session}</p>
        </section>

        <section className="min-w-0">
          <div className="overflow-hidden rounded-md border border-line bg-panel">
            <table className="w-full border-collapse text-left">
              <thead>
                <tr className="border-b border-line text-[13px] text-ink-soft">
                  <th className="px-5 py-2.5 font-normal">动作与边界执行</th>
                  <th className="px-3 py-2.5 font-normal">{s.controller} 已做</th>
                  <th className="px-3 py-2.5 font-normal">记一次</th>
                </tr>
              </thead>
              <tbody>
                {s.actions.map((a) => {
                  const ts = mine(a.key);
                  const active = s.active === a.key;
                  return (
                    <tr key={a.key} className={cn("border-t border-line-soft align-top", active && "bg-live/8")}>
                      <td className="px-5 py-3">
                        <div className="font-medium">{a.key}</div>
                        <div className="mt-0.5 max-w-[34ch] text-xs text-ink-soft">{a.how}</div>
                        <div className="mt-0.5 max-w-[34ch] text-xs text-ink-soft">观测：{a.watch}</div>
                      </td>
                      <td className="px-3 py-3">
                        <div className="flex flex-wrap gap-1">
                          {ts.map((t) => (
                            <span key={t.id} title={`${t.result}${t.boundary ? "：" + t.boundary : ""}${t.sway ? `，摆幅峰值 ${t.sway}°` : ""}`} className={cn("size-4 rounded-[2px]", CHIP[t.result])} />
                          ))}
                          {Array.from({ length: Math.max(0, s.min_trials - ts.length) }).map((_, i) => (
                            <span key={i} className="size-4 rounded-[2px] border border-dashed border-line" />
                          ))}
                        </div>
                        <div className={cn("num mt-1 text-xs", ts.length >= s.min_trials ? "text-ink-soft" : "text-wait")}>
                          {ts.length} / {s.min_trials} 次
                        </div>
                      </td>
                      <td className="px-3 py-3">
                        <div className="flex flex-wrap items-center gap-1.5">
                          {s.stream_on && (
                            <Button size="sm" variant={active ? "primary" : "ghost"} onClick={() => call("/boundary/begin", { action: a.key })}>
                              <Play />
                              {active ? `进行中 ${clock(s.active_since)}` : "开始这次"}
                            </Button>
                          )}
                          {s.results.map((r) => (
                            <Button key={r} size="sm" variant={r === "摔倒" ? "danger" : "outline"} onClick={() => record(a.key, r)}>
                              {r}
                            </Button>
                          ))}
                        </div>
                        <Input
                          className="mt-1.5 h-8 text-sm"
                          value={text[a.key] ?? ""}
                          onChange={(e) => setText({ ...text, [a.key]: e.target.value })}
                          placeholder="饱和时写边界，例如抬膝到胸前一半；其他写备注"
                        />
                        {active && s.live_sway && (
                          <div className="mt-1 text-xs text-ink-soft">
                            这次胸腔摆幅峰值 roll {s.live_sway.roll?.max ?? "—"}° / pitch {s.live_sway.pitch?.max ?? "—"}°，骨盆倾角峰值 {s.tilt_peak}°
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {s.summary.length > 0 && <Matrix rows={s.summary} controllers={s.controllers} />}

          {s.trials.length > 0 && (
            <div className="mt-6 rounded-md border border-line bg-panel">
              <div className="border-b border-line-soft px-5 py-2.5 text-[13px] text-ink-soft">本场逐次记录（{s.trials.length}）</div>
              <ul className="max-h-[320px] overflow-y-auto">
                {[...s.trials].reverse().map((t) => (
                  <li key={t.id} className="flex items-center gap-4 border-t border-line-soft px-5 py-2 text-sm first:border-t-0">
                    <span className="num w-[64px] shrink-0 text-ink-soft">{clock(t.created_at)}</span>
                    <span className="w-[72px] shrink-0">{t.controller}</span>
                    <span className="w-[80px] shrink-0">{t.action}</span>
                    <span className={cn("w-[52px] shrink-0 font-medium", t.result === "完成" ? "text-pass" : t.result === "饱和" ? "text-wait" : "text-fail")}>{t.result}</span>
                    <span className="min-w-0 flex-1 text-ink-soft">
                      {[t.boundary, t.note, t.sway && `摆幅峰值 ${t.sway}°`].filter(Boolean).join("；")}
                    </span>
                    <button className="rounded p-1 text-ink-soft hover:text-fail" aria-label="删除这次" onClick={() => call(`/boundary/trials/${t.id}`, undefined, "DELETE")}>
                      <Trash2 className="size-4" />
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </section>
      </div>
    </>
  );
}

function Chip({ r }: { r: string }) {
  return <span className={cn("mr-1 inline-block size-3 translate-y-px rounded-[2px]", CHIP[r])} />;
}

function Matrix({ rows, controllers }: { rows: Row[]; controllers: string[] }) {
  const actions = [...new Set(rows.map((r) => r.action))];
  const used = controllers.filter((c) => rows.some((r) => r.controller === c));
  const get = (a: string, c: string) => rows.find((r) => r.action === a && r.controller === c);
  return (
    <div className="mt-6 overflow-hidden rounded-md border border-line bg-panel">
      <div className="border-b border-line-soft px-5 py-2.5 text-[13px] text-ink-soft">
        {used.length > 1 ? "双控制器对比（同机同日同任务集）" : "汇总"}：完成度 = （完成 + 饱和）÷ 次数，目标 100% 且不摔
      </div>
      <table className="w-full border-collapse text-left text-sm">
        <thead>
          <tr className="text-xs text-ink-soft">
            <th className="px-5 py-2 font-normal">动作</th>
            {used.map((c) => (
              <th key={c} className="border-l border-line-soft px-3 py-2 font-normal" colSpan={1}>
                {c}：次数、完成度、CPI-F、摆幅峰值、边界
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {actions.map((a) => (
            <tr key={a} className="border-t border-line-soft align-top">
              <td className="px-5 py-2.5">{a}</td>
              {used.map((c) => {
                const r = get(a, c);
                return (
                  <td key={c} className="border-l border-line-soft px-3 py-2.5">
                    {r ? (
                      <Fragment>
                        <div className="flex flex-wrap items-baseline gap-x-3">
                          <span className={cn("num", !r.enough && "text-wait")}>{r.n} 次</span>
                          <span className={cn("num text-lg font-semibold", r.completion < 100 ? "text-fail" : "text-pass")}>{r.completion}%</span>
                          <span className="num">
                            CPI-F {r.cpif}
                            <span className="text-ink-soft">{r.cpif_ratio}</span>
                          </span>
                          {r.sway_max !== null && <span className="num">{r.sway_max}°</span>}
                          {r.f1 && <Verdict status="f1" text="F1 一票否决" />}
                          {!r.enough && <span className="text-xs text-wait">不足 3 次</span>}
                        </div>
                        {r.boundaries.length > 0 && <div className="mt-0.5 text-xs text-ink-soft">边界：{r.boundaries.join("；")}</div>}
                      </Fragment>
                    ) : (
                      <span className="text-ink-soft">—</span>
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
