import { useEffect, useRef, useState } from "react";
import { ChevronDown, Loader2 } from "lucide-react";
import { api, cn } from "@/lib";

type Action = { key: string; label: string; danger: boolean; no_hanging: boolean; hanging_only: boolean };
type Res = { ok: boolean; text: string };

const GROUPS = [
  { title: "基础状态", keys: ["damping", "prepare", "walk"] },
  { title: "姿态", keys: ["sit", "lie", "stand_sit", "stand_lie", "stand_hang"] },
];
const CONFIRM: Record<string, string> = {
  damping: "全身电机会立刻无力，机器人掉进天车。确认天车挂好了？",
  sit: "机器人会坐下，身后要有椅子或足够空间。",
  lie: "机器人会躺下，周围 1.5 m 内不能有人和东西。",
};

export function RobotControl({ disabled, onDone }: { disabled: boolean; onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const [acts, setActs] = useState<Action[]>([]);
  const [hanging, setHanging] = useState(() => localStorage.getItem("hanging") !== "0");
  const [ask, setAsk] = useState<Action | null>(null);
  const [busy, setBusy] = useState("");
  const [res, setRes] = useState<Res | null>(null);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (open) api<{ actions: Action[] }>("/control").then((r) => setActs(r.actions)).catch(() => undefined);
    else setAsk(null);
  }, [open]);
  useEffect(() => localStorage.setItem("hanging", hanging ? "1" : "0"), [hanging]);
  useEffect(() => {
    const close = (e: MouseEvent) => !busy && box.current && !box.current.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [busy]);

  const click = (a: Action) => {
    setRes(null);
    if (CONFIRM[a.key]) setAsk(a);
    else run(a);
  };

  const run = async (a: Action) => {
    setAsk(null);
    setBusy(a.label);
    setRes(null);
    try {
      setRes(await api<Res>("/control", { body: { action: a.key, hanging, unlocked: a.danger } }));
    } catch (e) {
      setRes({ ok: false, text: (e as Error).message });
    } finally {
      setBusy("");
      onDone();
    }
  };



  return (
    <div className="relative" ref={box}>
      <button
        disabled={disabled}
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1 rounded-[3px] border border-current/40 px-2.5 py-1 text-xs font-medium hover:bg-white/10 disabled:opacity-40"
      >
        切换状态
        <ChevronDown className="size-3.5" />
      </button>
      {open && (
        <div className="absolute right-0 top-9 z-50 w-[340px] rounded-md border border-line bg-panel p-4 text-ink shadow-[0_12px_40px_rgb(31_41_51/0.18)]">
          <label className="mb-3 flex items-center gap-2 text-sm">
            <input type="checkbox" checked={hanging} onChange={(e) => setHanging(e.target.checked)} />
            <span>
              吊装保护
              <span className="ml-1 text-xs text-ink-soft">挂着天车时勾上，不能坐下、躺下</span>
            </span>
          </label>
          {GROUPS.map((g) => (
            <div key={g.title} className="mb-3">
              <div className="mb-1.5 text-xs text-ink-soft">{g.title}</div>
              <div className="grid grid-cols-3 gap-1.5">
                {g.keys
                  .map((k) => acts.find((a) => a.key === k))
                  .filter((a): a is Action => !!a && !(hanging ? a.no_hanging : a.hanging_only))
                  .map((a) => (
                    <button
                      key={a.key}
                      disabled={!!busy || !!ask}
                      onClick={() => click(a)}
                      className={cn(
                        "flex h-10 items-center justify-center gap-1 rounded-[4px] border text-sm disabled:opacity-40",
                        a.danger ? "border-fail/50 text-fail hover:border-fail" : "border-line hover:border-ink",
                      )}
                    >
                      {busy === a.label && <Loader2 className="size-3.5 animate-spin" />}
                      {a.label}
                    </button>
                  ))}
              </div>
            </div>
          ))}
          {ask && (
            <div className={cn("mb-2 rounded-[4px] border p-3", ask.danger ? "border-fail bg-fail/5" : "border-line bg-line-soft")}>
              <p className={cn("text-sm", ask.danger && "text-fail")}>{CONFIRM[ask.key]}</p>
              <div className="mt-2 flex justify-end gap-2">
                <button className="h-8 rounded-[4px] border border-line px-3 text-sm hover:border-ink" onClick={() => setAsk(null)}>
                  取消
                </button>
                <button
                  className={cn("h-8 rounded-[4px] px-3 text-sm text-white", ask.danger ? "bg-fail hover:bg-fail/90" : "bg-ink hover:bg-ink/90")}
                  onClick={() => run(ask)}
                >
                  确认{ask.label}
                </button>
              </div>
            </div>
          )}
          {res && <p className={cn("mt-3 text-sm", res.ok ? "text-pass" : "text-fail")}>{res.text}</p>}
          <p className="mt-2 text-xs text-ink-soft">起身后自动回 Walk。</p>
        </div>
      )}
    </div>
  );
}
