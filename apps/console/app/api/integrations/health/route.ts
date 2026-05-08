import { NextResponse } from "next/server";
import { getConsoleApiAuth } from "../../../../lib/console-auth";
import { readJsonResponse } from "../../../../lib/http-json";
import { serverEnv } from "../../../../lib/server-env";

const API_BASE_URL =
  serverEnv("INTERNAL_API_BASE_URL") ?? serverEnv("NEXT_PUBLIC_API_BASE_URL") ?? "http://localhost:8000";

export async function GET() {
  const auth = await getConsoleApiAuth();
  if (auth.token === null) {
    return NextResponse.json({ detail: auth.detail }, { status: auth.status });
  }

  const response = await fetch(`${API_BASE_URL}/v1/integrations/health`, {
    headers: { Authorization: `Bearer ${auth.token}` },
    cache: "no-store",
  });
  const payload = await readJsonResponse(response);
  return NextResponse.json(payload, { status: response.status });
}
