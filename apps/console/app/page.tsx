import { AdminConsole } from "./admin-console";
import { loadAdminConsoleData } from "../lib/admin-data";

export default async function Home() {
  const data = await loadAdminConsoleData();
  return <AdminConsole initialData={data} />;
}
