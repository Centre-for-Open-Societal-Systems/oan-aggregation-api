"use client";

import { useAdminQuery } from "@/shared/api/adminApi";
import { overviewPath } from "./api";
import type { Overview } from "./types";

export function useOverview() {
    return useAdminQuery<Overview>(overviewPath());
}
