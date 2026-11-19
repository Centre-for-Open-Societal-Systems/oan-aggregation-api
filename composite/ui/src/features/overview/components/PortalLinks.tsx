"use client";

import { useTranslations } from "next-intl";
import { ExternalPortalLink } from "@/components/ui";
import type { PortalLinks as Links } from "../types";

/** Buttons to the Partner Management and Consent Manager portals (each only when configured). */
export function PortalLinks({ links }: { links: Links | null | undefined }) {
    const t = useTranslations();
    if (!links?.pm_portal && !links?.cm_portal) return null;
    return (
        <div className="flex flex-wrap gap-3">
            {links.pm_portal ? <ExternalPortalLink href={links.pm_portal} label={t("pm_portal")} /> : null}
            {links.cm_portal ? <ExternalPortalLink href={links.cm_portal} label={t("cm_portal")} /> : null}
        </div>
    );
}
