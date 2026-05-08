import { NextResponse } from "next/server";
import { getConsoleApiAuth } from "../../../../../lib/console-auth";
import { readJsonResponse } from "../../../../../lib/http-json";
import { serverEnv } from "../../../../../lib/server-env";

const API_BASE_URL =
  serverEnv("INTERNAL_API_BASE_URL") ?? serverEnv("NEXT_PUBLIC_API_BASE_URL") ?? "http://localhost:8000";

type RouteContext = {
  params: Promise<{ caseId: string }>;
};

export async function POST(request: Request, context: RouteContext) {
  const auth = await getConsoleApiAuth();
  const { caseId } = await context.params;
  const body = (await request.json()) as Record<string, unknown>;
  const fixtureMode = request.headers.get("x-console-fixture-mode") === "true";

  if (auth.token === null && fixtureMode) {
    return NextResponse.json({
      id: `local-correction-${caseId}`,
      case_id: caseId,
      merchant_id: "demo-merchant",
      expected_resolution: body.expected_resolution ?? {},
      notes: body.notes ?? "",
      created_by: "local-console",
      status: "queued",
      local_only: true,
    });
  }
  if (auth.token === null) {
    return NextResponse.json({ detail: auth.detail }, { status: auth.status });
  }

  const response = await fetch(`${API_BASE_URL}/v1/cases/${caseId}/corrections`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${auth.token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  const payload = await readJsonResponse(response);
  return NextResponse.json(payload, { status: response.status });
}
