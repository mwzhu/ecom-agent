import { clerkMiddleware } from "@clerk/nextjs/server";

export default clerkMiddleware(async (auth) => {
  if (requiresClerkAuth()) {
    await auth.protect();
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
