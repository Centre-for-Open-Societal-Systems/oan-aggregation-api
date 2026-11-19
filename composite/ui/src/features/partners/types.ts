import type { PortalLinks } from "@/features/overview/types";

export interface PartnerKey {
    kid: string;
    algorithm: string;
}

/** One item of GET /composite/v1/admin/partners: a partner with Partner Management policies for published use cases (one policy per use case). */
export interface Partner {
    /** Partner Management partner ID, e.g. PARTNER_BANK_A. */
    partner_id: string;
    /** Published use cases (name@major) it has a policy for. */
    use_cases: string[];
    /** null when Partner Management could not be read (see pm_error). */
    pm_keys: PartnerKey[] | null;
    pm_error: string | null;
}

export interface PartnerList {
    partners: Partner[];
    /** Set when Partner Management's policies could not be read (the list is then incomplete). */
    pm_error: string | null;
    links: PortalLinks;
}
