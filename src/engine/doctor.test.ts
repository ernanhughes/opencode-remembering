import { describe, expect, test } from "bun:test";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import * as path from "node:path";

import { redactDsn } from "./errors";
import { computeBaselineReadiness, computeProductReadiness } from "./readiness";

const INTEGRATION = process.env.REMEMBERING_INTEGRATION_TESTS === "1";
const DSN = process.env.MEMORY_BASELINE_DSN ?? "postgresql://postgres:postgres@localhost:5432/memory_baseline";

function healthyBaseline() {
  return {
    postgresReachable: true,
    databaseExists: true,
    pgvectorAvailable: true,
    pgTrgmAvailable: true,
    schemaInitialized: true,
    schemaIdentityOk: true,
    embeddingProviderReachable: true,
    embeddingModelAvailable: true,
    embeddingDimensionCompatible: true as boolean | null,
  };
}

describe("baseline readiness calculation", () => {
  test("healthy baseline reports ok=true", () => {
    const readiness = computeBaselineReadiness(healthyBaseline());
    expect(readiness.ok).toBe(true);
    expect(readiness.code).toBe("HEALTHY");
    expect(readiness.reasons).toEqual([]);
  });

  test("absent schema is NOT_INITIALIZED, not a crash", () => {
    const readiness = computeBaselineReadiness({ ...healthyBaseline(), schemaInitialized: false });
    expect(readiness.ok).toBe(false);
    expect(readiness.code).toBe("NOT_INITIALIZED");
  });

  test("null dimension verdict does not fail a fresh schema", () => {
    const readiness = computeBaselineReadiness({
      ...healthyBaseline(),
      schemaInitialized: false,
      embeddingDimensionCompatible: null,
    });
    expect(readiness.code).toBe("NOT_INITIALIZED");
  });

  test("null dimension verdict does not fail an empty initialized store", () => {
    const readiness = computeBaselineReadiness({ ...healthyBaseline(), embeddingDimensionCompatible: null });
    expect(readiness.ok).toBe(true);
  });

  test("unreachable postgres is BACKEND_UNREACHABLE", () => {
    const readiness = computeBaselineReadiness({ ...healthyBaseline(), postgresReachable: false, databaseExists: false });
    expect(readiness.ok).toBe(false);
    expect(readiness.code).toBe("BACKEND_UNREACHABLE");
  });

  test("missing pgvector or pg_trgm is EXTENSION_MISSING", () => {
    expect(computeBaselineReadiness({ ...healthyBaseline(), pgvectorAvailable: false }).code).toBe("EXTENSION_MISSING");
    expect(computeBaselineReadiness({ ...healthyBaseline(), pgTrgmAvailable: false }).code).toBe("EXTENSION_MISSING");
  });

  test("unreachable embedding is EMBEDDING_UNAVAILABLE", () => {
    const readiness = computeBaselineReadiness({ ...healthyBaseline(), embeddingProviderReachable: false, embeddingModelAvailable: false });
    expect(readiness.ok).toBe(false);
    expect(readiness.code).toBe("EMBEDDING_UNAVAILABLE");
  });

  test("dimension mismatch stays fail-closed", () => {
    const readiness = computeBaselineReadiness({ ...healthyBaseline(), embeddingDimensionCompatible: false });
    expect(readiness.ok).toBe(false);
    expect(readiness.code).toBe("DIMENSION_MISMATCH");
  });

  test("schema identity mismatch stays fail-closed", () => {
    const readiness = computeBaselineReadiness({ ...healthyBaseline(), schemaIdentityOk: false });
    expect(readiness.ok).toBe(false);
    expect(readiness.code).toBe("SCHEMA_MISMATCH");
  });
});

describe("product readiness calculation", () => {
  const subsystems = {
    temporalReady: true,
    standingReady: true,
    trustPolicyValid: true,
    traceReady: true,
    loopsReady: true,
    writesReady: true,
    writePolicyValid: true,
  };
  test("healthy baseline plus ready subsystems is healthy", () => {
    const readiness = computeProductReadiness({ ok: true, code: "HEALTHY", reasons: [] }, subsystems);
    expect(readiness).toEqual({ ok: true, code: "HEALTHY", reasons: [] });
  });

  test("baseline failure propagates unchanged", () => {
    const baseline = computeBaselineReadiness({ ...healthyBaseline(), schemaInitialized: false });
    expect(computeProductReadiness(baseline, subsystems)).toEqual(baseline);
  });

  test("invalid trust or write policy is POLICY_INVALID", () => {
    expect(
      computeProductReadiness({ ok: true, code: "HEALTHY", reasons: [] }, { ...subsystems, trustPolicyValid: false, trustPolicyError: "bad json" }).code,
    ).toBe("POLICY_INVALID");
    expect(
      computeProductReadiness({ ok: true, code: "HEALTHY", reasons: [] }, { ...subsystems, writePolicyValid: false }).code,
    ).toBe("POLICY_INVALID");
  });

  test("unready stores are SUBSYSTEM_FAILURE", () => {
    const readiness = computeProductReadiness(
      { ok: true, code: "HEALTHY", reasons: [] },
      { ...subsystems, traceReady: false },
    );
    expect(readiness.ok).toBe(false);
    expect(readiness.code).toBe("SUBSYSTEM_FAILURE");
  });
});

describe("dsn redaction", () => {
  test("password redacted, user and endpoint visible", () => {
    expect(redactDsn("postgresql://postgres:s3cret@localhost:5432/memory_baseline")).toBe(
      "postgresql://postgres:***@localhost:5432/memory_baseline",
    );
  });

  test("no credentials left intact", () => {
    expect(redactDsn("postgresql://localhost:5432/memory_baseline")).toBe(
      "postgresql://localhost:5432/memory_baseline",
    );
  });
});

describe("live doctor integration", () => {
  test.skipIf(!INTEGRATION)("absent schema returns NOT_INITIALIZED without throwing", async () => {
    const { connectPool, withClient } = await import("./db");
    const { doctorBaseline } = await import("./doctor");
    const pool = await connectPool(DSN);
    try {
      const schema = `remembering_doctor_test_${Date.now().toString(36)}`;
      const report = await doctorBaseline(pool, {
        dsn: DSN,
        schema,
        projectDirectory: tmpdir(),
        embedding: { provider: "hashing", model: "hashing-64", host: "", dimension: 64 },
      });
      expect(report["ok"]).toBe(false);
      expect((report["readiness"] as { code: string }).code).toBe("NOT_INITIALIZED");
      await withClient(pool, async (client) => {
        await client.query(`DROP SCHEMA IF EXISTS "${schema}" CASCADE`);
      });
    } finally {
      await pool.end();
    }
  });

  test.skipIf(!INTEGRATION)("unreachable postgres preserves the coded failure", async () => {
    const { connectPool } = await import("./db");
    await expect(connectPool("postgresql://postgres:postgres@localhost:55999/nope")).rejects.toThrow(/DB_UNREACHABLE/);
  });

  test.skipIf(!INTEGRATION)("fresh native setup reports whole-product healthy", async () => {
    process.env.REMEMBERING_ALLOW_TEST_EMBEDDINGS = "1";
    const { RememberingEngine } = await import("./pipeline");
    const dir = await mkdtemp(path.join(tmpdir(), "remembering-doctor-"));
    try {
      await writeFile(path.join(dir, "notes.md"), "# Notes\nThe auth design uses OAuth2.\n");
      const engine = new RememberingEngine({
        dsn: DSN,
        schema: `remembering_doctor_e2e_${Date.now().toString(36)}`,
        projectDirectory: dir,
        embedding: { provider: "hashing", model: "hashing-64", host: "", dimension: 64 },
        retrieval: { mode: "hybrid", lexicalK: 5, denseK: 5, fusionK: 60, rerankK: 5, reranker: "none" },
      });
      try {
        await engine.setup();
        const report = await engine.nativeDoctor();
        expect(report["ok"]).toBe(true);
        expect((report["readiness"] as { code: string }).code).toBe("HEALTHY");
      } finally {
        await engine.close();
      }
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  }, 120_000);
});
