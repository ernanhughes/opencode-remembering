import * as fs from "node:fs/promises";
import * as path from "node:path";

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
}

export async function readJsonLines<T>(file: string): Promise<T[]> {
  let content: string;
  try {
    content = await fs.readFile(file, "utf8");
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return [];
    throw error;
  }
  const out: T[] = [];
  for (const [i, line] of content.split("\n").entries()) {
    if (!line.trim()) continue;
    try {
      out.push(JSON.parse(line) as T);
    } catch {
      throw new Error(`corrupt JSONL at ${file}:${i + 1}`);
    }
  }
  return out;
}

/** Minimal async mutex per store file to serialize overlapping OpenCode ops. */
const locks = new Map<string, Promise<void>>();

export async function withFileLock<T>(key: string, fn: () => Promise<T>): Promise<T> {
  const prior = locks.get(key) ?? Promise.resolve();
  let release!: () => void;
  const current = new Promise<void>((resolve) => { release = resolve; });
  locks.set(key, prior.then(() => current));
  await prior;
  try {
    return await fn();
  } finally {
    release();
    if (locks.get(key) === current) locks.delete(key);
  }
}
