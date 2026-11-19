"use client";

import { useTranslations } from "next-intl";
import { ChipList } from "@/components/ui";
import type { UseCase } from "../types";

/** Partners whose use-case policy in Partner Management grants the use case (or why PM could not say). */
export default function GrantedTo({ useCase }: { useCase: Pick<UseCase, "granted_to" | "granted_to_error"> }) {
    const t = useTranslations();
    if (useCase.granted_to === null) {
        return <span className="break-words text-[13px] text-red-700">{useCase.granted_to_error || t("pm_unavailable")}</span>;
    }
    return <ChipList items={useCase.granted_to} mono empty={t("none")} />;
}
