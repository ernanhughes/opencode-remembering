import * as fs from "node:fs/promises";
import * as path from "node:path";
import { AsyncLocalStorage } from "node:async_hooks";

/** Atomic filesystem helpers for the JSON backend. */

export async function ensureDir(dir: string): Promise<void> {
  await fs.mkdir(dir, { recursive: true });
}

/** Atomic write: temp file + rename. Never leaves half-written JSON. */
export async function atomicWriteFile(file: string, content: string): Promise<void> {
  await ensureDir(path.dirname(file));
  const tmp = `${file}.${process.pid}.tmp`;
  await fs.writeFile(tmp, content, "utf8");
  try {
    const handle = await fs.open(tmp, "r");
    try {
      await handle.sync();
    } finally {
      await handle.close();
    }
  } catch { /* best effort fsync */ }
  await fs.rename(tmp, file);
}

export async function readJsonFile<T>(file: string, fallback: T): Promise<T> {
  try {
    const raw = await fs.readFile(file, "utf8");
    return JSON.parse(raw) as T;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return fallback;
    throw error;
  }
}

export async function appendJsonLine(file: string, value: unknown): Promise<void> {
  await ensureDir(path.dirname(file));
  await fs.appendFile(file, `${JSON.stringify(value)}\n`, "utf8");
  // Best-effort durability: fsync the file after appending so a committed
  // record survives a machine crash, not just a clean shutdown.
  try {
    const handle = await fs.open(file, "r");
    try {
      await handle.sync();
    } finally {
      await handle.close();
    }
  } catch { /* best effort fsync */ }
}

/**
 * Recovery log: append-only diagnostics for torn-tail quarantines and
 * orphan-generation sweeps. Never throws; visibility for doctor/stats.
 */
export async function logRecovery(storeDir: string, kind: string, detail: string): Promise<void> {
  try {
    await ensureDir(storeDir);
    await fs.appendFile(
      path.join(storeDir, "recovery.jsonl"),
      `${JSON.stringify({ at: new Date().toISOString(), kind, detail })}\n`,
      "utf8",
    );
  } catch { /* diagnostics must never break writes */ }
}

export async function recoveryEvents(storeDir: string): Promise<number> {
  try {
    const raw = await fs.readFile(path.join(storeDir, "recovery.jsonl"), "utf8");
    return raw.split("\n").filter((l) => l.trim()).length;
  } catch {
    return 0;
  }
}

/** Total bytes quarantined from torn JSONL tails (0 when healthy). */
export async function tornQuarantinedBytes(storeDir: string): Promise<number> {
  try {
    const entries = await fs.readdir(storeDir);
    let total = 0;
    for (const entry of entries) {
      if (!entry.endsWith(".torn")) continue;
      try {
        total += (await fs.stat(path.join(storeDir, entry))).size;
      } catch { /* ignore */ }
    }
    return total;
  } catch {
    return 0;
  }
}

export async function readJsonLines<T>(file: string): Promise<T[]> {
  // Repair runs under the per-file lock, which is re-entrant (see
  // withFileLock): append paths already hold this file's lock when they read,
  // so recovery and append can never race into valid+torn+new corruption.
  return withFileLock(file, async () => {
    let content: string;
    try {
      content = await fs.readFile(file, "utf8");
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return [];
      throw error;
    }
    const endsNewline = content.endsWith("\n");
    const lines = content.split("\n");
    // The last split element is "" when the file ends with a newline.
    const lastIndex = lines.length - (endsNewline ? 1 : 0);
    const out: T[] = [];
    // Byte offset of each line start, for truncating before a torn tail.
    const offsets: number[] = [];
    let cursor = 0;
    for (let i = 0; i < lines.length; i++) {
      offsets.push(cursor);
      cursor += (lines[i] as string).length + 1;
    }
    for (let i = 0; i < lastIndex; i++) {
      const line = lines[i] as string;
      if (!line.trim()) continue;
      const isTail = i === lastIndex - 1 && !endsNewline;
      try {
        out.push(JSON.parse(line) as T);
      } catch {
        if (!isTail) {
          // A complete record that fails to parse is data corruption, not a
          // crash tear: fail loudly, never silently drop history.
          throw new Error(`corrupt JSONL at ${file}:${i + 1}`);
        }
        // Torn final record: the process died mid-append. Quarantine the
        // bytes for forensics AND repair the canonical file to the valid
        // prefix, so a later append cannot fuse valid+torn+new into a
        // mid-file corrupt line. Quarantine-then-repair runs atomically
        // under the file lock, so repeated reads quarantine exactly once.
        const tornOffset = offsets[i] as number;
        const prefix = content.slice(0, tornOffset);
        try {
          await fs.appendFile(`${file}.torn`, line, "utf8");
          await logRecovery(path.dirname(file), "torn-tail-quarantined", `${file}`);
        } catch { /* quarantine best effort */ }
        try {
          await atomicWriteFile(file, prefix);
        } catch {
          // Repair best effort: the prefix is still returned; the next read
          // retries the truncate. Never lose parsed records over this.
        }
        continue;
      }
    }
    return out;
  });
}

/**
 * Minimal async mutex per store file to serialize overlapping OpenCode ops.
 * Re-entrant within one async context (tracked via AsyncLocalStorage): store
 * append paths hold a file lock while reading through it, and recovery runs
 * under the same lock, so nested acquisition must not deadlock.
 */
const locks = new Map<string, Promise<void>>();
const heldKeys = new AsyncLocalStorage<Set<string>>();

export async function withFileLock<T>(key: string, fn: () => Promise<T>): Promise<T> {
  if (heldKeys.getStore()?.has(key)) return fn();
  const prior = locks.get(key) ?? Promise.resolve();
  let release!: () => void;
  const current = new Promise<void>((resolve) => { release = resolve; });
  const queued = prior.then(() => current);
  locks.set(key, queued);
  await prior;
  const store = heldKeys.getStore();
  const next = new Set(store ?? []);
  next.add(key);
  try {
    return await heldKeys.run(next, fn);
  } finally {
    release();
    if (locks.get(key) === queued) locks.delete(key);
  }
}

/** Test-only visibility into lock bookkeeping (map size, not behavior). */
export function __lockMapSizeForTests(): number {
  return locks.size;
}
