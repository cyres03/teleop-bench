import * as React from "react";
import { cn } from "@/lib";

const control =
  "h-9 w-full rounded-[4px] border border-line bg-panel px-3 text-sm text-ink placeholder:text-ink-soft/60 focus-visible:border-ink focus-visible:outline-none";

export const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  ({ className, ...props }, ref) => <input ref={ref} className={cn(control, className)} {...props} />,
);
Input.displayName = "Input";

export const Select = React.forwardRef<HTMLSelectElement, React.SelectHTMLAttributes<HTMLSelectElement>>(
  ({ className, ...props }, ref) => <select ref={ref} className={cn(control, "pr-8", className)} {...props} />,
);
Select.displayName = "Select";

export function Field({ label, hint, children, className }: { label: string; hint?: string; children: React.ReactNode; className?: string }) {
  return (
    <label className={cn("flex flex-col gap-1.5", className)}>
      <span className="text-[13px] text-ink-soft">{label}</span>
      {children}
      {hint && <span className="text-xs text-ink-soft/80">{hint}</span>}
    </label>
  );
}

export function Check({ checked, onChange, children, disabled }: { checked: boolean; onChange: (v: boolean) => void; children: React.ReactNode; disabled?: boolean }) {
  return (
    <label className={cn("flex cursor-pointer items-start gap-2.5 text-sm", disabled && "opacity-50")}>
      <input
        type="checkbox"
        className="mt-[3px] size-4 accent-[var(--color-ink)]"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span>{children}</span>
    </label>
  );
}
