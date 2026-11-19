"use client";

import { useAdminQuery } from "@/shared/api/adminApi";
import { useCasePath, useCasesPath } from "./api";
import type { UseCaseDetail, UseCaseList } from "./types";

export function useUseCases() {
    return useAdminQuery<UseCaseList>(useCasesPath());
}

export function useUseCase(ref: string) {
    return useAdminQuery<UseCaseDetail>(useCasePath(ref));
}
