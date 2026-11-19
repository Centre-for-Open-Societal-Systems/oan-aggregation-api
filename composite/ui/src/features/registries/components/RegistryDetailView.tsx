"use client";

import { Fragment, useState } from "react";
import { useTranslations } from "next-intl";
import { ArrowLeft, ChevronDown, ChevronRight } from "lucide-react";
import { Link } from "@/i18n/navigation";
import QueryView from "@/components/QueryView";
import TableSkeleton from "@/components/TableSkeleton";
import {
    Chip,
    DefinitionList,
    EmptyState,
    ErrorBox,
    Panel,
    SectionTitle,
    StatusBadge,
    tdClass,
    thClass,
} from "@/components/ui";
import { toUseCaseHref } from "@/features/use-cases/api";
import { formatDateTime } from "@/shared/utils/format";
import { useDataScopes, useRegistries } from "../hooks";
import type { DataScope, Registry } from "../types";
import ReachableBadge from "./ReachableBadge";

function ScopeUsage({ scope }: { scope: DataScope }) {
    const t = useTranslations();
    if (scope.used_by.length === 0) return <span className="text-gray-500">{t("not_used")}</span>;
    return (
        <div className="flex flex-wrap gap-1.5">
            {scope.used_by.map((u) => (
                <Chip
                    key={`${u.use_case}:${u.source}`}
                    href={toUseCaseHref(u.use_case)}
                    title={u.required ? t("scope_required_by") : t("scope_optional_for")}
                >
                    <span className="font-mono">{u.use_case}</span>
                    <span className="mx-1 text-gray-400">/</span>
                    <span className="font-mono">{u.source}</span>
                    <span className={`ml-1.5 text-[11px] font-semibold uppercase ${u.required ? "text-[#ED7C22]" : "text-gray-500"}`}>
                        {u.required ? t("required") : t("optional")}
                    </span>
                </Chip>
            ))}
        </div>
    );
}

function ScopesTable({ scopes }: { scopes: DataScope[] }) {
    const t = useTranslations();
    const [open, setOpen] = useState<Set<string>>(new Set());
    const toggle = (id: string) =>
        setOpen((prev) => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });

    return (
        <div className="overflow-x-auto">
            <table className="w-full border-collapse">
                <thead>
                    <tr>
                        <th className={thClass}>{t("col_scope_id")}</th>
                        <th className={thClass}>{t("col_label")}</th>
                        <th className={thClass}>{t("col_status")}</th>
                        <th className={thClass}>{t("col_version")}</th>
                        <th className={thClass}>{t("col_fields")}</th>
                        <th className={thClass}>{t("col_used_by")}</th>
                    </tr>
                </thead>
                <tbody>
                    {scopes.map((s, index) => {
                        const isOpen = open.has(s.scope_id);
                        return (
                            <Fragment key={s.scope_id}>
                                <tr className={`${index % 2 === 1 ? "bg-white" : "bg-gray-50"} hover:bg-gray-100`}>
                                    <td className={tdClass}>
                                        <button
                                            type="button"
                                            onClick={() => toggle(s.scope_id)}
                                            aria-expanded={isOpen}
                                            title={isOpen ? t("hide_fields") : t("show_fields")}
                                            className="flex cursor-pointer items-start gap-1 text-left font-mono text-[14px] text-black hover:text-[#ED7C22]"
                                        >
                                            {isOpen ? <ChevronDown size={16} className="mt-0.5 shrink-0" /> : <ChevronRight size={16} className="mt-0.5 shrink-0" />}
                                            <span className="break-all">{s.scope_id}</span>
                                        </button>
                                    </td>
                                    <td className={tdClass}>
                                        <div>{s.label || s.name}</div>
                                        {s.description ? <div className="text-[13px] text-gray-500">{s.description}</div> : null}
                                    </td>
                                    <td className={tdClass}>
                                        <StatusBadge status={s.status} />
                                    </td>
                                    <td className={tdClass}>{s.current_version ?? "—"}</td>
                                    <td className={tdClass}>{s.fields.length}</td>
                                    <td className={tdClass}>
                                        <ScopeUsage scope={s} />
                                    </td>
                                </tr>
                                {isOpen ? (
                                    <tr className="bg-white">
                                        <td colSpan={6} className="px-4 pb-4 pt-1">
                                            {s.fields.length === 0 ? (
                                                <span className="text-[14px] text-gray-500">{t("no_fields")}</span>
                                            ) : (
                                                <ul className="grid gap-x-6 gap-y-1 rounded-[10px] border border-gray-200 bg-gray-50 p-3 font-mono text-[13px] text-gray-800 md:grid-cols-2 xl:grid-cols-3">
                                                    {s.fields.map((f) => (
                                                        <li key={f} className="break-all">
                                                            {f}
                                                        </li>
                                                    ))}
                                                </ul>
                                            )}
                                        </td>
                                    </tr>
                                ) : null}
                            </Fragment>
                        );
                    })}
                </tbody>
            </table>
        </div>
    );
}

function RegistryInfo({ registry }: { registry: Registry }) {
    const t = useTranslations();
    return (
        <Panel>
            <DefinitionList
                items={[
                    { label: t("col_url"), value: <span className="break-all font-mono text-[13px]">{registry.url}</span> },
                    {
                        label: t("catalogue_url"),
                        value: <span className="break-all font-mono text-[13px]">{registry.catalogue_url || "—"}</span>,
                    },
                    { label: t("col_reachable"), value: <ReachableBadge reachable={registry.reachable} /> },
                    ...(registry.partner_id
                        ? [{ label: t("registry_partner_id"), value: <span className="font-mono">{registry.partner_id}</span> }]
                        : []),
                    ...(registry.receiver_id
                        ? [{ label: t("registry_receiver_id"), value: <span className="font-mono">{registry.receiver_id}</span> }]
                        : []),
                ]}
            />
        </Panel>
    );
}

export default function RegistryDetailView({ registryId }: { registryId: string }) {
    const t = useTranslations();
    const scopesQuery = useDataScopes(registryId);
    // The registry's connection details come from the list (there is no single-registry endpoint).
    const registry = useRegistries().data?.registries.find((r) => r.id === registryId);

    return (
        <QueryView
            query={scopesQuery}
            loading={<TableSkeleton columns={6} rows={6} />}
            notFound={{ backHref: "/registries", backLabel: t("back_to_registries") }}
        >
            {(data) => (
                <div className="space-y-6">
                    <div className="space-y-3">
                        <Link href="/registries" className="inline-flex items-center gap-1 text-[14px] font-medium text-[#ED7C22] hover:underline">
                            <ArrowLeft size={14} />
                            {t("nav_registries")}
                        </Link>
                        <h1 className="font-mono text-[24px] font-semibold text-black">{data.registry}</h1>
                        {data.data_controller && data.data_controller !== data.registry ? (
                            <p className="text-[14px] text-gray-600">
                                {t("data_controller")}: <span className="font-mono">{data.data_controller}</span>
                            </p>
                        ) : null}
                    </div>
                    {registry ? <RegistryInfo registry={registry} /> : null}
                    <ErrorBox message={data.error ? t("scopes_fetch_error", { error: data.error }) : null} />
                    <Panel>
                        <SectionTitle
                            title={t("data_scopes")}
                            hint={
                                data.fetched_at
                                    ? t("data_scopes_hint_fetched", { at: formatDateTime(data.fetched_at) })
                                    : t("data_scopes_hint")
                            }
                        />
                        {data.data_scopes.length === 0 ? (
                            <EmptyState title={t("no_data_scopes")} />
                        ) : (
                            <ScopesTable scopes={data.data_scopes} />
                        )}
                    </Panel>
                </div>
            )}
        </QueryView>
    );
}
