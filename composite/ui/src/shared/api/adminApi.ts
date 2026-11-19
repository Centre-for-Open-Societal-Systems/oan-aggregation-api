"use client";

import { useCallback, useEffect, useState } from "react";
import { useAuth } from "@/context/Authcontext";

/** Error of an admin API call: HTTP status plus the backend's code and message. */
export class AdminApiError extends Error {
    status: number;
    code?: string;

    constructor(message: string, status: number, code?: string) {
        super(message);
        this.name = "AdminApiError";
        this.status = status;
        this.code = code;
    }
}

/** Message and code from the error bodies the proxy can return (composite, iam_core, proxy). */
function parseError(body: unknown, res: Response): { message: string; code?: string } {
    const obj = (body ?? {}) as Record<string, unknown>;
    const error = (obj.error ?? undefined) as Record<string, unknown> | string | undefined;
    const nested = Array.isArray(obj.errors) ? (obj.errors[0] as Record<string, unknown>) : undefined;
    const detail = obj.detail as unknown;
    const message =
        (typeof error === "object" && error?.message) ||
        (typeof error === "string" && error) ||
        nested?.message ||
        (typeof detail === "string" && detail) ||
        obj.message ||
        obj.statusText ||
        res.statusText ||
        `Error ${res.status}`;
    const code = (typeof error === "object" && error?.code) || nested?.code || obj.code;
    return { message: String(message), code: code != null ? String(code) : undefined };
}

/** True when IAM no longer has a session for this browser (/api/me answers 401). */
async function isSessionGone(signal?: AbortSignal): Promise<boolean> {
    try {
        const res = await fetch("/api/me", { credentials: "include", cache: "no-store", signal });
        return res.status === 401;
    } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") throw error;
        return false;
    }
}

/**
 * Returns `get(path, query?, signal?)`, which GETs `/api/admin/<path>` and resolves to the JSON
 * body or throws an AdminApiError. A 401 sends the user to the IAM login when the IAM session is gone.
 */
export function useAdminApi() {
    const { handleUnauthorized } = useAuth();

    return useCallback(
        async <T,>(path: string, signal?: AbortSignal): Promise<T> => {
            const res = await fetch(`/api/admin/${path}`, {
                method: "GET",
                credentials: "include",
                headers: { accept: "application/json" },
                cache: "no-store",
                signal,
            });
            if (res.status === 401) {
                // Only a lost IAM session means "log in again": ask /api/me. If IAM still knows the
                // user, the composite API rejected the token itself and a login would just loop.
                if (await isSessionGone(signal)) {
                    handleUnauthorized();
                    throw new AdminApiError("Unauthorized", 401, "G2P-AUT-401");
                }
                throw new AdminApiError("The composite API rejected your session", 401, "G2P-AUT-401");
            }
            let body: unknown = null;
            try {
                body = await res.json();
            } catch {
                body = null;
            }
            if (!res.ok) {
                const { message, code } = parseError(body, res);
                throw new AdminApiError(message, res.status, code);
            }
            return body as T;
        },
        [handleUnauthorized],
    );
}

/** Query string from the defined, non-empty values (`?a=1&b=x`, or "" when there are none). */
export function toQuery(params: Record<string, string | number | null | undefined>): string {
    const q = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
        if (value !== undefined && value !== null && value !== "") q.set(key, String(value));
    }
    const s = q.toString();
    return s ? `?${s}` : "";
}

export interface AdminQuery<T> {
    data?: T;
    error?: string;
    /** HTTP status of the failed call (403 → access denied, 404 → not found). */
    status?: number;
    loading: boolean;
    reload: () => void;
}

interface QueryState<T> {
    key: string | null;
    nonce: number;
    data?: T;
    error?: string;
    status?: number;
}

/**
 * Loads `/api/admin/<path>` (skipped while path is null) and reloads when it changes.
 * Keeps the previous data visible while a reload of the same path is running.
 */
export function useAdminQuery<T>(path: string | null): AdminQuery<T> {
    const get = useAdminApi();
    const [nonce, setNonce] = useState(0);
    const [state, setState] = useState<QueryState<T>>({ key: null, nonce: -1 });

    useEffect(() => {
        if (path === null) return;
        const controller = new AbortController();
        get<T>(path, controller.signal).then(
            (data) => {
                if (!controller.signal.aborted) setState({ key: path, nonce, data });
            },
            (error: unknown) => {
                if (controller.signal.aborted) return;
                if (error instanceof DOMException && error.name === "AbortError") return;
                // A failed reload of the same path keeps the last data (QueryView shows it + a toast).
                setState((prev) => ({
                    key: path,
                    nonce,
                    data: prev.key === path ? prev.data : undefined,
                    error: error instanceof Error ? error.message : "Something went wrong",
                    status: error instanceof AdminApiError ? error.status : undefined,
                }));
            },
        );
        return () => controller.abort();
    }, [path, nonce, get]);

    const sameKey = path !== null && state.key === path;
    const loading = path !== null && (!sameKey || state.nonce !== nonce);
    const reload = useCallback(() => setNonce((n) => n + 1), []);

    return {
        data: sameKey ? state.data : undefined,
        error: sameKey && !loading ? state.error : undefined,
        status: sameKey && !loading ? state.status : undefined,
        loading,
        reload,
    };
}
