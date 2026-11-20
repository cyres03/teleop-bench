import { cn } from "@/lib";

/** 指标读数：大号窄体数字 + 小号样本比。 */
export function Readout({ metric, display, ratio, size = "lg", tone = "ink", note }: {
  metric: string;
  display: string;
  ratio: string;
  size?: "sm" | "lg";
  tone?: "ink" | "pass" | "fail";
  note?: string;
}) {
  const color = tone === "pass" ? "text-pass" : tone === "fail" ? "text-fail" : "text-ink";
  return (
    <div className="flex items-baseline gap-2">
      <span className="text-[13px] text-ink-soft">{metric}</span>
      <span className={cn("num font-semibold leading-none", color, size === "lg" ? "text-[64px]" : "text-[30px]")}>{display}</span>
      <span className={cn("num text-ink-soft", size === "lg" ? "text-xl" : "text-base")}>{ratio}</span>
      {note && <span className="text-xs text-wait">{note}</span>}
    </div>
  );
}
