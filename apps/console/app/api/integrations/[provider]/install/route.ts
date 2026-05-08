import { NextResponse } from "next/server";
import { getConsoleApiAuth } from "../../../../../lib/console-auth";
import { readJsonResponse } from "../../../../../lib/http-json";
import { serverEnv } from "../../../../../lib/server-env";

const API_BASE_URL =
  serverEnv("INTERNAL_API_BASE_URL") ?? serverEnv("NEXT_PUBLIC_API_BASE_URL") ?? "http://localhost:8000";

type RouteContext = {
  params: Promise<{ provider: string }>;
};

type InstallBody = {
  shop?: string;
  account?: string;
  access_token?: string;
  refresh_token?: string | null;
  metadata?: Record<string, unknown>;
};

export async function POST(request: Request, context: RouteContext) {
  const auth = await getConsoleApiAuth();
  const { provider } = await context.params;
  const body = (await request.json()) as InstallBody;

  if (auth.token === null) {
    return NextResponse.json({ detail: auth.detail }, { status: auth.status });
  }

  if (provider === "shopify") {
    return installFromUrl(`/v1/integrations/shopify/install?shop=${encodeURIComponent(body.shop ?? "")}`, auth.token);
  }
  if (provider === "stripe") {
    return installFromUrl("/v1/integrations/stripe/connect/install", auth.token);
  }
  if (provider === "gorgias") {
    return installFromRedirect(
      `/v1/integrations/gorgias/install?account=${encodeURIComponent(body.account ?? "")}`,
      auth.token,
    );
  }

  const response = await fetch(`${API_BASE_URL}/v1/integrations/${provider}/install`, {
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

async function installFromUrl(pathname: string, token: string) {
  const response = await fetch(`${API_BASE_URL}${pathname}`, {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
  });
  const payload = await readJsonResponse(response);
  return NextResponse.json(payload, { status: response.status });
}

async function installFromRedirect(pathname: string, token: string) {
  const response = await fetch(`${API_BASE_URL}${pathname}`, {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
    redirect: "manual",
  });
  const location = response.headers.get("location");
  if (location) {
    return NextResponse.json({ install_url: location }, { status: 200 });
  }
  const payload = await readJsonResponse(response);
  return NextResponse.json(payload, { status: response.status });
}
