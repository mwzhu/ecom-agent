import { NextResponse } from "next/server";
import { loadAdminConsoleData } from "../../../lib/admin-data";

export async function GET() {
  const data = await loadAdminConsoleData();
  return NextResponse.json(data);
}
