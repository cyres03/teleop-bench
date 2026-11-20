import { useEffect, useState } from "react";
import { api, cn } from "@/lib";

/** 判定标记：F1 一票否决 > 不达标 > 比上一版回退 > 达标 > 首测基线。 */
export function Verdict({ status, text }: { status?: string; text?: string }) {
  if (!status || status === "none") return null;
  const style: Record<string, string> = {
    f1: "bg-fail text-white border-fail",
    fail: "text-fail border-fail",
    regress: "text-fail border-fail border-dashed",
    pass: "text-pass border-pass",
    baseline: "text-ink-soft border-line border-dashed",
  };
  return <span className={cn("inline-block whitespace-nowrap rounded-[3px] border px-1.5 py-px text-xs", style[status])}>{text}</span>;
}

export const toneOf = (status?: string): "ink" | "pass" | "fail" =>
  status === "f1" || status === "fail" || status === "regress" ? "fail" : status === "pass" ? "pass" : "ink";

type Layer = { layer: string; name: string; when: string; due: boolean; why: string; last_at: number | null };

/** 执行节奏：L2 / L4 / L5 每版本，L3 每周，L1 新硬件或新算法时。 */
export function Cadence() {
  const [data, setData] = useState<{ version: string; source: string; layers: Layer[] } | null>(null);
  useEffect(() => {
    api<{ version: string; source: string; layers: Layer[] }>("/cadence").then(setData).catch(() => setData(null));
  }, []);
  if (!data) return null;
  return (
    <section aria-label="执行节奏" className="mb-6">
      <div className="mb-1.5 text-[13px] text-ink-soft">
        执行节奏{data.version ? `：按${data.source}版本 ${data.version} 判断` : ""}
      </div>
      <div className="grid grid-cols-5 overflow-hidden rounded-md border border-line bg-panel">
      {data.layers.map((l) => (
        <div key={l.layer} className={cn("border-l border-line-soft px-4 py-3 first:border-l-0", l.due && "bg-live/8")}>
          <div className="flex items-baseline gap-2">
            <span className="num text-xl font-semibold">{l.layer}</span>
            <span className="text-sm">{l.name}</span>
          </div>
          <div className="mt-0.5 text-xs text-ink-soft">{l.when}</div>
          <div className={cn("mt-1.5 text-xs", l.due ? "font-medium text-live-deep" : "text-ink-soft")}>
            {l.due ? `该跑了：${l.why}` : l.why}
          </div>
        </div>
      ))}
      </div>
    </section>
  );
}
