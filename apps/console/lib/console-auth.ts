import { createHmac } from "node:crypto";
import { auth } from "@clerk/nextjs/server";
import { serverEnv } from "./server-env";

type ConsoleApiAuth =
  | {
      token: string;
      source: "clerk" | "static" | "generated-dev";
    }
  | {
      token: null;
      detail: string;
      status: 401 | 503;
    };

export async function getConsoleApiAuth(): Promise<ConsoleApiAuth> {
  const staticToken = serverEnv("INTERNAL_CONSOLE_BEARER_TOKEN");
  const devToken = generatedDevToken();
  if (devToken) {
    return { token: devToken, source: "generated-dev" };
  }

  if (staticToken && staticTokenAllowed()) {
    return { token: staticToken, source: "static" };
  }

  const clerkToken = await getClerkToken();
  if (clerkToken.token) {
    return { token: clerkToken.token, source: "clerk" };
  }

  if (staticToken && !staticTokenAllowed()) {
    return {
      token: null,
      detail: "Clerk sign-in is required in production; static console tokens are ignored.",
      status: 401,
    };
  }

  if (productionLike()) {
    return {
      token: null,
      detail: clerkToken.detail ?? "Clerk sign-in is required for the production console.",
      status: clerkConfigured() ? 401 : 503,
    };
  }

  return {
    token: null,
    detail:
      clerkToken.detail ??
      "A Clerk session or INTERNAL_CONSOLE_BEARER_TOKEN is required for API mode.",
    status: clerkConfigured() ? 401 : 503,
  };
}

export function clerkProviderEnabled(): boolean {
  return clerkConfigured();
}

function staticTokenAllowed(): boolean {
  return !productionLike();
}

function productionLike(): boolean {
  return (
    serverEnv("ENVIRONMENT") === "production" ||
    serverEnv("VERCEL_ENV") === "production" ||
    serverEnv("CONSOLE_REQUIRE_CLERK_AUTH") === "true"
  );
}

function generatedDevToken(): string | null {
  if (productionLike() || serverEnv("CLERK_ALLOW_UNVERIFIED_JWT") !== "true") {
    return null;
  }
  const secret = serverEnv("CLERK_DEV_JWT_SECRET");
  if (!secret) {
    return null;
  }
  const now = Math.floor(Date.now() / 1000);
  return signHs256Jwt(
    {
      sub: serverEnv("CONSOLE_LOCAL_USER_ID") ?? "user_local_console",
      org_id: serverEnv("CONSOLE_LOCAL_ORG_ID") ?? "org_local_demo",
      email: serverEnv("CONSOLE_LOCAL_EMAIL") ?? "ops@example.com",
      iat: now,
      exp: now + 60 * 60,
    },
    secret,
  );
}

function signHs256Jwt(payload: Record<string, string | number>, secret: string): string {
  const encodedHeader = base64Url(JSON.stringify({ alg: "HS256", typ: "JWT" }));
  const encodedPayload = base64Url(JSON.stringify(payload));
  const signingInput = `${encodedHeader}.${encodedPayload}`;
  const signature = createHmac("sha256", secret).update(signingInput).digest("base64url");
  return `${signingInput}.${signature}`;
}

function base64Url(value: string): string {
  return Buffer.from(value).toString("base64url");
}

async function getClerkToken(): Promise<{ token: string | null; detail?: string }> {
  if (!clerkConfigured()) {
    return { token: null };
  }

  try {
    const { getToken, isAuthenticated } = await auth();
    if (!isAuthenticated) {
      return { token: null, detail: "No signed-in Clerk session was found." };
    }
    const token = await getToken();
    if (!token) {
      return {
        token: null,
        detail: "The Clerk session did not provide a bearer token.",
      };
    }
    return { token };
  } catch (error) {
    return {
      token: null,
      detail: error instanceof Error ? error.message : "Clerk session lookup failed.",
    };
  }
}

function clerkConfigured(): boolean {
  return realEnvValue("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY") && realEnvValue("CLERK_SECRET_KEY");
}

function realEnvValue(name: string): boolean {
  const value = serverEnv(name);
  return Boolean(value && !value.endsWith("...") && !value.includes("<"));
}
