import type { ToastItem } from "../hooks/useToast";

export function ToastList({ toasts }: { toasts: ToastItem[] }) {
  return (
    <>
      {toasts.map((t) => (
        <div key={t.id} className={`toast${t.isError ? " error" : ""}`}>
          {t.message}
        </div>
      ))}
    </>
  );
}
