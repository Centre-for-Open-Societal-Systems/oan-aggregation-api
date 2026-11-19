"use client";

import LoadingState from "@/components/LoadingState";
import {
    createContext,
    useCallback,
    useContext,
    useMemo,
    useState,
    useEffect,
    type ReactNode,
} from "react";

import { useTranslations } from "next-intl";
import { ErrorBox } from "@/components/ui";
import { useAuth } from "@/context/Authcontext";

interface RbacContextType {
    loading: boolean;
    actions: string[];
    can: (action: string) => boolean;
    canAny: (actionList: readonly string[]) => boolean;
    canAll: (actionList: readonly string[]) => boolean;
    refresh: () => Promise<void>;
}

const RbacContext = createContext<RbacContextType | null>(null);

export function RbacProvider({ children }: { children: ReactNode }) {
    const { isLoggedIn, handleUnauthorized } = useAuth();
    const [loading, setLoading] = useState(true);
    const [actionSet, setActionSet] = useState<Set<string>>(new Set());
    /** Set when the permissions could not be read (not the same as "no permissions"). */
    const [error, setError] = useState<string | null>(null);
    const t = useTranslations();

    /**
     * Permissions of the user in this application (APPLICATION_MNEMONIC), null after a 401, or
     * `{ error }` when IAM could not be asked (so the UI shows an error, not "Access denied").
     */
    const fetchActions = useCallback(async (): Promise<Set<string> | { error: string } | null> => {
        if (!isLoggedIn) return new Set();
        try {
            const res = await fetch("/api/permissions", { cache: "no-store" });
            if (res.status === 401) {
                handleUnauthorized();
                return null;
            }
            let data: unknown = null;
            try {
                data = await res.json();
            } catch {
                data = null;
            }
            if (!res.ok) {
                const body = (data ?? {}) as { error?: { message?: string } | string };
                const message = typeof body.error === "string" ? body.error : body.error?.message;
                return { error: message || `HTTP ${res.status}` };
            }
            const permissions = Array.isArray(data)
                ? data.flatMap((app: { permissions?: string[] }) => app.permissions || [])
                : [];
            return new Set<string>(permissions);
        } catch (e) {
            return { error: e instanceof Error ? e.message : String(e) };
        }
    }, [isLoggedIn, handleUnauthorized]);

    const applyActions = useCallback((actions: Set<string> | { error: string } | null) => {
        if (actions instanceof Set) {
            setActionSet(actions);
            setError(null);
        } else if (actions) {
            setError(actions.error);
        }
    }, []);

    const loadActions = useCallback(async () => {
        setLoading(true);
        applyActions(await fetchActions());
        setLoading(false);
    }, [fetchActions, applyActions]);

    useEffect(() => {
        let cancelled = false;
        fetchActions().then((actions) => {
            if (cancelled) return;
            applyActions(actions);
            setLoading(false);
        });
        return () => {
            cancelled = true;
        };
    }, [fetchActions, applyActions]);

    const can = useCallback(
        (action: string) => actionSet.has(action),
        [actionSet]
    );

    const canAny = useCallback(
        (actionList: readonly string[]) => actionList.some((a) => actionSet.has(a)),
        [actionSet]
    );

    const canAll = useCallback(
        (actionList: readonly string[]) => actionList.every((a) => actionSet.has(a)),
        [actionSet]
    );

    const value = useMemo<RbacContextType>(
        () => ({
            loading,
            actions: Array.from(actionSet),
            can,
            canAny,
            canAll,
            refresh: loadActions,
        }),
        [loading, actionSet, can, canAny, canAll, loadActions]
    );

    if (loading) {
        return <LoadingState fullScreen />;
    }

    if (error) {
        return (
            <div className="w-full min-h-screen flex items-center justify-center bg-secondary-first px-4">
                <div className="max-w-150 w-full">
                    <ErrorBox message={`${t("permissions_error")}: ${error}`}>
                        <button type="button" onClick={loadActions} className="ml-3 font-semibold underline">
                            {t("retry")}
                        </button>
                    </ErrorBox>
                </div>
            </div>
        );
    }

    return (
        <RbacContext.Provider value={value}>
            {children}
        </RbacContext.Provider>
    );
}

export function useRbac() {
    const context = useContext(RbacContext);
    if (!context) {
        throw new Error("useRbac must be used inside <RbacProvider>");
    }
    return context;
}
