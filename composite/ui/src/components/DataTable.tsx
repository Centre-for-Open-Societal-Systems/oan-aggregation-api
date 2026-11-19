"use client";

import { useMemo, useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import Pagination from "@/components/Pagination";
import SearchInput from "@/components/SearchInput";
import { tdClass, thClass } from "@/components/ui";

export type DataTableColumn<T> = {
    key: string;
    header: string;
    className?: string;
    render: (row: T) => ReactNode;
};

type DataTableProps<T> = {
    columns: DataTableColumn<T>[];
    rows: T[];
    rowKey: (row: T) => string;
    /** Text the search box matches against; no search box when omitted. */
    searchText?: (row: T) => string;
    pageSize?: number;
    emptyMessage?: string;
};

/**
 * Read-only table in the Master Data style (white card, orange headers, zebra rows) with
 * client-side search and paging.
 */
export default function DataTable<T>({
    columns,
    rows,
    rowKey,
    searchText,
    pageSize = 20,
    emptyMessage,
}: DataTableProps<T>) {
    const t = useTranslations();
    const [search, setSearch] = useState("");
    const [page, setPage] = useState(1);

    const filtered = useMemo(() => {
        const query = search.trim().toLowerCase();
        if (!query || !searchText) return rows;
        return rows.filter((row) => searchText(row).toLowerCase().includes(query));
    }, [rows, search, searchText]);

    const total = filtered.length;
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    const currentPage = Math.min(page, totalPages);
    const pageRows = filtered.slice((currentPage - 1) * pageSize, currentPage * pageSize);

    return (
        <div className="bg-white rounded-[10px] py-5 shadow-sm">
            {searchText ? (
                <div className="flex justify-end px-4 pb-4">
                    <SearchInput
                        value={search}
                        onChange={(value) => {
                            setSearch(value);
                            setPage(1);
                        }}
                        placeholder={t("search")}
                    />
                </div>
            ) : null}
            <div className="overflow-x-auto px-4">
                <table className="w-full border-collapse bg-white">
                    <thead>
                        <tr>
                            {columns.map((column) => (
                                <th key={column.key} className={`${thClass} ${column.className ?? ""}`}>
                                    {column.header}
                                </th>
                            ))}
                        </tr>
                    </thead>
                    <tbody>
                        {pageRows.length === 0 ? (
                            <tr>
                                <td colSpan={columns.length} className="px-4 py-10 text-center text-gray-600">
                                    {search ? t("no_results") : emptyMessage ?? t("no_results")}
                                </td>
                            </tr>
                        ) : (
                            pageRows.map((row, index) => (
                                <tr
                                    key={rowKey(row)}
                                    className={`transition-colors duration-150 ${index % 2 === 1 ? "bg-white" : "bg-gray-50"} hover:bg-gray-100`}
                                >
                                    {columns.map((column) => (
                                        <td key={column.key} className={`${tdClass} ${column.className ?? ""}`}>
                                            {column.render(row)}
                                        </td>
                                    ))}
                                </tr>
                            ))
                        )}
                    </tbody>
                </table>
            </div>
            {total > pageSize ? (
                <div className="px-4 pt-4">
                    <Pagination page={currentPage} pageSize={pageSize} total={total} onPageChange={setPage} />
                </div>
            ) : null}
        </div>
    );
}
