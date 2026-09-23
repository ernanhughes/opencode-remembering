import { createHash } from "node:crypto";
import * as fs from "node:fs/promises";
import * as path from "node:path";

/** Port of engine/remembering/baseline/ingest.py. Behavior preserved. */

export const TEXT_SUFFIXES = new Set([
  ".md", ".markdown", ".txt", ".rst",
  ".py", ".js", ".ts", ".go", ".rs", ".java", ".sql",
  ".json", ".yaml", ".yml", ".toml", ".cfg", ".ini",
  ".log",
]);

const SKIP_DIRS = new Set([
  ".git", "node_modules", "__pycache__", ".venv", "venv",
  "dist", "build", "coverage", "target", ".next", ".turbo",
]);

const SKIP_PATHS: string[][] = [
  [".remembering", "loops"],
  [".remembering", "traces"],
  [".remembering", "memory"],
  [".remembering", "write-policy.json"],
];

export type ChunkingConfig = {
  policy: "fixed" | "section" | "sentence";
  targetChars: number;
  overlapChars: number;
  chunkerVersion: string;
};

export const DEFAULT_CHUNKING: ChunkingConfig = {
  policy: "sentence",
  targetChars: 2000,
  overlapChars: 300,
  chunkerVersion: "0.1.0",
};

export type Source = {
  sourceId: string;
  artifactType: string;
  content: string;
  contentHash: string;
  timestamp: string | null;
};

export type Chunk = {
  chunkId: string;
  sourceId: string;
  ordinal: number;
  text: string;
  charStart: number;
  charEnd: number;
  contentHash: string;
  section: string | null;
};

export function sha1(text: string): string {
  return createHash("sha1").update(text, "utf8").digest("hex");
}

export function artifactType(fileName: string, suffix: string): string {
  const name = fileName.toLowerCase();
  if (name.startsWith("adr-") || name.includes("decision")) return "decision-record";
  if (name.startsWith("session-")) return "session";
  if (name.startsWith("issue-")) return "issue";
  if (name === "commits.log" || suffix === ".log") return "commit-log";
  if (suffix === ".json") return "record";
  if ([".py", ".js", ".ts", ".go", ".rs", ".java", ".sql"].includes(suffix)) return "code";
  if ([".yaml", ".yml", ".toml", ".cfg", ".ini"].includes(suffix)) return "config";
  return "document";
}

async function walkFiles(root: string): Promise<string[]> {
  const found: string[] = [];
  async function walk(dir: string): Promise<void> {
    const entries = await fs.readdir(dir, { withFileTypes: true });
    entries.sort((a, b) => a.name.localeCompare(b.name));
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        if (SKIP_DIRS.has(entry.name)) continue;
        await walk(full);
      } else if (entry.isFile()) {
        found.push(full);
      }
    }
  }
  await walk(root);
  found.sort();
  return found;
}

export async function discover(root: string): Promise<string[]> {
  const all = await walkFiles(root);
  const out: string[] = [];
  for (const full of all) {
    const relative = path.relative(root, full).split(path.sep).join("/");
    const parts = relative.split("/");
    if (parts.some((p) => SKIP_DIRS.has(p))) continue;
    if (SKIP_PATHS.some((skip) => skip.every((seg, i) => parts[i] === seg))) continue;
    const suffix = path.extname(full).toLowerCase();
    if (TEXT_SUFFIXES.has(suffix)) out.push(full);
  }
  return out;
}

const DATE_RE = /^(?:date|effective)[:\s]+(\d{4}-\d{2}-\d{2})/m;

export async function parseFile(fullPath: string, root: string): Promise<Source | null> {
  let content: string;
  try {
    content = await fs.readFile(fullPath, "utf8");
  } catch {
    return null;
  }
  if (!content.trim()) return null;
  const relative = path.relative(root, fullPath).split(path.sep).join("/");
  const suffix = path.extname(fullPath).toLowerCase();
  const match = DATE_RE.exec(content.slice(0, 500));
  return {
    sourceId: relative,
    artifactType: artifactType(path.basename(fullPath), suffix),
    content,
    contentHash: sha1(content),
    timestamp: match?.[1] ?? null,
  };
}

const SENTENCE_RE = /(?<=[.!?])\s+(?=[A-Z0-9#\-*`])/g;
const HEADING_RE = /^(#{1,4}\s+.+)$/gm;

function splitSentences(text: string): string[] {
  return text.split(SENTENCE_RE).map((p) => p.trim()).filter(Boolean);
}

export function chunkFixed(text: string, target: number, overlap: number): Array<[number, number]> {
  const spans: Array<[number, number]> = [];
  let start = 0;
  const size = text.length;
  while (start < size) {
    const end = Math.min(start + target, size);
    spans.push([start, end]);
    if (end === size) break;
    start = Math.max(end - overlap, start + 1);
  }
  return spans;
}

export function chunkSection(text: string): Array<[number, number, string | null]> {
  const matches = [...text.matchAll(HEADING_RE)];
  if (matches.length === 0) return [[0, text.length, null]];
  const spans: Array<[number, number, string | null]> = [];
  const first = matches[0];
  if (first && (first.index ?? 0) > 0) spans.push([0, first.index ?? 0, null]);
  for (let i = 0; i < matches.length; i++) {
    const current = matches[i];
    if (!current) continue;
    const start = current.index ?? 0;
    const next = i + 1 < matches.length ? matches[i + 1] : undefined;
    const end = next?.index ?? text.length;
    spans.push([start, end, (current[1] ?? "").trim().slice(0, 120)]);
  }
  return spans;
}

export function chunkSentenceAware(text: string, target: number, overlap: number): Array<[number, number]> {
  const sentences = splitSentences(text);
  if (sentences.length === 0) return [];
  const offsets: Array<[number, number]> = [];
  let cursor = 0;
  for (const sentence of sentences) {
    let at = text.indexOf(sentence, cursor);
    if (at < 0) at = cursor;
    offsets.push([at, at + sentence.length]);
    cursor = at + sentence.length;
  }
  const spans: Array<[number, number]> = [];
  let i = 0;
  while (i < sentences.length) {
    const startTuple = offsets[i];
    const endTuple = offsets[i];
    if (!startTuple || !endTuple) break;
    const start = startTuple[0];
    let end = endTuple[1];
    let j = i;
    while (j + 1 < sentences.length && (offsets[j + 1]?.[1] ?? 0) - start < target) {
      j += 1;
      end = offsets[j]?.[1] ?? end;
    }
    spans.push([start, end]);
    if (j + 1 >= sentences.length) break;
    let k = j;
    while (k > i && end - (offsets[k]?.[0] ?? 0) < overlap) k -= 1;
    if (k < j) i = Math.max(k, i + 1);
    else i = j + 1;
    if (i <= j && k === j) i = j + 1;
  }
  return spans;
}

export function chunkSource(source: Source, cfg: ChunkingConfig): Chunk[] {
  let spans: Array<[number, number]>;
  let sections: Array<string | null>;
  if (cfg.policy === "section") {
    const raw = chunkSection(source.content);
    spans = raw.map(([s, e]) => [s, e] as [number, number]);
    sections = raw.map(([, , section]) => section);
  } else if (cfg.policy === "sentence") {
    spans = chunkSentenceAware(source.content, cfg.targetChars, cfg.overlapChars);
    sections = new Array(spans.length).fill(null);
  } else {
    spans = chunkFixed(source.content, cfg.targetChars, cfg.overlapChars);
    sections = new Array(spans.length).fill(null);
  }
  const chunks: Chunk[] = [];
  spans.forEach(([start, end], ordinal) => {
    const text = source.content.slice(start, end).trim();
    if (!text) return;
    const chunkId = sha1(
      `${source.sourceId}|${cfg.policy}|${cfg.chunkerVersion}|${ordinal}|${sha1(text)}`,
    ).slice(0, 16);
    chunks.push({
      chunkId,
      sourceId: source.sourceId,
      ordinal,
      text,
      charStart: start,
      charEnd: end,
      contentHash: sha1(text),
      section: sections[ordinal] ?? null,
    });
  });
  return chunks;
}
