"use client";

import { useEffect, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import { toast } from "react-toastify";
import Forbidden from "@/components/Forbidden";
import LoadingState from "@/components/LoadingState";
import NotFound from "@/components/NotFound";
import { ErrorBox } from "@/components/ui";
import type { AdminQuery } from "@/shared/api/adminApi";

type QueryViewProps<T> = {
    query: AdminQuery<T>;
    children: (data: T) => ReactNode;
    /** Shown while the first load is running (default: the loading spinner). */
    loading?: ReactNode;
    /** For detail pages: where the "not found" view links back to. */
    notFound?: { backHref: string; backLabel: string };
};

/**
 * Renders an admin query: loading → spinner/skeleton, 403 → access denied, 404 → not found
 * (detail pages), other errors → error box with retry, otherwise `children(data)`.
 */
export default function QueryView<T>({ query, children, loading, notFound }: QueryViewProps<T>) {
    const t = useTranslations();
    const reloadFailed = query.data !== undefined ? query.error : undefined;

    // A failed refresh keeps the last data on screen and reports the error as a toast.
    useEffect(() => {
        if (reloadFailed) toast.error(reloadFailed);
    }, [reloadFailed]);

    if (query.status === 403) return <Forbidden />;
    if (query.status === 404 && notFound) return <NotFound message={query.error} {...notFound} />;
    if (query.data === undefined) {
        if (query.error) {
            return (
                <ErrorBox message={query.error}>
                    <button type="button" onClick={query.reload} className="ml-3 font-semibold underline">
                        {t("retry")}
                    </button>
                </ErrorBox>
            );
        }
        return <>{loading ?? <LoadingState />}</>;
    }
    return <>{children(query.data)}</>;
}
