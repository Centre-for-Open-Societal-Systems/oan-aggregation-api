export type ConsentMode = "passthrough" | "exchange";

export interface PortalLinks {
    pm_portal: string | null;
    cm_portal: string | null;
}

/** GET /composite/v1/admin/overview */
export interface Overview {
    composite_partner_id: string;
    consent_mode: ConsentMode;
    exchange_cm_url: string;
    partner_mgmt_api_url: string;
    audit_manager_enabled: boolean;
    activity_recording: boolean;
    signing_kid: string | null;
    use_cases: { published: number; errors: number };
    registries: number;
    /** Partners granted a published use case in Partner Management; null when PM could not be read. */
    partners: number | null;
    activity_24h: { calls: number; failed: number } | null;
    links: PortalLinks;
}
