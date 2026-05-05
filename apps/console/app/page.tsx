import { AdminConsole } from "./admin-console";
import { loadAdminConsoleData } from "../lib/admin-data";

export const dynamic = "force-dynamic";

export default async function Home() {
  const data = await loadAdminConsoleData();
  return <AdminConsole initialData={data} />;
}
