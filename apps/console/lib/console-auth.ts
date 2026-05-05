import { auth } from "@clerk/nextjs/server";
import { serverEnv } from "./server-env";

type ConsoleApiAuth =
  | {
      token: string;
      source: "clerk" | "static";
    }
  | {
      token: null;
      detail: string;
      status: 401 | 503;
    };

export async function getConsoleApiAuth(): Promise<ConsoleApiAuth> {
  const clerkToken = await getClerkToken();
  if (clerkToken.token) {
    return { token: clerkToken.token, source: "clerk" };
  }

  const staticToken = serverEnv("INTERNAL_CONSOLE_BEARER_TOKEN");
  if (staticToken && staticTokenAllowed()) {
    return { token: staticToken, source: "static" };
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
