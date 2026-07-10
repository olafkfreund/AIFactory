/**
 * Toast Hook
 *
 * Manages toast state for displaying notifications.
 */
import { create } from 'zustand';

import type { ReactNode } from 'react';
import type { ToastActionElement, ToastProps } from '../components/ui/toast';

const TOAST_LIMIT = 1;
const TOAST_REMOVE_DELAY = 1000000;

type ToasterToast = ToastProps & {
  id: string;
  title?: ReactNode;
  description?: ReactNode;
  action?: ToastActionElement;
};

let count = 0;

function genId() {
  count = (count + 1) % Number.MAX_SAFE_INTEGER;
  return count.toString();
}

interface State {
  toasts: ToasterToast[];
}

const useToastStore = create<State>(() => ({ toasts: [] }));

const toastTimeouts = new Map<string, ReturnType<typeof setTimeout>>();

const addToRemoveQueue = (toastId: string) => {
  if (toastTimeouts.has(toastId)) {
    return;
  }

  const timeout = setTimeout(() => {
    toastTimeouts.delete(toastId);
    useToastStore.setState((state) => ({
      toasts: state.toasts.filter((t) => t.id !== toastId),
    }));
  }, TOAST_REMOVE_DELAY);

  toastTimeouts.set(toastId, timeout);
};

function dismiss(toastId?: string) {
  if (toastId) {
    addToRemoveQueue(toastId);
  } else {
    useToastStore.getState().toasts.forEach((t) => addToRemoveQueue(t.id));
  }

  useToastStore.setState((state) => ({
    toasts: state.toasts.map((t) =>
      t.id === toastId || toastId === undefined ? { ...t, open: false } : t
    ),
  }));
}

type Toast = Omit<ToasterToast, 'id'>;

function toast({ ...props }: Toast) {
  const id = genId();

  const update = (props: ToasterToast) =>
    useToastStore.setState((state) => ({
      toasts: state.toasts.map((t) => (t.id === id ? { ...t, ...props, id } : t)),
    }));

  const dismissThis = () => dismiss(id);

  useToastStore.setState((state) => ({
    toasts: [
      {
        ...props,
        id,
        open: true,
        onOpenChange: (open: boolean) => {
          if (!open) dismissThis();
        },
      },
      ...state.toasts,
    ].slice(0, TOAST_LIMIT),
  }));

  return {
    id: id,
    dismiss: dismissThis,
    update,
  };
}

function useToast() {
  const toasts = useToastStore((state) => state.toasts);
  return { toasts, toast, dismiss };
}

export { useToast, toast };
