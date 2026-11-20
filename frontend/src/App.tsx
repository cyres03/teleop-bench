import { useCallback, useEffect, useState } from "react";
import { BookOpen, Footprints, Gauge, Hand, PersonStanding, Repeat2, Ruler, Timer, Video } from "lucide-react";
import { RobotStrip, type RobotInfo } from "@/components/RobotStrip";
import { api, cn } from "@/lib";
import Ledger from "@/pages/Ledger";
import Stress from "@/pages/Stress";
import Watch from "@/pages/Watch";
import Latency from "@/pages/Latency";
import Boundary from "@/pages/Boundary";
import LBMotion from "@/pages/LBMotion";
import WBT from "@/pages/WBT";
import Gripper from "@/pages/Gripper";
import Help from "@/pages/Help";
import { ConfirmHost } from "@/components/Confirm";
import { AccessBar } from "@/components/AccessBar";

const PAGES = [
  { key: "ledger", label: "指标台账", icon: Gauge },
  { key: "stress", label: "模式切换压测", icon: Repeat2 },
  { key: "watch", label: "长测看护", icon: Timer },
  { key: "boundary", label: "边界探测", icon: Ruler },
  { key: "lbm", label: "LBMotion 推杆", icon: Footprints },
  { key: "wbt", label: "全身遥操回放", icon: PersonStanding },
  { key: "gripper", label: "夹爪夹持力", icon: Hand },
  { key: "latency", label: "视频延迟", icon: Video },
  { key: "help", label: "使用说明", icon: BookOpen },
] as const;
type PageKey = (typeof PAGES)[number]["key"];

type Live = { active: boolean; text: string };

export default function App() {
  const [page, setPage] = useState<PageKey>(() => (location.hash.slice(1) as PageKey) || "ledger");
  const [robot, setRobot] = useState<RobotInfo | null>(null);
  const [simulated, setSimulated] = useState(false);
  const [live, setLive] = useState<Live>({ active: false, text: "" });
  const [modeError, setModeError] = useState("");

  const refreshRobot = useCallback(() => {
    api<RobotInfo>("/robot")
      .then((r) => {
        setRobot(r);
        setSimulated(!!r.simulated); // 刷新页面后以后端的模式为准
      })
      .catch(() => setRobot(null));
  }, []);

  useEffect(() => {
    refreshRobot();
    const id = setInterval(refreshRobot, 10000);
    return () => clearInterval(id);
  }, [refreshRobot]);

  useEffect(() => {
    location.hash = page;
  }, [page]);

  // 浏览器后退、手改地址栏时跟着切页面
  useEffect(() => {
    const onHash = () => {
      const k = location.hash.slice(1) as PageKey;
      if (PAGES.some((p) => p.key === k)) setPage(k);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  // 压测在后台跑，换页面也要让顶部条保持“执行中”
  useEffect(() => {
    const id = setInterval(() => {
      api<{ running: boolean; current: string; simulated: boolean }>("/stress")
        .then((s) => setLive({ active: s.running, text: (s.simulated ? "演练 " : "") + (s.current || "模式切换压测") }))
        .catch(() => undefined);
    }, 1000);
    return () => clearInterval(id);
  }, []);

  const toggleSim = async (v: boolean) => {
    try {
      await api("/mode", { body: { simulated: v } });
      setSimulated(v);
      setModeError("");
      refreshRobot();
    } catch (e) {
      setModeError((e as Error).message);
    }
  };

  return (
    <div className="flex h-full flex-col">
      <RobotStrip info={robot} live={live.active} liveText={live.text} simulated={simulated} onToggleSim={toggleSim} onRefresh={refreshRobot} />
      {modeError && <div className="bg-fail px-5 py-1.5 text-sm text-white">{modeError}</div>}
      <ConfirmHost />
      <AccessBar sn={robot?.sn ?? ""} simulated={simulated} />
      <div className="flex min-h-0 flex-1">
        <nav className="flex w-[188px] shrink-0 flex-col gap-0.5 border-r border-line bg-panel px-3 py-5" aria-label="功能">
          <div className="mb-6 px-2">
            <div className="num text-[26px] font-semibold leading-none tracking-wide">Teleop Bench</div>
            <div className="mt-1 text-xs text-ink-soft">遥操测试台 0.1</div>
          </div>
          {PAGES.map(({ key, label, icon: Icon }) => (
            <button
              key={key}
              onClick={() => setPage(key)}
              aria-current={page === key ? "page" : undefined}
              className={cn(
                "flex items-center gap-2.5 rounded-[4px] px-2.5 py-2 text-left text-sm transition-colors",
                page === key ? "bg-ink text-panel" : "text-ink-soft hover:bg-line-soft hover:text-ink",
              )}
            >
              <Icon className="size-4" />
              {label}
            </button>
          ))}
        </nav>
        <main className="min-w-0 flex-1 overflow-y-auto">
          <div className="mx-auto max-w-[1180px] px-8 py-7">
            {page === "ledger" && <Ledger />}
            {page === "stress" && <Stress robot={robot} simulated={simulated} />}
            {page === "watch" && <Watch robot={robot} simulated={simulated} />}
            {page === "boundary" && <Boundary robot={robot} simulated={simulated} />}
            {page === "lbm" && <LBMotion robot={robot} simulated={simulated} />}
            {page === "wbt" && <WBT robot={robot} simulated={simulated} />}
            {page === "gripper" && <Gripper robot={robot} simulated={simulated} />}
            {page === "latency" && <Latency />}
            {page === "help" && <Help />}
          </div>
        </main>
      </div>
    </div>
  );
}

export function PageHead({ title, desc, children }: { title: string; desc: string; children?: React.ReactNode }) {
  return (
    <div className="mb-6 flex items-end justify-between gap-6 border-b border-line pb-4">
      <div>
        <h1 className="text-[22px] font-semibold leading-tight">{title}</h1>
        <p className="mt-1 max-w-[70ch] text-ink-soft">{desc}</p>
      </div>
      {children && <div className="flex shrink-0 gap-2">{children}</div>}
    </div>
  );
}
