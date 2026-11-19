"use client";

import { useAdminQuery } from "@/shared/api/adminApi";
import { partnersPath } from "./api";
import type { PartnerList } from "./types";

export function usePartners() {
    return useAdminQuery<PartnerList>(partnersPath());
}
