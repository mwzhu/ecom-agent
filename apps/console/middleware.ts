import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";

const isPublicRoute = createRouteMatcher(["/sign-in(.*)", "/sign-up(.*)"]);

export default clerkMiddleware(async (auth, request) => {
  if (requiresClerkAuth() && !isPublicRoute(request)) {
    await auth.protect({ unauthenticatedUrl: "/sign-in" });
  }
});

export const config = {
  matcher: [
    "/((?!_next|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico|csv|docx?|xlsx?|zip|webmanifest)).*)",
    "/(api|trpc)(.*)",
  ],
};

function requiresClerkAuth(): boolean {
  return (
    process.env.ENVIRONMENT === "production" ||
    process.env.VERCEL_ENV === "production" ||
    process.env.CONSOLE_REQUIRE_CLERK_AUTH === "true"
  );
}
