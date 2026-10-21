import { createContext, useCallback, useContext, useState, type ReactNode } from "react";
import * as RadixToast from "@radix-ui/react-toast";
import { X } from "lucide-react";

export type ToastTone = "info" | "success" | "error";
export type ToastInput = {
  title: string;
  description?: string;
  tone?: ToastTone;
  action?: { label: string; onClick: () => void };
  /** Milliseconds; 5 s by default, 10 s for Undo (07 §6.7). */
  duration?: number;
};
type ToastItem = ToastInput & { id: number };

const ToastContext = createContext<(t: ToastInput) => void>(() => undefined);

/** Bottom-right, 5 s, at most 3 stacked, optional Undo/Open action (07 §6.5). */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const push = useCallback((t: ToastInput) => {
    setItems((list) => [...list.slice(-2), { ...t, id: Date.now() + Math.random() }]);
  }, []);
  const remove = (id: number) => setItems((list) => list.filter((t) => t.id !== id));
  return (
    <ToastContext.Provider value={push}>
      <RadixToast.Provider duration={5000} label="Notification">
        {children}
        {items.map((t) => (
          <RadixToast.Root
            key={t.id}
            className={`toast toast-${t.tone ?? "info"}`}
            onOpenChange={(open) => !open && remove(t.id)}
            type={t.tone === "error" ? "foreground" : "background"}
            duration={t.duration}
          >
            <div className="grow">
              <RadixToast.Title className="toast-title">
                <strong>{t.title}</strong>
              </RadixToast.Title>
              {t.description && <RadixToast.Description className="small muted">{t.description}</RadixToast.Description>}
            </div>
            {t.action && (
              <RadixToast.Action asChild altText={t.action.label}>
                <button type="button" className="btn btn-sm btn-secondary" onClick={t.action.onClick}>
                  {t.action.label}
                </button>
              </RadixToast.Action>
            )}
            <RadixToast.Close className="btn btn-ghost btn-sm" aria-label="Dismiss">
              <X size={14} aria-hidden />
            </RadixToast.Close>
          </RadixToast.Root>
        ))}
        <RadixToast.Viewport className="toast-viewport" />
      </RadixToast.Provider>
    </ToastContext.Provider>
  );
}

export function useToast(): (t: ToastInput) => void {
  return useContext(ToastContext);
}
