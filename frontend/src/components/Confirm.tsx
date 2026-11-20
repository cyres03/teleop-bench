import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";

type Req = { text: string; ok: string; cancel: string; title: string; danger: boolean; resolve: (v: boolean | null) => void; dismiss: boolean | null };
let push: ((r: Req) => void) | null = null;

// 代替浏览器原生 confirm：桌面版里原生弹窗会变成屏幕左上角的系统窗口
export function ask(text: string, opts: { ok?: string; danger?: boolean } = {}): Promise<boolean> {
  return new Promise((resolve) => {
    if (!push) return resolve(window.confirm(text));
    push({ text, ok: opts.ok ?? "确定", cancel: "取消", title: "请确认", danger: !!opts.danger, resolve: (v) => resolve(!!v), dismiss: false });
  });
}

// 两个都是正经选项（例如 保存 / 不保存）时用：点右上角关掉返回 null，表示先不决定
export function choose(text: string, opts: { title: string; yes: string; no: string }): Promise<boolean | null> {
  return new Promise((resolve) => {
    if (!push) return resolve(window.confirm(text));
    push({ text, ok: opts.yes, cancel: opts.no, title: opts.title, danger: false, resolve, dismiss: null });
  });
}

export function ConfirmHost() {
  const [req, setReq] = useState<Req | null>(null);
  useEffect(() => {
    push = setReq;
    return () => {
      push = null;
    };
  }, []);
  const done = (v: boolean | null) => {
    req?.resolve(v);
    setReq(null);
  };
  return (
    <Dialog open={!!req} onOpenChange={(o) => !o && done(req ? req.dismiss : false)}>
      {req && (
        <DialogContent title={req.title} className="w-[min(460px,92vw)]" autoFocus={false}>
          <p className="whitespace-pre-line text-[15px]">{req.text}</p>
          <div className="mt-5 flex justify-end gap-2">
            <Button variant="outline" onClick={() => done(false)}>
              {req.cancel}
            </Button>
            <Button variant={req.danger ? "danger" : "primary"} onClick={() => done(true)}>
              {req.ok}
            </Button>
          </div>
        </DialogContent>
      )}
    </Dialog>
  );
}
