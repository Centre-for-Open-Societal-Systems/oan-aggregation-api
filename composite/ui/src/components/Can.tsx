"use client";

import { useRbac } from "@/context/RbacContext";
import { checkPermission } from "@/shared/utils/checkPermission";
import type { ReactNode } from "react";

interface CanProps {
  action?: string;
  anyOf?: readonly string[];
  allOf?: readonly string[];
  children: ReactNode;
  fallback?: ReactNode;
}

export default function Can({
  action,
  anyOf,
  allOf,
  fallback = null,
  children,
}: CanProps) {
  const { loading, can, canAny, canAll } = useRbac();

  if (loading) return <>{fallback}</>;

  const allowed = checkPermission(
    { action, anyOf, allOf },
    { can, canAny, canAll },
  );

  return allowed ? <>{children}</> : <>{fallback}</>;
}
