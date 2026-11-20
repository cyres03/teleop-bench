import { Square } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib";

const tone = (st: string) => (st === "ok" ? "bg-pass text-white" : st === "fail" ? "bg-fail text-white" : st === "run" ? "bg-ink text-panel" : "bg-line-soft text-ink-soft");

// 全身遥操回放、VR部分动捕自动推杆共用：上面一排阶段，中间一句当前在干什么，下面进度条和停止按钮
export function StageProgress({
  names,
  idx,
  failed,
  states,
  text,
  playing,
  done,
  elapsed,
  total,
  onStop,
  stopDisabled,
}: {
  names: string[];
  idx: number;
  failed?: boolean;
  states?: ("wait" | "run" | "ok" | "fail")[];
  text: string;
  playing: boolean;
  done: boolean;
  elapsed: number;
  total: number;
  onStop?: () => void;
  stopDisabled?: boolean;
}) {
  const pct = playing || done ? Math.min(100, (elapsed / Math.max(total, 1)) * 100) : 0;
  return (
    <div className="space-y-5">
      <ol className="flex gap-1">
        {names.map((n, i) => (
          <li
            key={n}
            className={cn(
              "flex flex-1 items-center justify-center rounded-[3px] px-1 py-1 text-center text-xs",
              tone(states ? states[i] : i < idx ? "ok" : i === idx ? (failed ? "fail" : "run") : "wait"),
            )}
          >
            {n}
          </li>
        ))}
      </ol>
      <p className={cn("text-center text-lg", playing && "font-semibold")}>{text}</p>
      <div>
        <div className="h-3 overflow-hidden rounded-full bg-line-soft">
          <div className="h-full bg-ink transition-all" style={{ width: `${pct}%` }} />
        </div>
        <div className="num mt-1 text-right text-sm text-ink-soft">
          {Math.min(elapsed, total).toFixed(0)} / {total.toFixed(0)} 秒
        </div>
      </div>
      {onStop && (
        <Button variant="danger" size="lg" className="h-12 w-full text-base" disabled={stopDisabled} onClick={onStop}>
          <Square />
          停止（危险时先按遥控器急停）
        </Button>
      )}
    </div>
  );
}
