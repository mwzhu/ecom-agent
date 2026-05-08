import { NextResponse } from "next/server";
import { getConsoleApiAuth } from "../../../../lib/console-auth";
import { readJsonResponse } from "../../../../lib/http-json";
import { serverEnv } from "../../../../lib/server-env";

const API_BASE_URL =
  serverEnv("INTERNAL_API_BASE_URL") ?? serverEnv("NEXT_PUBLIC_API_BASE_URL") ?? "http://localhost:8000";

type RouteContext = {
  params: Promise<{ provider: string }>;
};

export async function DELETE(_: Request, context: RouteContext) {
  const auth = await getConsoleApiAuth();
  const { provider } = await context.params;

  if (auth.token === null) {
    return NextResponse.json({ detail: auth.detail }, { status: auth.status });
  }

  const response = await fetch(`${API_BASE_URL}/v1/integrations/${provider}`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${auth.token}` },
  });
  const payload = await readJsonResponse(response);
  return NextResponse.json(payload, { status: response.status });
}
