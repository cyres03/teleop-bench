import { cn } from "@/lib";

export type LatRun = { id: number; kind: string; link: string; version: string; created_at: number; result: { ok: boolean; mean?: number; p5?: number; p95?: number; pairs?: number } };

export const MATRIX = [
  { link: "近场", codec: "H264", target: 100 },
  { link: "近场", codec: "H265", target: 100 },
  { link: "近场", codec: "JPEG", target: 100 },
  { link: "远程 阿里云", codec: "H264", target: 150 },
  { link: "远程 腾讯云", codec: "H264", target: 150 },
] as const;

const SCALE = 300; // ms，横轴满格
const REGRESS = 0.1; // 比上一版均值高 10% 以上算回退（版本间不回退）

const avg = (rs: LatRun[]) => rs.reduce((a, r) => a + (r.result.mean ?? 0), 0) / rs.length;

/** 固件版本按第一次出现的时间排序。 */
function versionOrder(runs: LatRun[]) {
  const first = new Map<string, number>();
  for (const r of runs) if (r.version) first.set(r.version, Math.min(first.get(r.version) ?? Infinity, r.created_at));
  return [...first.keys()].sort((a, b) => first.get(a)! - first.get(b)!);
}
const pct = (ms: number) => `${Math.max(0, Math.min(100, (ms / SCALE) * 100))}%`;

/** 今日矩阵：每格一行，横条是各次均值的平均，细线是最差的 P5–P95，竖刻度是目标值。只算最新固件版本的数据，和上一版比。 */
export function LatencyBoard({ runs: all, perCell = 2, onPick }: { runs: LatRun[]; perCell?: number; onPick?: (link: string, codec: string) => void }) {
  const measures = all.filter((r) => r.kind === "measure" && r.result.ok);
  const order = versionOrder(measures);
  const latest = measures.length ? measures.reduce((a, b) => (a.created_at > b.created_at ? a : b)).version : "";
  const runs = measures.filter((r) => r.version === latest);
  return (
    <div className="rounded-md border border-line bg-panel px-5 py-4">
      <div className="mb-2 grid grid-cols-[150px_64px_1fr_150px] items-end gap-4 text-[13px] text-ink-soft">
        <span>链路与编码</span>
        <span>次数</span>
        <div className="relative h-4">
          {[0, 50, 100, 150, 200, 250, 300].map((t) => (
            <span key={t} className="num absolute -translate-x-1/2 text-xs" style={{ left: pct(t) }}>
              {t}
            </span>
          ))}
        </div>
        <span className="text-right">均值，P5–P95 ms</span>
      </div>
      {MATRIX.map((cell) => {
        const key = `${cell.link} ${cell.codec}`;
        const mine = runs.filter((r) => r.link === key);
        const mean = mine.length ? avg(mine) : null;
        const prevVer = order.slice(0, order.indexOf(latest)).reverse().find((v) => measures.some((r) => r.version === v && r.link === key));
        const prev = prevVer ? avg(measures.filter((r) => r.version === prevVer && r.link === key)) : null;
        const regressed = mean !== null && prev !== null && mean > prev * (1 + REGRESS);
        const lo = mine.length ? Math.min(...mine.map((r) => r.result.p5 ?? 0)) : null;
        const hi = mine.length ? Math.max(...mine.map((r) => r.result.p95 ?? 0)) : null;
        const pass = mean !== null && mean <= cell.target && !regressed;
        return (
          <button
            key={key}
            type="button"
            onClick={() => onPick?.(cell.link, cell.codec)}
            className="grid w-full grid-cols-[150px_64px_1fr_150px] items-center gap-4 border-t border-line-soft py-3 text-left hover:bg-paper/60"
          >
            <span className="text-sm font-medium">
              {cell.link} <span className="num text-base">{cell.codec}</span>
            </span>
            <span className="flex gap-1" aria-label={`已测 ${mine.length} 次，计划 ${perCell} 次`}>
              {Array.from({ length: Math.max(perCell, mine.length) }).map((_, i) => (
                <span key={i} className={cn("size-2.5 rounded-full", i < mine.length ? "bg-ink" : "border border-line")} />
              ))}
            </span>
            <div className="relative h-6">
              <div className="absolute inset-y-[11px] left-0 right-0 bg-line-soft" />
              {mean !== null && lo !== null && hi !== null && (
                <>
                  <div className="absolute top-[7px] h-[10px] border-x border-ink/60" style={{ left: pct(lo), width: `calc(${pct(hi)} - ${pct(lo)})` }}>
                    <div className="absolute inset-x-0 top-[4px] h-px bg-ink/60" />
                  </div>
                  <div className={cn("absolute top-[3px] h-[18px] rounded-[2px]", pass ? "bg-pass" : "bg-fail")} style={{ left: 0, width: pct(mean) }} />
                </>
              )}
              <div className="absolute -top-1 bottom-[-4px] w-[2px] bg-live" style={{ left: pct(cell.target) }} title={`目标 ≤${cell.target} ms`} />
            </div>
            <span className="text-right">
              {mean === null ? (
                <span className="text-sm text-ink-soft">未测</span>
              ) : (
                <>
                  <span className={cn("num text-[26px] font-semibold leading-none", pass ? "text-pass" : "text-fail")}>{mean.toFixed(1)}</span>
                  <span className="num ml-1.5 text-sm text-ink-soft">
                    {lo!.toFixed(0)}–{hi!.toFixed(0)}
                  </span>
                </>
              )}
              {prev !== null && (
                <span className={cn("block text-xs", regressed ? "font-medium text-fail" : "text-ink-soft")} title={prevVer}>
                  {regressed ? "比上一版回退" : "上一版"} {prev.toFixed(1)}
                </span>
              )}
            </span>
          </button>
        );
      })}
      <p className="mt-2 text-xs text-ink-soft">
        只算最新固件版本{latest ? `（${latest}）` : ""}的数据。橙色竖线是目标：近场 ≤100 ms，远程 ≤150 ms（提测单 2.4.5）；版本间不回退，比上一版均值高 10% 以上标红。点一行会把下面的“链路”“编码”切到这一格。
      </p>
    </div>
  );
}
