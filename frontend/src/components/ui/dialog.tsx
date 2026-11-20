import * as DialogPrimitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import * as React from "react";
import { cn } from "@/lib";

export const Dialog = DialogPrimitive.Root;

export function DialogContent({ title, children, className, closable = true, autoFocus = true }: { title: string; children: React.ReactNode; className?: string; closable?: boolean; autoFocus?: boolean }) {
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="fixed inset-0 bg-ink/30" />
      <DialogPrimitive.Content
        onInteractOutside={(e) => !closable && e.preventDefault()}
        onEscapeKeyDown={(e) => !closable && e.preventDefault()}
        onOpenAutoFocus={(e) => !autoFocus && e.preventDefault()}
        className={cn("fixed left-1/2 top-1/2 max-h-[92vh] w-[min(560px,92vw)] -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-md border border-line bg-panel p-6 shadow-[0_12px_40px_rgb(31_41_51/0.18)] focus:outline-none", className)}
      >
        <div className="mb-5 flex items-center justify-between">
          <DialogPrimitive.Title className="text-lg font-semibold">{title}</DialogPrimitive.Title>
          {closable && (
            <DialogPrimitive.Close className="rounded p-1 text-ink-soft hover:text-ink" aria-label="关闭">
              <X className="size-5" />
            </DialogPrimitive.Close>
          )}
        </div>
        {children}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
}

export const DialogClose = DialogPrimitive.Close;
