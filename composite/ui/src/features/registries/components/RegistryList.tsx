"use client";

import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import DataTable, { type DataTableColumn } from "@/components/DataTable";
import QueryView from "@/components/QueryView";
import TableSkeleton from "@/components/TableSkeleton";
import { ChipList, PageHeader } from "@/components/ui";
import { toUseCaseHref } from "@/features/use-cases/api";
import { registryHref } from "../api";
import { useRegistries } from "../hooks";
import type { Registry } from "../types";
import ReachableBadge from "./ReachableBadge";

export default function RegistryList() {
    const t = useTranslations();
    const query = useRegistries();

    const columns: DataTableColumn<Registry>[] = [
        {
            key: "id",
            header: t("col_id"),
            render: (r) => (
                <Link href={registryHref(r.id)} className="font-mono font-medium text-black hover:text-[#ED7C22] hover:underline">
                    {r.id}
                </Link>
            ),
        },
        { key: "url", header: t("col_url"), render: (r) => <span className="break-all font-mono text-[13px]">{r.url}</span> },
        { key: "reachable", header: t("col_reachable"), render: (r) => <ReachableBadge reachable={r.reachable} /> },
        { key: "scopes", header: t("col_scopes"), render: (r) => (r.scope_count ?? "—") },
        {
            key: "used_by",
            header: t("col_used_by"),
            render: (r) => <ChipList items={r.used_by} mono hrefOf={toUseCaseHref} empty={t("not_used")} />,
        },
        {
            key: "error",
            header: t("col_error"),
            render: (r) => (r.error ? <span className="break-words text-[13px] text-red-700">{r.error}</span> : "—"),
        },
    ];

    return (
        <div className="space-y-6">
            <PageHeader title={t("nav_registries")} hint={t("registries_hint")} />
            <QueryView query={query} loading={<TableSkeleton columns={6} rows={4} />}>
                {(data) => (
                    <DataTable
                        columns={columns}
                        rows={data.registries}
                        rowKey={(r) => r.id}
                        searchText={(r) => [r.id, r.url, ...r.used_by].join(" ")}
                        emptyMessage={t("no_registries")}
                    />
                )}
            </QueryView>
        </div>
    );
}
