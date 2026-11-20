import { cn } from "@/lib";

/** 试验带：每一次尝试一格，成功青绿、失败红，按时间顺序排。超过 max 只画最后 max 次。 */
export function TrialStrip({ outcomes, max = 120, pending = 0, className }: { outcomes: string; max?: number; pending?: number; className?: string }) {
  const shown = outcomes.slice(-max);
  const hidden = outcomes.length - shown.length;
  return (
    <div className={cn("flex items-center gap-2", className)}>
      {hidden > 0 && <span className="num text-xs text-ink-soft">前 {hidden} 次略</span>}
      <div className="flex flex-wrap gap-[2px]" role="img" aria-label={`共 ${outcomes.length} 次，失败 ${[...outcomes].filter((c) => c === "0").length} 次`}>
        {[...shown].map((c, i) => (
          <span key={i} className={cn("h-[18px] w-[5px] rounded-[1px]", c === "1" ? "bg-pass" : "bg-fail")} />
        ))}
        {Array.from({ length: pending }).map((_, i) => (
          <span key={`p${i}`} className="h-[18px] w-[5px] rounded-[1px] border border-line-soft" />
        ))}
      </div>
    </div>
  );
}
