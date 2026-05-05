import { readFileSync } from "node:fs";
import path from "node:path";

const rootEnv = loadEnvFile();

export function serverEnv(name: string, fallback?: string): string | undefined {
  const value = process.env[name] ?? rootEnv[name] ?? fallback;
  return value && value.length > 0 ? value : undefined;
}

function loadEnvFile(): Record<string, string> {
  for (const filePath of envCandidates()) {
    try {
      return parseEnv(readFileSync(filePath, "utf8"));
    } catch {
      // Try the next likely cwd shape.
    }
  }
  return {};
}

function envCandidates(): string[] {
  return [
    path.resolve(process.cwd(), ".env"),
    path.resolve(process.cwd(), "..", "..", ".env"),
  ];
}

function parseEnv(contents: string): Record<string, string> {
  const values: Record<string, string> = {};
  for (const rawLine of contents.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) {
      continue;
    }
    const separator = line.indexOf("=");
    if (separator === -1) {
      continue;
    }
    const key = line.slice(0, separator).trim();
    const value = line.slice(separator + 1).trim();
    if (!key) {
      continue;
    }
    values[key] = unquote(value);
  }
  return values;
}

function unquote(value: string): string {
  if (
    (value.startsWith('"') && value.endsWith('"')) ||
    (value.startsWith("'") && value.endsWith("'"))
  ) {
    return value.slice(1, -1);
  }
  return value;
}
