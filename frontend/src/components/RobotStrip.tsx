import { RobotControl } from "@/components/RobotControl";
import { cn } from "@/lib";

export type RobotInfo = { sn: string; version: string; robot_status: string; battery: string; reachable: boolean; simulated: boolean };

function Item({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <span className="text-xs opacity-75">{label}</span>
      <span className="font-medium">{value || "—"}</span>
    </div>
  );
}

/** 顶部机器人条：真机为墨色；演练为靛紫斜纹；有指令在执行时整条变安全橙斜纹。 */
export function RobotStrip({ info, live, liveText, simulated, onToggleSim, onRefresh }: {
  info: RobotInfo | null;
  live: boolean;
  liveText: string;
  simulated: boolean;
  onToggleSim: (v: boolean) => void;
  onRefresh: () => void;
}) {
  const connected = !!info?.sn;
  return (
    <header
      className={cn(
        "flex h-12 shrink-0 items-center gap-6 px-5 text-sm",
        live ? "hazard text-white" : simulated ? "sim-hatch bg-[#ecebfa] text-sim" : "bg-ink text-panel",
      )}
    >
      {live ? (
        <div className="flex items-center gap-3 rounded-[3px] bg-ink/85 px-3 py-1 font-semibold">
          <span className="size-2 animate-pulse rounded-full bg-live" />
          机器人执行中：{liveText}
        </div>
      ) : (
        <div className="flex items-center gap-2 font-semibold">
          <span className={cn("size-2 rounded-full", connected ? "bg-pass" : "bg-fail")} />
          {simulated ? "演练模式，不连真机" : connected ? info!.sn : "未连接机器人"}
        </div>
      )}
      {!live && connected && !simulated && (
        <>
          <Item label="固件" value={info!.version} />
          <Item label="状态" value={info!.robot_status} />
          <Item label="电量" value={info!.battery} />
        </>
      )}
      {!live && !connected && !simulated && <span className="opacity-80">连上机器人热点后点“重新识别”；只用台账可以不连</span>}
      <div className="ml-auto flex items-center gap-3">
        {(connected || simulated) && <RobotControl disabled={live} onDone={onRefresh} />}
        {!live && (
          <button className="rounded-[3px] px-2 py-1 text-xs underline-offset-2 hover:underline" onClick={onRefresh}>
            重新识别
          </button>
        )}
        <label className={cn("flex items-center gap-2 text-xs", live && "opacity-60")}>
          <input type="checkbox" className="size-3.5 accent-[var(--color-sim)]" checked={simulated} disabled={live} onChange={(e) => onToggleSim(e.target.checked)} />
          演练模式
        </label>
      </div>
    </header>
  );
}
