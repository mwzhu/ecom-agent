import { NextResponse } from "next/server";
import { getConsoleApiAuth } from "../../../../../lib/console-auth";
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
      case_id: caseId,
      status:
        body.decision === "reject"
          ? "canceled"
          : body.decision === "modify"
            ? "pending_approval"
            : "executing",
      langgraph_run_id: null,
      submitted_to_langgraph: false,
      local_only: true,
    });
  }
  if (auth.token === null) {
    return NextResponse.json({ detail: auth.detail }, { status: auth.status });
  }

  const response = await fetch(`${API_BASE_URL}/v1/cases/${caseId}/decision`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${auth.token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  const payload = (await response.json()) as unknown;
  return NextResponse.json(payload, { status: response.status });
}
