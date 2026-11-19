"use client";

import { useTranslations } from "next-intl";
import QueryView from "@/components/QueryView";
import {
    DefinitionList,
    PageHeader,
    Panel,
    SectionTitle,
    StatTile,
    StatusBadge,
    WarningBox,
} from "@/components/ui";
import { useOverview } from "../hooks";
import type { Overview } from "../types";
import { PortalLinks } from "./PortalLinks";

function OnOff({ on }: { on: boolean }) {
    const t = useTranslations();
    return <StatusBadge status={on ? "on" : "off"} label={on ? t("on") : t("off")} />;
}

function Mono({ children }: { children: string }) {
    return <span className="break-all font-mono text-[13px]">{children}</span>;
}

function Settings({ overview }: { overview: Overview }) {
    const t = useTranslations();
    const exchange = overview.consent_mode === "exchange";

    return (
        <Panel>
            <SectionTitle title={t("overview_settings")} />
            <DefinitionList
                items={[
                    { label: t("composite_partner_id"), value: <Mono>{overview.composite_partner_id}</Mono> },
                    {
                        label: t("consent_mode"),
                        value: <StatusBadge status={overview.consent_mode} label={t(`consent_mode_${overview.consent_mode}`)} />,
                        hint: exchange ? t("consent_mode_exchange_hint") : t("consent_mode_passthrough_hint"),
                    },
                    ...(exchange
                        ? [{ label: t("exchange_cm_url"), value: <Mono>{overview.exchange_cm_url || "—"}</Mono> }]
                        : []),
                    { label: t("pm_url"), value: <Mono>{overview.partner_mgmt_api_url || "—"}</Mono> },
                    { label: t("audit_manager"), value: <OnOff on={overview.audit_manager_enabled} /> },
                    { label: t("activity_recording"), value: <OnOff on={overview.activity_recording} /> },
                    {
                        label: t("signing_kid"),
                        value: overview.signing_kid ? (
                            <Mono>{overview.signing_kid}</Mono>
                        ) : (
                            <WarningBox>{t("signing_kid_missing")}</WarningBox>
                        ),
                    },
                ]}
            />
        </Panel>
    );
}

/** Home page: the composite's configuration, catalogue counts and the last 24 hours of calls. */
export default function OverviewDashboard() {
    const t = useTranslations();
    const query = useOverview();

    return (
        <div className="space-y-6">
            <PageHeader title={t("nav_overview")} hint={t("overview_hint")} />
            <QueryView query={query}>
                {(overview) => (
                    <>
                        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
                            <StatTile
                                label={t("published_use_cases")}
                                value={overview.use_cases.published}
                                href="/use-cases"
                            />
                            <StatTile
                                label={t("load_errors")}
                                value={
                                    <span className={overview.use_cases.errors ? "text-red-600" : undefined}>
                                        {overview.use_cases.errors}
                                    </span>
                                }
                                sub={overview.use_cases.errors ? t("load_errors_sub") : undefined}
                                href="/use-cases"
                            />
                            <StatTile label={t("nav_registries")} value={overview.registries} href="/registries" />
                            <StatTile label={t("nav_partners")} value={overview.partners ?? "—"} href="/partners" />
                        </div>

                        <div className="grid gap-6 xl:grid-cols-3">
                            <div className="xl:col-span-2">
                                <Settings overview={overview} />
                            </div>
                            <div className="space-y-6">
                                <Panel>
                                    <SectionTitle
                                        title={t("last_24h")}
                                        href={overview.activity_24h ? "/activity" : undefined}
                                        linkLabel={t("view_activity")}
                                    />
                                    {overview.activity_24h ? (
                                        <div className="grid grid-cols-2 gap-4">
                                            <div>
                                                <div className="text-[13px] font-semibold uppercase tracking-wide text-gray-500">
                                                    {t("calls")}
                                                </div>
                                                <div className="text-[28px] font-semibold text-black">
                                                    {overview.activity_24h.calls.toLocaleString()}
                                                </div>
                                            </div>
                                            <div>
                                                <div className="text-[13px] font-semibold uppercase tracking-wide text-gray-500">
                                                    {t("failed")}
                                                </div>
                                                <div
                                                    className={`text-[28px] font-semibold ${
                                                        overview.activity_24h.failed ? "text-red-600" : "text-black"
                                                    }`}
                                                >
                                                    {overview.activity_24h.failed.toLocaleString()}
                                                </div>
                                            </div>
                                        </div>
                                    ) : (
                                        <p className="text-[14px] text-gray-500">{t("activity_not_recorded")}</p>
                                    )}
                                </Panel>
                                {overview.links.pm_portal || overview.links.cm_portal ? (
                                    <Panel>
                                        <SectionTitle title={t("portals")} hint={t("portals_hint")} />
                                        <PortalLinks links={overview.links} />
                                    </Panel>
                                ) : null}
                            </div>
                        </div>
                    </>
                )}
            </QueryView>
        </div>
    );
}
