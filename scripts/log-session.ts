#!/usr/bin/env ts-node
/**
 * Writes a governed Markdown session log to docs/sessions/ and commits it.
 *
 * Required (CLI args or env vars):
 *   SESSION_PR_NUMBER  / argv[2]
 *   SESSION_PR_TITLE   / argv[3]
 *   SESSION_PR_URL     / argv[4]
 *
 * Optional env vars:
 *   SESSION_DECISIONS    — newline-separated decisions
 *   SESSION_ARTIFACTS    — comma-separated artifact names
 *   SESSION_FINDINGS     — newline-separated findings
 *   SESSION_NEXT_ACTIONS — newline-separated next actions
 */

import { execSync } from "child_process";
import { writeFileSync, mkdirSync } from "fs";
import { join } from "path";
import * as crypto from "crypto";

// ---------------------------------------------------------------------------
// UUID v4 (no external deps)
// ---------------------------------------------------------------------------
function uuidv4(): string {
  const bytes = crypto.randomBytes(16);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = bytes.toString("hex");
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20, 32),
  ].join("-");
}

// ---------------------------------------------------------------------------
// Git helpers
// ---------------------------------------------------------------------------
function git(cmd: string): string {
  return execSync(cmd, { encoding: "utf8" }).trim();
}

// ---------------------------------------------------------------------------
// Slug helper — lowercase, hyphens only, max 40 chars
// ---------------------------------------------------------------------------
function slugify(s: string): string {
  return s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 40);
}

// ---------------------------------------------------------------------------
// Section builder — returns body text or a fallback
// ---------------------------------------------------------------------------
function lines(raw: string | undefined, fallback: string): string {
  if (!raw || !raw.trim()) return fallback;
  return raw
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean)
    .join("\n");
}

function csvList(raw: string | undefined, fallback: string): string {
  if (!raw || !raw.trim()) return fallback;
  return raw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .join(", ");
}

// ---------------------------------------------------------------------------
// Warn helper
// ---------------------------------------------------------------------------
function warn(msg: string): void {
  process.stderr.write(`[log-session] WARNING: ${msg}\n`);
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
function main(): void {
  const argv = process.argv.slice(2);

  // Required fields — CLI args take precedence over env vars
  const prNumber: string =
    argv[0] || process.env.SESSION_PR_NUMBER || "";
  const prTitle: string =
    argv[1] || process.env.SESSION_PR_TITLE || "";
  const prUrl: string =
    argv[2] || process.env.SESSION_PR_URL || "";

  let missingFields = false;

  if (!prNumber) {
    warn("SESSION_PR_NUMBER is missing — using placeholder");
    missingFields = true;
  }
  if (!prTitle) {
    warn("SESSION_PR_TITLE is missing — using placeholder");
    missingFields = true;
  }
  if (!prUrl) {
    warn("SESSION_PR_URL is missing — using placeholder");
    missingFields = true;
  }

  const sessionId = uuidv4();
  const now = new Date();
  const dateStr = now.toISOString().slice(0, 10); // YYYY-MM-DD
  const isoDate = now.toISOString();

  let branch: string;
  let commitSha: string;
  try {
    branch = git("git rev-parse --abbrev-ref HEAD");
    commitSha = git("git rev-parse HEAD");
  } catch (e) {
    warn("Could not determine git branch/commit. Falling back to placeholders.");
    branch = "unknown-branch";
    commitSha = "unknown-sha";
  }

  const displayPrNumber = prNumber || "000";
  const displayPrTitle = prTitle || "(no title captured)";
  const displayPrUrl = prUrl || "(no URL captured)";

  const slug = slugify(branch);
  const filename = `${dateStr}-pr-${displayPrNumber}-${slug}.md`;
  const repoRoot = git("git rev-parse --show-toplevel");
  const outDir = join(repoRoot, "docs", "sessions");
  mkdirSync(outDir, { recursive: true });
  const outPath = join(outDir, filename);

  // Optional sections
  const decisions = lines(
    process.env.SESSION_DECISIONS,
    "No decisions captured."
  );
  const artifacts = csvList(
    process.env.SESSION_ARTIFACTS,
    "No artifacts captured."
  );
  const findings = lines(
    process.env.SESSION_FINDINGS,
    "No findings captured."
  );
  const nextActions = lines(
    process.env.SESSION_NEXT_ACTIONS,
    "No next actions captured."
  );

  const content = `---
session_id: ${sessionId}
date: ${isoDate}
pr_number: ${displayPrNumber}
pr_url: ${displayPrUrl}
pr_title: ${displayPrTitle}
branch: ${branch}
commit_sha: ${commitSha}
---

## Decisions Made
${decisions}

## Artifacts Produced
${artifacts}

## Findings
${findings}

## Next Actions
${nextActions}
`;

  writeFileSync(outPath, content, "utf8");
  process.stdout.write(`[log-session] Written: ${outPath}\n`);

  // Commit and push
  const relPath = `docs/sessions/${filename}`;
  const commitMsg = `chore: add session log for PR #${displayPrNumber}`;

  try {
    execSync(`git add "${relPath}"`, { cwd: repoRoot, stdio: "inherit" });
    execSync(`git commit -m "${commitMsg}"`, {
      cwd: repoRoot,
      stdio: "inherit",
    });
    execSync(`git push -u origin "${branch}"`, {
      cwd: repoRoot,
      stdio: "inherit",
    });
    process.stdout.write(`[log-session] Committed and pushed: ${commitMsg}\n`);
  } catch (e) {
    warn(`git commit/push failed: ${(e as Error).message}`);
    process.exit(1);
  }

  if (missingFields) {
    process.exit(0); // file written, but note the warnings
  }
}

main();
