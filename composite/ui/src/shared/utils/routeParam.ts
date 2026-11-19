/**
 * A dynamic route segment as the app meant it. useParams() hands it back still URL-encoded
 * (e.g. "loan-profile%401" for "loan-profile@1"); decode once, and keep the raw value if it
 * is not valid percent-encoding (a stray "%" must not crash the page).
 */
export function routeParam(value: string | string[] | undefined): string {
    const raw = Array.isArray(value) ? value[0] ?? "" : value ?? "";
    try {
        return decodeURIComponent(raw);
    } catch {
        return raw;
    }
}
