import * as crypto from "node:crypto";
import * as fs from "node:fs/promises";
import * as path from "node:path";

/** Stable project identity without breaking existing isolation. */

export type ProjectIdentity = {
  /** Explicit configured ID wins when present. */
  projectId: string;
  /** Canonical local path (realpath, lowercased on win32). */
  canonicalPath: string;
  /** Derived postgres schema (existing semantics preserved). */
  schema: string;
  /** How the identity was derived. */
  source: "explicit" | "git-remote" | "local-path";
  /** Git remote slug when available, e.g. github:owner/repo. */
  gitSlug: string | null;
};

export async function canonicalProjectDir(directory: string): Promise<string> {
  let resolved: string;
  try {
    resolved = await fs.realpath(directory);
  } catch {
    resolved = path.resolve(directory);
  }
  return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}

export async function schemaForProjectDir(directory: string): Promise<string> {
  const canonical = await canonicalProjectDir(directory);
  const digest = crypto.createHash("sha256").update(canonical).digest("hex").slice(0, 12);
  return `remembering_${digest}`;
}

async function gitSlugForDir(directory: string): Promise<string | null> {
  try {
    const raw = await fs.readFile(path.join(directory, ".git", "config"), "utf8").catch(() => null);
    if (raw) {
      const m = /url\s*=\s*(.+)/g;
      let match: RegExpExecArray | null;
      while ((match = m.exec(raw))) {
        const url = (match[1] ?? "").trim();
        const gh = url.match(/github\.com[:/]([^/]+\/[^/.]+?)(?:\.git)?\s*$/i);
        if (gh?.[1]) return `github:${gh[1]}`;
      }
      return null;
    }
  } catch { /* ignore */ }
  return null;
}

export async function resolveProjectIdentity(
  directory: string,
  opts: { explicitId?: string; schemaOverride?: string } = {},
): Promise<ProjectIdentity> {
  const canonicalPath = await canonicalProjectDir(directory);
  if (opts.explicitId?.trim()) {
    const projectId = opts.explicitId.trim();
    const schema = opts.schemaOverride ?? (await schemaForProjectDir(directory));
    return { projectId, canonicalPath, schema, source: "explicit", gitSlug: await gitSlugForDir(directory) };
  }
  const gitSlug = await gitSlugForDir(directory);
  // Preserve current isolation: schema still derives from the canonical path
  // so existing installs never merge. The git slug is advisory identity only.
  const schema = opts.schemaOverride ?? (await schemaForProjectDir(directory));
  const projectId = gitSlug ? `${gitSlug}#${schema}` : `local:${schema}`;
  return { projectId, canonicalPath, schema, source: gitSlug ? "git-remote" : "local-path", gitSlug };
}
