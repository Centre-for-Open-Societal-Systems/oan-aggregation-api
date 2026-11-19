"use client";

import { useTranslations } from "next-intl";
import { StatusBadge } from "@/components/ui";

/** reachable: true → reachable, false → unreachable, null → not checked yet. */
export default function ReachableBadge({ reachable }: { reachable: boolean | null }) {
    const t = useTranslations();
    const status = reachable === null ? "unchecked" : reachable ? "reachable" : "unreachable";
    return <StatusBadge status={status} label={t(`registry_${status}`)} />;
}
