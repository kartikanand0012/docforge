import { demoSignIn } from "@/lib/demo";
import { LoginPage } from "./form";

// Read at request time: the same image serves the public demo and real deployments.
export const dynamic = "force-dynamic";

export default function Page() {
  return <LoginPage demo={demoSignIn(process.env)} />;
}
