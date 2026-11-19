"use client";

import { useState, type ReactNode } from "react";
import { ArrowRight, ChevronDown, ChevronRight, ExternalLink, Inbox } from "lucide-react";
import { Link } from "@/i18n/navigation";

export const thClass =
    "text-left pb-3 pt-1 px-4 border-b border-gray-200 font-semibold text-[#ED7C22] text-[15px] tracking-wider";
export const tdClass = "py-2 px-4 align-top text-[15px]";

export function Panel({ children, className = "" }: { children: ReactNode; className?: string }) {
    return <div className={`bg-white rounded-[10px] p-5 shadow-sm ${className}`}>{children}</div>;
}

/** Inline error box for backend errors (shown in place). */
export function ErrorBox({ message, children }: { message?: string | null; children?: ReactNode }) {
    if (!message && !children) return null;
    return (
        <div className="rounded border border-red-200 bg-red-50 px-4 py-2 text-[14px] text-red-700" role="alert">
            {message}
            {children}
        </div>
    );
}

/** Amber notice for configuration worth attention (not an error). */
export function WarningBox({ children }: { children: ReactNode }) {
    return (
        <div className="rounded border border-amber-300 bg-amber-50 px-4 py-2 text-[14px] text-amber-800" role="status">
            {children}
        </div>
    );
}

const STATUS_STYLES: Record<string, string> = {
    published: "bg-green-50 text-green-700 border-green-200",
    active: "bg-green-50 text-green-700 border-green-200",
    success: "bg-green-50 text-green-700 border-green-200",
    ok: "bg-green-50 text-green-700 border-green-200",
    reachable: "bg-green-50 text-green-700 border-green-200",
    on: "bg-green-50 text-green-700 border-green-200",
    draft: "bg-blue-50 text-blue-700 border-blue-200",
    denied: "bg-amber-50 text-amber-700 border-amber-300",
    unchecked: "bg-gray-100 text-gray-600 border-gray-300",
    off: "bg-gray-100 text-gray-600 border-gray-300",
    retired: "bg-gray-100 text-gray-600 border-gray-300",
    inactive: "bg-gray-100 text-gray-600 border-gray-300",
    skipped: "bg-gray-100 text-gray-600 border-gray-300",
    failure: "bg-red-50 text-red-700 border-red-200",
    error: "bg-red-50 text-red-700 border-red-200",
    unreachable: "bg-red-50 text-red-700 border-red-200",
    unavailable: "bg-red-50 text-red-700 border-red-200",
    mandatory: "bg-[#f4bb1b]/20 text-black border-[#f4bb1b]",
    required: "bg-[#f4bb1b]/20 text-black border-[#f4bb1b]",
    optional: "bg-gray-100 text-gray-700 border-gray-300",
};

/** Small pill for a status / outcome. Unknown statuses are grey. */
export function StatusBadge({ status, label, title }: { status: string; label?: string; title?: string }) {
    const cls = STATUS_STYLES[status.toLowerCase()] ?? "bg-gray-100 text-gray-700 border-gray-300";
    return (
        <span
            title={title}
            className={`inline-flex items-center whitespace-nowrap rounded-full border px-2 py-0.5 text-[12px] font-semibold uppercase tracking-wide ${cls}`}
        >
            {label ?? status}
        </span>
    );
}

/** Neutral pill for identifiers (scopes, partners, ID types); links when `href` is given. */
export function Chip({ children, href, title, mono = false }: { children: ReactNode; href?: string; title?: string; mono?: boolean }) {
    const cls = `inline-flex items-center rounded-full border border-gray-200 bg-gray-50 px-2.5 py-0.5 text-[13px] text-gray-800 ${
        mono ? "font-mono" : ""
    }`;
    return href ? (
        <Link href={href} title={title} className={`${cls} hover:border-[#ED7C22] hover:text-black`}>
            {children}
        </Link>
    ) : (
        <span title={title} className={cls}>
            {children}
        </span>
    );
}

export function ChipList({ items, empty = "—", mono = false, hrefOf }: {
    items: string[] | null | undefined;
    empty?: string;
    mono?: boolean;
    hrefOf?: (item: string) => string | undefined;
}) {
    if (!items || items.length === 0) return <span className="text-gray-500">{empty}</span>;
    return (
        <div className="flex flex-wrap gap-1.5">
            {items.map((item) => (
                <Chip key={item} mono={mono} href={hrefOf?.(item)}>
                    {item}
                </Chip>
            ))}
        </div>
    );
}

export function PageHeader({ title, hint, actions }: { title: ReactNode; hint?: ReactNode; actions?: ReactNode }) {
    return (
        <div className="flex flex-wrap items-end justify-between gap-4">
            <div className="min-w-0">
                <h1 className="text-[24px] font-semibold text-black">{title}</h1>
                {hint ? <p className="text-[14px] text-gray-600">{hint}</p> : null}
            </div>
            {actions ? <div className="flex flex-wrap items-center gap-3">{actions}</div> : null}
        </div>
    );
}

export function SectionTitle({ title, href, linkLabel, hint }: { title: string; href?: string; linkLabel?: string; hint?: ReactNode }) {
    return (
        <div className="mb-3">
            <div className="flex items-center justify-between gap-3">
                <h2 className="text-[18px] font-semibold text-black">{title}</h2>
                {href && linkLabel ? (
                    <Link href={href} className="flex items-center gap-1 text-[14px] font-medium text-[#ED7C22] hover:underline">
                        {linkLabel}
                        <ArrowRight size={14} />
                    </Link>
                ) : null}
            </div>
            {hint ? <p className="mt-0.5 text-[13px] text-gray-600">{hint}</p> : null}
        </div>
    );
}

export function StatTile({ label, value, sub, href }: { label: string; value: ReactNode; sub?: ReactNode; href?: string }) {
    const body = (
        <>
            <div className="text-[13px] font-semibold uppercase tracking-wide text-gray-500">{label}</div>
            <div className="mt-1 text-[28px] font-semibold leading-tight text-black">{value}</div>
            {sub ? <div className="mt-0.5 truncate text-[13px] text-gray-600">{sub}</div> : null}
        </>
    );
    return href ? (
        <Link href={href} className="block rounded-[10px] bg-white p-4 shadow-sm transition-colors hover:bg-[#f4bb1b]/10">
            {body}
        </Link>
    ) : (
        <div className="rounded-[10px] bg-white p-4 shadow-sm">{body}</div>
    );
}

/** Two-column term / value list. */
export function DefinitionList({ items }: { items: { label: string; value: ReactNode; hint?: ReactNode }[] }) {
    return (
        <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-[14px] sm:grid-cols-[max-content_1fr]">
            {items.map((item) => (
                <div key={item.label} className="contents">
                    <dt className="text-gray-500">{item.label}</dt>
                    <dd className="min-w-0 break-words text-black">
                        {item.value}
                        {item.hint ? <div className="mt-0.5 text-[13px] text-gray-600">{item.hint}</div> : null}
                    </dd>
                </div>
            ))}
        </dl>
    );
}

export function EmptyState({ title, hint }: { title: string; hint?: ReactNode }) {
    return (
        <div className="flex flex-col items-center justify-center gap-2 px-4 py-10 text-center">
            <Inbox size={36} className="text-gray-300" />
            <p className="text-[16px] font-medium text-gray-700">{title}</p>
            {hint ? <p className="max-w-xl text-[14px] text-gray-500">{hint}</p> : null}
        </div>
    );
}

/** Section that starts closed; the header toggles it. */
export function Collapsible({ title, children, defaultOpen = false }: { title: string; children: ReactNode; defaultOpen?: boolean }) {
    const [open, setOpen] = useState(defaultOpen);
    return (
        <div>
            <button
                type="button"
                onClick={() => setOpen((o) => !o)}
                aria-expanded={open}
                className="flex cursor-pointer items-center gap-1.5 text-[15px] font-semibold text-black hover:text-[#ED7C22]"
            >
                {open ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
                {title}
            </button>
            {open ? <div className="mt-3">{children}</div> : null}
        </div>
    );
}

export function CodeBlock({ children }: { children: string }) {
    return (
        <pre className="modal-scroll max-h-[600px] overflow-auto rounded-[10px] bg-[#061327] p-4 font-mono text-[13px] leading-relaxed text-gray-100">
            {children}
        </pre>
    );
}

/** Link to another OpenG2P portal (opens in a new tab). */
export function ExternalPortalLink({ href, label }: { href: string; label: string }) {
    return (
        <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 rounded-[10px] bg-[#f4bb1b] px-4 py-2 text-[15px] font-medium text-black hover:bg-[#e5a818]"
        >
            {label}
            <ExternalLink size={14} />
        </a>
    );
}
