import { NextResponse } from "next/server";

const API_BASE_URL =
  process.env.INTERNAL_API_BASE_URL ?? process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

type RouteContext = {
  params: Promise<{ caseId: string }>;
};

export async function POST(request: Request, context: RouteContext) {
  const token = process.env.INTERNAL_CONSOLE_BEARER_TOKEN;
  const { caseId } = await context.params;
  const body = (await request.json()) as Record<string, unknown>;
  const fixtureMode = request.headers.get("x-console-fixture-mode") === "true";

  if (!token && fixtureMode) {
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
  if (!token) {
    return NextResponse.json(
      { detail: "INTERNAL_CONSOLE_BEARER_TOKEN is required outside fixture mode." },
      { status: 503 },
    );
  }

  const response = await fetch(`${API_BASE_URL}/v1/cases/${caseId}/corrections`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  const payload = (await response.json()) as unknown;
  return NextResponse.json(payload, { status: response.status });
}
