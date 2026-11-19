"use client";

import { type ReactNode } from "react";
import Forbidden from "@/components/Forbidden";
import LoadingState from "@/components/LoadingState";
import { useRbac } from "@/context/RbacContext";
import { checkPermission } from "@/shared/utils/checkPermission";

interface RequireActionProps {
  action?: string;
  anyOf?: readonly string[];
  allOf?: readonly string[];
  children: ReactNode;
  forbiddenFallback?: ReactNode;
}

export default function RequireAction({
  action,
  anyOf,
  allOf,
  children,
  forbiddenFallback = <Forbidden />,
}: RequireActionProps) {
  const { loading, can, canAny, canAll } = useRbac();

  if (loading) {
    return <LoadingState />;
  }

  const allowed = checkPermission(
    { action, anyOf, allOf },
    { can, canAny, canAll },
  );

  if (!allowed) {
    return <>{forbiddenFallback}</>;
  }

  return <>{children}</>;
}
