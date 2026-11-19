"use client";

import { useAdminQuery } from "@/shared/api/adminApi";
import { activityPath } from "./api";
import type { ActivityFilters, ActivityPage } from "./types";

export function useActivity(filters: ActivityFilters, limit: number, offset: number) {
    return useAdminQuery<ActivityPage>(activityPath(filters, limit, offset));
}
