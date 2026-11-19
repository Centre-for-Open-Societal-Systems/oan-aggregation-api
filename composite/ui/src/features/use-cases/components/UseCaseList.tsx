"use client";

import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";
import DataTable, { type DataTableColumn } from "@/components/DataTable";
import QueryView from "@/components/QueryView";
import TableSkeleton from "@/components/TableSkeleton";
import { ChipList, ErrorBox, PageHeader, StatusBadge } from "@/components/ui";
import { registryHref } from "@/features/registries/api";
import { toUseCaseHref } from "../api";
import { useUseCases } from "../hooks";
import type { UseCase, UseCaseLoadError } from "../types";
import { registriesOf } from "../utils";
import GrantedTo from "./GrantedTo";

function LoadErrors({ errors }: { errors: UseCaseLoadError[] }) {
    const t = useTranslations();
    if (errors.length === 0) return null;
    return (
        <ErrorBox>
            <p className="font-semibold">{t("load_errors_title", { count: errors.length })}</p>
            <p className="mb-2 text-[13px]">{t("load_errors_hint")}</p>
            <ul className="space-y-1">
                {errors.map((e) => (
                    <li key={e.file} className="text-[13px]">
                        <span className="font-mono font-semibold">{e.file}</span>
                        <span className="mx-2">—</span>
                        <span className="break-words font-mono">{e.error}</span>
                    </li>
                ))}
            </ul>
        </ErrorBox>
    );
}

export default function UseCaseList() {
    const t = useTranslations();
    const query = useUseCases();

    const columns: DataTableColumn<UseCase>[] = [
        {
            key: "ref",
            header: t("col_use_case"),
            render: (u) => (
                <Link href={toUseCaseHref(u.use_case)} className="font-mono font-medium text-black hover:text-[#ED7C22] hover:underline">
                    {u.use_case}
                </Link>
            ),
        },
        { key: "title", header: t("col_title"), render: (u) => u.title || "—" },
        { key: "version", header: t("col_version"), render: (u) => u.version },
        { key: "status", header: t("col_status"), render: (u) => <StatusBadge status={u.status} /> },
        { key: "purpose", header: t("col_purpose"), render: (u) => <span className="font-mono text-[13px]">{u.purpose || "—"}</span> },
        {
            key: "registries",
            header: t("col_registries"),
            render: (u) => <ChipList items={registriesOf(u)} mono hrefOf={registryHref} />,
        },
        {
            key: "partners",
            header: t("col_granted_to"),
            render: (u) => <GrantedTo useCase={u} />,
        },
    ];

    return (
        <div className="space-y-6">
            <PageHeader title={t("nav_use_cases")} hint={t("use_cases_hint")} />
            <QueryView query={query} loading={<TableSkeleton columns={7} rows={5} />}>
                {(data) => (
                    <>
                        <LoadErrors errors={data.errors} />
                        <DataTable
                            columns={columns}
                            rows={data.use_cases}
                            rowKey={(u) => u.use_case}
                            searchText={(u) =>
                                [u.use_case, u.title, u.purpose, ...registriesOf(u), ...(u.granted_to ?? [])].join(" ")
                            }
                            emptyMessage={t("no_use_cases")}
                        />
                    </>
                )}
            </QueryView>
        </div>
    );
}
