"use client";

import { useAdminQuery } from "@/shared/api/adminApi";
import { dataScopesPath, registriesPath } from "./api";
import type { RegistryDataScopes, RegistryList } from "./types";

export function useRegistries() {
    return useAdminQuery<RegistryList>(registriesPath());
}

export function useDataScopes(id: string) {
    return useAdminQuery<RegistryDataScopes>(dataScopesPath(id));
}
