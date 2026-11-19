import { toQuery } from "@/shared/api/adminApi";
import type { ActivityFilters } from "./types";

/** Backend maximum for `limit`. */
export const MAX_PAGE_SIZE = 200;

export function activityPath(filters: ActivityFilters, limit: number, offset: number): string {
    return `activity${toQuery({ ...filters, limit, offset })}`;
}
