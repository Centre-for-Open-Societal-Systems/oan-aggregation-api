"use client";

import { useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import { RefreshCw } from "lucide-react";
import Pagination from "@/components/Pagination";
import QueryView from "@/components/QueryView";
import TableSkeleton from "@/components/TableSkeleton";
import { EmptyState, PageHeader, StatusBadge, tdClass, thClass } from "@/components/ui";
import { Link } from "@/i18n/navigation";
import { toUseCaseHref } from "@/features/use-cases/api";
import { useUseCases } from "@/features/use-cases/hooks";
import { formatDateTime, unique } from "@/shared/utils/format";
import { useActivity } from "../hooks";
import { OUTCOMES, type ActivityFilters, type ActivityItem, type ActivityPage } from "../types";

const PAGE_SIZES = [25, 50, 100, 200];
const NO_FILTERS: ActivityFilters = { partner: "", use_case: "", outcome: "" };

const selectClass =
    "h-9 rounded-[10px] border border-[#ED7C22] bg-white px-3 text-[15px] text-black focus:outline-none";

function FilterSelect({ label, value, onChange, children }: {
    label: string;
    value: string | number;
    onChange: (value: string) => void;
    children: ReactNode;
}) {
    return (
        <label className="flex flex-col gap-1">
            <span className="text-[12px] font-semibold uppercase tracking-wide text-gray-500">{label}</span>
            <select value={value} onChange={(e) => onChange(e.target.value)} className={selectClass}>
                {children}
            </select>
        </label>
    );
}

function SourceChips({ sources }: { sources: ActivityItem["sources"] }) {
    const entries = Object.entries(sources ?? {});
    if (entries.length === 0) return <span className="text-gray-500">—</span>;
    return (
        <div className="flex flex-wrap gap-1">
            {entries.map(([source, status]) => (
                <StatusBadge key={source} status={status} label={`${source}: ${status}`} />
            ))}
        </div>
    );
}

function ActivityTable({ page }: { page: ActivityPage }) {
    const t = useTranslations();
    if (page.items.length === 0) return <EmptyState title={t("no_activity")} hint={t("no_activity_hint")} />;
    return (
        <div className="overflow-x-auto">
            <table className="w-full border-collapse">
                <thead>
                    <tr>
                        <th className={thClass}>{t("col_time")}</th>
                        <th className={thClass}>{t("col_use_case")}</th>
                        <th className={thClass}>{t("col_partner_id")}</th>
                        <th className={thClass}>{t("col_http_status")}</th>
                        <th className={thClass}>{t("col_outcome")}</th>
                        <th className={thClass}>{t("col_reason")}</th>
                        <th className={thClass}>{t("col_consent")}</th>
                        <th className={`${thClass} text-right`}>{t("col_duration_ms")}</th>
                        <th className={thClass}>{t("sources")}</th>
                    </tr>
                </thead>
                <tbody>
                    {page.items.map((item, index) => (
                        <tr
                            key={item.request_id || `${item.at}-${index}`}
                            className={`${index % 2 === 1 ? "bg-white" : "bg-gray-50"} hover:bg-gray-100`}
                        >
                            <td className={`${tdClass} whitespace-nowrap text-[14px]`} title={item.request_id}>
                                {formatDateTime(item.at)}
                            </td>
                            <td className={tdClass}>
                                {item.use_case ? (
                                    <Link href={toUseCaseHref(item.use_case)} className="font-mono text-[14px] hover:text-[#ED7C22] hover:underline">
                                        {item.use_case}
                                    </Link>
                                ) : (
                                    <span className="text-gray-500">—</span>
                                )}
                            </td>
                            <td className={`${tdClass} font-mono text-[14px]`}>{item.partner_id || "—"}</td>
                            <td className={`${tdClass} font-mono text-[14px]`}>{item.http_status}</td>
                            <td className={tdClass}>
                                <StatusBadge
                                    status={item.outcome}
                                    label={t.has(`outcome_${item.outcome}`) ? t(`outcome_${item.outcome}`) : item.outcome}
                                />
                            </td>
                            <td className={`${tdClass} break-words text-[14px]`}>{item.reason || "—"}</td>
                            <td className={`${tdClass} break-all font-mono text-[14px]`}>{item.consent_id || "—"}</td>
                            <td className={`${tdClass} text-right font-mono text-[14px]`}>
                                {item.duration_ms != null ? item.duration_ms.toLocaleString() : "—"}
                            </td>
                            <td className={tdClass}>
                                <SourceChips sources={item.sources} />
                            </td>
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
    );
}

/** Calls made to the composite, newest first, with server-side filters and limit/offset paging. */
export default function ActivityLog() {
    const t = useTranslations();
    const [filters, setFilters] = useState<ActivityFilters>(NO_FILTERS);
    const [pageSize, setPageSize] = useState(50);
    const [page, setPage] = useState(1);

    const query = useActivity(filters, pageSize, (page - 1) * pageSize);
    const useCaseRefs = useUseCases().data?.use_cases.map((u) => u.use_case) ?? [];
    // Senders in the call log (what it can be filtered on); the selected one stays listed while a page loads.
    const partnerIds = unique([...(query.data?.partners ?? []), filters.partner].filter(Boolean)).sort();

    const setFilter = (key: keyof ActivityFilters, value: string) => {
        setFilters((prev) => ({ ...prev, [key]: value }));
        setPage(1);
    };

    const recording = query.data?.recording !== false;

    return (
        <div className="space-y-6">
            <PageHeader
                title={t("nav_activity")}
                hint={t("activity_hint")}
                actions={
                    <button
                        type="button"
                        onClick={query.reload}
                        disabled={query.loading}
                        className="inline-flex items-center gap-2 rounded-[10px] bg-[#f4bb1b] px-4 py-2 text-[15px] font-medium text-black hover:bg-[#e5a818] disabled:opacity-50"
                    >
                        <RefreshCw size={15} className={query.loading ? "animate-spin" : ""} />
                        {t("refresh")}
                    </button>
                }
            />

            {recording ? (
                <div className="flex flex-wrap items-end gap-4 rounded-[10px] bg-white p-4 shadow-sm">
                    <FilterSelect label={t("col_partner_id")} value={filters.partner} onChange={(v) => setFilter("partner", v)}>
                        <option value="">{t("all")}</option>
                        {partnerIds.map((id) => (
                            <option key={id} value={id}>
                                {id}
                            </option>
                        ))}
                    </FilterSelect>
                    <FilterSelect label={t("col_use_case")} value={filters.use_case} onChange={(v) => setFilter("use_case", v)}>
                        <option value="">{t("all")}</option>
                        {useCaseRefs.map((ref) => (
                            <option key={ref} value={ref}>
                                {ref}
                            </option>
                        ))}
                    </FilterSelect>
                    <FilterSelect label={t("col_outcome")} value={filters.outcome} onChange={(v) => setFilter("outcome", v)}>
                        <option value="">{t("all")}</option>
                        {OUTCOMES.map((o) => (
                            <option key={o} value={o}>
                                {t(`outcome_${o}`)}
                            </option>
                        ))}
                    </FilterSelect>
                    <FilterSelect
                        label={t("page_size")}
                        value={pageSize}
                        onChange={(v) => {
                            setPageSize(Number(v));
                            setPage(1);
                        }}
                    >
                        {PAGE_SIZES.map((n) => (
                            <option key={n} value={n}>
                                {n}
                            </option>
                        ))}
                    </FilterSelect>
                    {filters.partner || filters.use_case || filters.outcome ? (
                        <button
                            type="button"
                            onClick={() => {
                                setFilters(NO_FILTERS);
                                setPage(1);
                            }}
                            className="h-9 text-[14px] font-medium text-[#ED7C22] hover:underline"
                        >
                            {t("clear_filters")}
                        </button>
                    ) : null}
                </div>
            ) : null}

            <QueryView query={query} loading={<TableSkeleton columns={8} rows={8} />}>
                {(data) =>
                    data.recording ? (
                        <div className="bg-white rounded-[10px] py-5 shadow-sm">
                            <div className="px-4">
                                <ActivityTable page={data} />
                            </div>
                            {data.total > 0 ? (
                                <div className="px-4 pt-4">
                                    <Pagination page={page} pageSize={pageSize} total={data.total} totalCapped={data.total_capped === true} onPageChange={setPage} />
                                </div>
                            ) : null}
                        </div>
                    ) : (
                        <div className="bg-white rounded-[10px] shadow-sm">
                            <EmptyState title={t("activity_not_recorded")} hint={t("activity_not_recorded_hint")} />
                        </div>
                    )
                }
            </QueryView>
        </div>
    );
}
