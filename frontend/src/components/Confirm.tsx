import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";

type Req = { text: string; ok: string; danger: boolean; resolve: (v: boolean) => void };
let push: ((r: Req) => void) | null = null;

// 代替浏览器原生 confirm：桌面版里原生弹窗会变成屏幕左上角的系统窗口
export function ask(text: string, opts: { ok?: string; danger?: boolean } = {}): Promise<boolean> {
  return new Promise((resolve) => {
    if (!push) return resolve(window.confirm(text));
    push({ text, ok: opts.ok ?? "确定", danger: !!opts.danger, resolve });
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
  const done = (v: boolean) => {
    req?.resolve(v);
    setReq(null);
  };
  return (
    <Dialog open={!!req} onOpenChange={(o) => !o && done(false)}>
      {req && (
        <DialogContent title="请确认" className="w-[min(460px,92vw)]" autoFocus={false}>
          <p className="whitespace-pre-line text-[15px]">{req.text}</p>
          <div className="mt-5 flex justify-end gap-2">
            <Button variant="outline" onClick={() => done(false)}>
              取消
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
