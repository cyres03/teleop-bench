import { useEffect, useMemo, useState } from "react";
import { ClipboardCopy, Plus, Trash2 } from "lucide-react";
import { PageHead } from "@/App";
import { Readout } from "@/components/Readout";
import { TrialStrip } from "@/components/TrialStrip";
import { Cadence, Verdict, toneOf } from "@/components/Verdict";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent } from "@/components/ui/dialog";
import { Field, Input, Select } from "@/components/ui/field";
import { api, cn, day } from "@/lib";
import { ask } from "@/components/Confirm";

export type LedgerRecord = {
  id: number;
  created_at: number;
  scene: string;
  issue_type: string;
  metric: string;
  total: number;
  events: number;
  target: number | null;
  version: string;
  robot: string;
  source: string;
  note: string;
  evidence: string;
  outcomes: string;
  display: string;
  ratio: string;
  small_sample: boolean;
  meets_target: boolean | null;
  layer: string;
  flat: number;
  f1: boolean;
  status: string;
  status_text: string;
  prev_version: string;
  prev_display: string;
  is_baseline: boolean;
  version_display: string;
};

type Measure = {
  id: number;
  created_at: number;
  scene: string;
  metric: string;
  unit: string;
  value: number;
  max: number | null;
  n: number;
  target: number | null;
  layer: string;
  version: string;
  robot: string;
  source: string;
  note: string;
  status: string;
  status_text: string;
  prev_version: string;
  prev_value: number | null;
};

const LAYERS: Record<string, string> = { L1: "分段精度", L2: "端到端跟随", L3: "稳定与可靠", L4: "功能边界", L5: "自动化回归" };

const METRIC_HELP: Record<string, string> = {
  CPI: "总次数 ÷ issue 次数",
  "CPI-F": "总次数 ÷ 摔倒次数",
  HPI: "累计小时 ÷ issue 次数",
  HPF: "累计小时 ÷ 摔倒次数",
};

export default function Ledger() {
  const [records, setRecords] = useState<LedgerRecord[]>([]);
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");
  const [measures, setMeasures] = useState<Measure[]>([]);

  const load = () => {
    api<LedgerRecord[]>("/records").then(setRecords).catch((e) => setError(e.message));
    api<Measure[]>("/measures").then(setMeasures).catch(() => undefined);
  };
  useEffect(() => {
    load();
  }, []);

  const groups = useMemo(() => {
    const m = new Map<string, LedgerRecord[]>();
    for (const r of records) m.set(r.scene, [...(m.get(r.scene) ?? []), r]);
    return [...m.entries()];
  }, [records]);

  const copy = async () => {
    const text = await api<string>("/records/markdown");
    await navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 1800);
  };

  const removeMeasure = async (m: Measure) => {
    if (!(await ask(`删除“${m.scene} ${m.metric}”这条测量？`, { ok: "删除", danger: true }))) return;
    await api(`/measures/${m.id}`, { method: "DELETE" });
    load();
  };

  const remove = async (r: LedgerRecord) => {
    if (!(await ask(`删除“${r.scene}（${r.issue_type}）”这条记录？`, { ok: "删除", danger: true }))) return;
    await api(`/records/${r.id}`, { method: "DELETE" });
    load();
  };

  return (
    <>
      <PageHead title="指标台账" desc="按功能场景记录次数（或小时）和问题次数，自动算 CPI / HPI；同一项按固件版本比较，第一版是基线，比上一版差就标回退。平地摔倒记 F1，一票否决。">
        <Button variant="outline" onClick={copy} disabled={!records.length}>
          <ClipboardCopy />
          {copied ? "已复制" : "复制为飞书文本"}
        </Button>
        <Dialog open={open} onOpenChange={setOpen}>
          <Button variant="primary" onClick={() => setOpen(true)}>
            <Plus />
            新增记录
          </Button>
          <DialogContent title="新增一条记录">
            <RecordForm
              onSaved={() => {
                setOpen(false);
                load();
              }}
            />
          </DialogContent>
        </Dialog>
      </PageHead>

      {error && <p role="alert" className="mb-4 text-fail">{error}</p>}

      <Cadence />

      {!records.length ? (
        <div className="rounded-md border border-dashed border-line bg-panel px-8 py-14">
          <p className="text-[17px] font-medium">台账还是空的</p>
          <p className="mt-2 max-w-[62ch] text-ink-soft">
            跑一次模式切换压测或长测看护，结果会自动写进来；之前手工测过的，也可以点“新增记录”补上，比如“Move 稳定性，CPI-F，136 次，0 次摔倒”。
          </p>
        </div>
      ) : (
        <div className="overflow-hidden rounded-md border border-line bg-panel">
          <table className="w-full border-collapse text-left">
            <thead>
              <tr className="border-b border-line text-[13px] text-ink-soft">
                <th className="w-[22%] px-5 py-2.5 font-normal">问题类型</th>
                <th className="px-3 py-2.5 font-normal">指标</th>
                <th className="px-3 py-2.5 font-normal">每次结果</th>
                <th className="px-3 py-2.5 font-normal">版本与机器</th>
                <th className="w-12 px-3 py-2.5" />
              </tr>
            </thead>
            {groups.map(([scene, rows]) => (
              <tbody key={scene}>
                <tr>
                  <th colSpan={5} className="border-t border-line bg-paper/60 px-5 pb-1.5 pt-3 text-left text-[15px] font-semibold">
                    {scene}
                  </th>
                </tr>
                {rows.map((r) => (
                  <tr key={r.id} className="border-t border-line-soft align-top">
                    <td className="px-5 py-3">
                      <div>{r.issue_type}</div>
                      <div className="mt-0.5 text-xs text-ink-soft">
                        {r.layer && <span className="mr-1.5 text-ink">{r.layer} {LAYERS[r.layer]}</span>}
                        {r.source} {day(r.created_at)}
                      </div>
                      {!r.flat && <div className="mt-0.5 text-xs text-ink-soft">非平地</div>}
                    </td>
                    <td className="px-3 py-3">
                      <Readout
                        metric={r.metric}
                        display={r.display}
                        ratio={r.ratio}
                        size="sm"
                        tone={toneOf(r.status)}
                        note={r.small_sample ? "样本少，只作参考" : undefined}
                      />
                      <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-ink-soft">
                        <Verdict status={r.status} text={r.status_text} />
                        {r.target !== null && <span>目标 {r.target}</span>}
                      </div>
                    </td>
                    <td className="px-3 py-3">
                      {r.outcomes ? <TrialStrip outcomes={r.outcomes} max={80} /> : <span className="text-xs text-ink-soft">{METRIC_HELP[r.metric]}</span>}
                      {r.note && <div className="mt-1.5 max-w-[46ch] text-xs text-ink-soft">{r.note}</div>}
                    </td>
                    <td className="px-3 py-3 text-[13px]">
                      <div>{r.robot || "—"}</div>
                      <div className="max-w-[28ch] break-all text-xs text-ink-soft">{r.version}</div>
                      {r.prev_version && (
                        <div className="mt-1 max-w-[28ch] break-all text-xs text-ink-soft">
                          本版合计 <span className="num text-ink">{r.version_display}</span>，上一版 <span className="num text-ink">{r.prev_display}</span>
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-3">
                      <button className="rounded p-1 text-ink-soft hover:text-fail" onClick={() => remove(r)} aria-label="删除这条记录">
                        <Trash2 className="size-4" />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            ))}
          </table>
        </div>
      )}

      {measures.length > 0 && (
        <section className="mt-8">
          <h2 className="mb-1 text-[17px] font-semibold">测量值</h2>
          <p className="mb-3 max-w-[70ch] text-sm text-ink-soft">摆幅、稳定时间这类越小越好的量，长测看护结束时自动写入。同一项比上一版大 10% 以上标回退。</p>
          <div className="overflow-hidden rounded-md border border-line bg-panel">
            <table className="w-full border-collapse text-left text-sm">
              <thead>
                <tr className="border-b border-line text-[13px] text-ink-soft">
                  <th className="px-5 py-2.5 font-normal">场景与指标</th>
                  <th className="px-3 py-2.5 font-normal">中位</th>
                  <th className="px-3 py-2.5 font-normal">最大</th>
                  <th className="px-3 py-2.5 font-normal">判定</th>
                  <th className="px-3 py-2.5 font-normal">版本</th>
                  <th className="w-12 px-3 py-2.5" />
                </tr>
              </thead>
              <tbody>
                {measures.map((m) => (
                  <tr key={m.id} className="border-t border-line-soft align-top">
                    <td className="px-5 py-3">
                      <div>
                        {m.scene}：{m.metric}
                      </div>
                      <div className="mt-0.5 text-xs text-ink-soft">
                        {m.layer && <span className="mr-1.5 text-ink">{m.layer}</span>}
                        {m.source} {day(m.created_at)}，{m.n} 个样本{m.note ? `；${m.note}` : ""}
                      </div>
                    </td>
                    <td className={cn("num px-3 py-3 text-2xl font-semibold", toneOf(m.status) === "fail" ? "text-fail" : toneOf(m.status) === "pass" ? "text-pass" : "")}>
                      {m.value}
                      <span className="ml-0.5 text-sm font-normal text-ink-soft">{m.unit}</span>
                    </td>
                    <td className="num px-3 py-3 text-lg">{m.max ?? "—"}</td>
                    <td className="px-3 py-3">
                      <Verdict status={m.status} text={m.status_text} />
                      <div className="mt-1 text-xs text-ink-soft">
                        {m.target !== null && `目标 ≤${m.target}${m.unit}`}
                        {m.prev_version && `${m.target !== null ? "，" : ""}上一版 ${m.prev_value}${m.unit}`}
                      </div>
                    </td>
                    <td className="max-w-[24ch] break-all px-3 py-3 text-xs text-ink-soft">
                      <div className="text-ink">{m.robot || "—"}</div>
                      {m.version}
                    </td>
                    <td className="px-3 py-3">
                      <button className="rounded p-1 text-ink-soft hover:text-fail" onClick={() => removeMeasure(m)} aria-label="删除这条测量">
                        <Trash2 className="size-4" />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </>
  );
}

function RecordForm({ onSaved }: { onSaved: () => void }) {
  const [f, setF] = useState({ scene: "", issue_type: "", metric: "CPI", total: "", events: "0", target: "", version: "", robot: "", note: "", layer: "" });
  const [flat, setFlat] = useState(true);
  const [error, setError] = useState("");
  const timeBased = f.metric === "HPI" || f.metric === "HPF";
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setF({ ...f, [k]: e.target.value });

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await api("/records", {
        body: { ...f, flat, total: Number(f.total), events: Number(f.events), target: f.target === "" ? null : Number(f.target) },
      });
      onSaved();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  return (
    <form className="grid grid-cols-2 gap-4" onSubmit={save}>
      <Field label="功能场景" className="col-span-2">
        <Input required value={f.scene} onChange={set("scene")} placeholder="例如 Mode 切换至 Stand" />
      </Field>
      <Field label="问题类型">
        <Input required value={f.issue_type} onChange={set("issue_type")} placeholder="例如 摔倒、卡住、切换失败" />
      </Field>
      <Field label="指标" hint={METRIC_HELP[f.metric]}>
        <Select value={f.metric} onChange={set("metric")}>
          {Object.keys(METRIC_HELP).map((m) => (
            <option key={m}>{m}</option>
          ))}
        </Select>
      </Field>
      <Field label={timeBased ? "累计小时" : "总次数"}>
        <Input required type="number" min={0} step={timeBased ? 0.01 : 1} value={f.total} onChange={set("total")} />
      </Field>
      <Field label={f.metric.endsWith("F") ? "摔倒次数" : "issue 次数"}>
        <Input required type="number" min={0} step={1} value={f.events} onChange={set("events")} />
      </Field>
      <Field label="测试层级" hint="决定“执行节奏”里算哪一层">
        <Select value={f.layer} onChange={set("layer")}>
          <option value="">不分层</option>
          {Object.entries(LAYERS).map(([k, v]) => (
            <option key={k} value={k}>
              {k} {v}
            </option>
          ))}
        </Select>
      </Field>
      <label className="flex items-end gap-2 pb-2 text-sm">
        <input type="checkbox" checked={flat} onChange={(e) => setFlat(e.target.checked)} className="size-4" />
        平地无障碍（有摔倒记 F1，一票否决）
      </label>
      <Field label="目标值" hint="空着时第一版自动当基线">
        <Input type="number" min={0} value={f.target} onChange={set("target")} />
      </Field>
      <Field label="机器">
        <Input value={f.robot} onChange={set("robot")} placeholder="HU_D04_01_060" />
      </Field>
      <Field label="固件版本" className="col-span-2">
        <Input value={f.version} onChange={set("version")} placeholder="robot-teleophu-d-26.8.2.20261105175940" />
      </Field>
      <Field label="备注" className="col-span-2">
        <Input value={f.note} onChange={set("note")} placeholder="测法、证据位置" />
      </Field>
      {error && <p role="alert" className="col-span-2 text-sm text-fail">{error}</p>}
      <div className={cn("col-span-2 mt-1 flex justify-end gap-2")}>
        <DialogClose asChild>
          <Button type="button" variant="ghost">
            取消
          </Button>
        </DialogClose>
        <Button type="submit" variant="primary">
          保存记录
        </Button>
      </div>
    </form>
  );
}
