"use client";

import { usePathname, useRouter } from "next/navigation";

export default function SignOut() {
  const router = useRouter();
  if (usePathname() === "/login") return null;
  return (
    <button
      type="button"
      className="signout"
      onClick={async () => {
        await fetch("/api/session", { method: "DELETE" });
        router.replace("/login");
      }}
    >
      Sign out
    </button>
  );
}
