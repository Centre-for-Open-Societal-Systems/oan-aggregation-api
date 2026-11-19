export function formatDateTime(value?: string | null): string {
    if (!value) return "—";
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return value;
    return d.toLocaleString();
}

/** Value of a use-case parameter default / batch setting as short text. */
export function formatValue(value: unknown): string {
    if (value === undefined || value === null || value === "") return "—";
    if (typeof value === "string") return value;
    return JSON.stringify(value);
}

/** Distinct values, in first-seen order. */
export function unique<T>(values: T[]): T[] {
    return Array.from(new Set(values));
}
