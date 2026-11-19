/** One item of GET /composite/v1/admin/registries. */
export interface Registry {
    id: string;
    url: string;
    catalogue_url: string;
    partner_id: string;
    receiver_id: string;
    /** null: not checked yet; false: see `error`. */
    reachable: boolean | null;
    /** null when the registry was not reachable. */
    scope_count: number | null;
    error: string | null;
    used_by: string[];
}

export interface RegistryList {
    registries: Registry[];
}

export interface ScopeUsage {
    use_case: string;
    source: string;
    required: boolean;
}

export interface DataScope {
    scope_id: string;
    name: string;
    label: string;
    description: string;
    status: string;
    current_version: number | null;
    fields: string[];
    used_by: ScopeUsage[];
}

/** GET /composite/v1/admin/registries/{id}/data-scopes */
export interface RegistryDataScopes {
    registry: string;
    data_controller: string;
    fetched_at: string | null;
    error: string | null;
    data_scopes: DataScope[];
}
