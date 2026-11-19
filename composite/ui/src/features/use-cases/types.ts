export interface UseCaseParameter {
    type?: string;
    required?: boolean;
    default?: unknown;
    description?: string;
    [key: string]: unknown;
}

export interface UseCaseSource {
    id: string;
    /** Registry (data controller) the source reads from. */
    controller: string;
    requirement: "mandatory" | "optional" | string;
    depends_on: string[];
    scopes: string[];
    optional_scopes: string[];
}

export interface ConsentScopes {
    required: string[];
    optional: string[];
}

/** One item of GET /composite/v1/admin/use-cases. */
export interface UseCase {
    use_case: string;
    name: string;
    version: string;
    status: string;
    title: string;
    description: string;
    purpose: string;
    consent: { required: boolean };
    input: {
        subject?: { id_types?: string[] };
        parameters?: Record<string, UseCaseParameter>;
        batch?: Record<string, unknown>;
    };
    sources: UseCaseSource[];
    consent_grants_needed: string[];
    consent_scopes: Record<string, ConsentScopes>;
    output_fields: string[];
    partial_response: string;
    rate_per_partner: string | null;
    /** Partner Management partner IDs whose use-case policy grants this use case; null when PM could not be read. */
    granted_to: string[] | null;
    /** Why `granted_to` is null (Partner Management could not be read). */
    granted_to_error: string | null;
    file: string;
}

export interface UseCaseLoadError {
    file: string;
    error: string;
}

export interface UseCaseList {
    use_cases: UseCase[];
    errors: UseCaseLoadError[];
}

/** GET /composite/v1/admin/use-cases/{ref} */
export interface UseCaseDetail extends UseCase {
    mapping?: Record<string, string>;
    derived?: Record<string, string>;
    source_yaml?: string;
}
