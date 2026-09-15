import { useCallback, useRef, useState } from "react";

const MAX_HISTORY = 60;

export function useUndoableState<T>(initial: T) {
  const [present, setPresentState] = useState<T>(initial);
  const [pastLen, setPastLen] = useState(0);
  const [futureLen, setFutureLen] = useState(0);
  const pastRef = useRef<T[]>([]);
  const futureRef = useRef<T[]>([]);
  const presentRef = useRef(present);
  presentRef.current = present;

  const syncMeta = () => {
    setPastLen(pastRef.current.length);
    setFutureLen(futureRef.current.length);
  };

  const reset = useCallback((value: T) => {
    pastRef.current = [];
    futureRef.current = [];
    presentRef.current = value;
    setPresentState(value);
    setPastLen(0);
    setFutureLen(0);
  }, []);

  const set = useCallback((next: T | ((prev: T) => T), record = true) => {
    const prev = presentRef.current;
    const value =
      typeof next === "function" ? (next as (p: T) => T)(prev) : next;
    if (record) {
      pastRef.current = [...pastRef.current, prev].slice(-MAX_HISTORY);
      futureRef.current = [];
    }
    presentRef.current = value;
    setPresentState(value);
    syncMeta();
  }, []);

  const undo = useCallback(() => {
    if (pastRef.current.length === 0) return false;
    const previous = pastRef.current[pastRef.current.length - 1];
    pastRef.current = pastRef.current.slice(0, -1);
    futureRef.current = [presentRef.current, ...futureRef.current].slice(
      0,
      MAX_HISTORY,
    );
    presentRef.current = previous;
    setPresentState(previous);
    syncMeta();
    return true;
  }, []);

  const redo = useCallback(() => {
    if (futureRef.current.length === 0) return false;
    const next = futureRef.current[0];
    futureRef.current = futureRef.current.slice(1);
    pastRef.current = [...pastRef.current, presentRef.current].slice(
      -MAX_HISTORY,
    );
    presentRef.current = next;
    setPresentState(next);
    syncMeta();
    return true;
  }, []);

  return {
    value: present,
    set,
    reset,
    undo,
    redo,
    canUndo: pastLen > 0,
    canRedo: futureLen > 0,
  };
}
