"use client";

import { useTranslations } from "next-intl";
import { ArrowLeft, CircleHelp } from "lucide-react";
import { Link } from "@/i18n/navigation";
import QueryView from "@/components/QueryView";
import {
    ChipList,
    CodeBlock,
    Collapsible,
    DefinitionList,
    Panel,
    SectionTitle,
    StatusBadge,
    tdClass,
    thClass,
} from "@/components/ui";
import { registryHref } from "@/features/registries/api";
import { formatValue } from "@/shared/utils/format";
import { useUseCase } from "../hooks";
import type { UseCaseDetail } from "../types";
import GrantedTo from "./GrantedTo";

function Header({ useCase }: { useCase: UseCaseDetail }) {
    const t = useTranslations();
    // use_case is "<name>@<major>": the reference partners call.
    const major = useCase.use_case.split("@")[1] ?? useCase.version.split(".")[0];
    return (
        <div className="space-y-3">
            <Link href="/use-cases" className="inline-flex items-center gap-1 text-[14px] font-medium text-[#ED7C22] hover:underline">
                <ArrowLeft size={14} />
                {t("nav_use_cases")}
            </Link>
            <div className="flex flex-wrap items-center gap-3">
                <h1 className="text-[24px] font-semibold text-black">{useCase.title || useCase.name}</h1>
                <StatusBadge status={useCase.status} />
            </div>
            <Panel>
                <DefinitionList
                    items={[
                        {
                            label: t("col_use_case"),
                            value: (
                                <span>
                                    <span className="font-mono">{useCase.name}</span>
                                    {" · "}
                                    {t("use_case_version", { version: useCase.version, major })}
                                </span>
                            ),
                        },
                        { label: t("col_purpose"), value: <span className="font-mono">{useCase.purpose || "—"}</span> },
                        { label: t("description"), value: useCase.description || "—" },
                        { label: t("consent"), value: useCase.consent?.required ? t("consent_required") : t("consent_not_required") },
                        { label: t("file"), value: <span className="font-mono">{useCase.file || "—"}</span> },
                    ]}
                />
            </Panel>
        </div>
    );
}

function Inputs({ useCase }: { useCase: UseCaseDetail }) {
    const t = useTranslations();
    const parameters = Object.entries(useCase.input.parameters ?? {});
    const batch = Object.entries(useCase.input.batch ?? {});

    return (
        <Panel>
            <SectionTitle title={t("inputs")} />
            <div className="space-y-4">
                <DefinitionList
                    items={[
                        {
                            label: t("subject_id_types"),
                            value: <ChipList items={useCase.input.subject?.id_types} mono />,
                        },
                        ...(batch.length
                            ? [
                                  {
                                      label: t("batch"),
                                      value: (
                                          <span className="font-mono text-[13px]">
                                              {batch.map(([k, v]) => `${k}: ${formatValue(v)}`).join(" · ")}
                                          </span>
                                      ),
                                  },
                              ]
                            : []),
                    ]}
                />
                <div>
                    <h3 className="mb-2 text-[13px] font-semibold uppercase tracking-wide text-gray-500">{t("parameters")}</h3>
                    {parameters.length === 0 ? (
                        <p className="text-[14px] text-gray-500">{t("no_parameters")}</p>
                    ) : (
                        <div className="overflow-x-auto">
                            <table className="w-full border-collapse">
                                <thead>
                                    <tr>
                                        <th className={thClass}>{t("col_name")}</th>
                                        <th className={thClass}>{t("col_type")}</th>
                                        <th className={thClass}>{t("col_required")}</th>
                                        <th className={thClass}>{t("col_default")}</th>
                                        <th className={thClass}>{t("description")}</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {parameters.map(([name, p]) => (
                                        <tr key={name} className="border-b border-gray-100 last:border-0">
                                            <td className={`${tdClass} font-mono`}>{name}</td>
                                            <td className={`${tdClass} font-mono`}>{p.type ?? "—"}</td>
                                            <td className={tdClass}>{p.required ? t("yes") : t("no")}</td>
                                            <td className={`${tdClass} font-mono`}>{formatValue(p.default)}</td>
                                            <td className={tdClass}>{p.description || "—"}</td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    )}
                </div>
            </div>
        </Panel>
    );
}

function Sources({ useCase }: { useCase: UseCaseDetail }) {
    const t = useTranslations();
    return (
        <Panel>
            <SectionTitle title={t("sources")} hint={t("sources_hint")} />
            <div className="overflow-x-auto">
                <table className="w-full border-collapse">
                    <thead>
                        <tr>
                            <th className={thClass}>{t("col_search")}</th>
                            <th className={thClass}>{t("col_registry")}</th>
                            <th className={thClass} title={t("requirement_help")}>
                                <span className="inline-flex items-center gap-1 whitespace-nowrap">
                                    {t("col_if_cant_answer")}
                                    <CircleHelp size={14} aria-label={t("requirement_help")} />
                                </span>
                            </th>
                            <th className={thClass}>{t("col_runs_after")}</th>
                            <th className={thClass}>{t("col_required_scopes")}</th>
                            <th className={thClass}>{t("col_optional_scopes")}</th>
                        </tr>
                    </thead>
                    <tbody>
                        {useCase.sources.map((s) => (
                            <tr key={s.id} className="border-b border-gray-100 last:border-0">
                                <td className={`${tdClass} font-mono`}>{s.id}</td>
                                <td className={tdClass}>
                                    <Link href={registryHref(s.controller)} className="font-mono hover:text-[#ED7C22] hover:underline">
                                        {s.controller}
                                    </Link>
                                </td>
                                <td className={tdClass}>
                                    <StatusBadge
                                        status={s.requirement}
                                        label={t.has(`requirement_${s.requirement}`) ? t(`requirement_${s.requirement}`) : s.requirement}
                                        title={t("requirement_help")}
                                    />
                                </td>
                                <td className={tdClass}>
                                    <ChipList items={s.depends_on} mono />
                                </td>
                                <td className={tdClass}>
                                    <ChipList items={s.scopes} mono />
                                </td>
                                <td className={tdClass}>
                                    <ChipList items={s.optional_scopes} mono />
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
        </Panel>
    );
}

function ExpressionTable({ title, entries }: { title: string; entries: [string, string][] }) {
    const t = useTranslations();
    if (entries.length === 0) return null;
    return (
        <div>
            <h3 className="mb-2 text-[13px] font-semibold uppercase tracking-wide text-gray-500">{title}</h3>
            <div className="overflow-x-auto">
                <table className="w-full border-collapse">
                    <thead>
                        <tr>
                            <th className={thClass}>{t("col_field")}</th>
                            <th className={thClass}>{t("col_expression")}</th>
                        </tr>
                    </thead>
                    <tbody>
                        {entries.map(([field, expr]) => (
                            <tr key={field} className="border-b border-gray-100 last:border-0">
                                <td className={`${tdClass} whitespace-nowrap font-mono text-[13px]`}>{field}</td>
                                <td className={`${tdClass} break-all font-mono text-[13px] text-gray-700`}>{expr}</td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>
        </div>
    );
}

function OutputFields({ useCase }: { useCase: UseCaseDetail }) {
    const t = useTranslations();
    const mapping = Object.entries(useCase.mapping ?? {});
    const derived = Object.entries(useCase.derived ?? {});
    return (
        <Panel>
            <SectionTitle title={t("output_fields")} hint={t("output_fields_count", { count: useCase.output_fields.length })} />
            <ChipList items={useCase.output_fields} mono />
            {mapping.length || derived.length ? (
                <div className="mt-5 border-t border-gray-100 pt-4">
                    <Collapsible title={t("mapping_and_derived")}>
                        <div className="space-y-5">
                            <ExpressionTable title={t("mapping")} entries={mapping} />
                            <ExpressionTable title={t("derived")} entries={derived} />
                        </div>
                    </Collapsible>
                </div>
            ) : null}
        </Panel>
    );
}

export default function UseCaseDetailView({ useCaseRef }: { useCaseRef: string }) {
    const t = useTranslations();
    const query = useUseCase(useCaseRef);

    return (
        <QueryView query={query} notFound={{ backHref: "/use-cases", backLabel: t("back_to_use_cases") }}>
            {(useCase) => (
                <div className="space-y-6">
                    <Header useCase={useCase} />
                    <Inputs useCase={useCase} />
                    <Sources useCase={useCase} />
                    <OutputFields useCase={useCase} />
                    <div className="grid gap-6 lg:grid-cols-2">
                        <Panel>
                            <SectionTitle title={t("col_granted_to")} hint={t("granted_to_hint")} />
                            <GrantedTo useCase={useCase} />
                        </Panel>
                        <Panel>
                            <SectionTitle title={t("execution")} />
                            <DefinitionList
                                items={[
                                    {
                                        label: t("partial_response"),
                                        value: <span className="font-mono">{useCase.partial_response || "—"}</span>,
                                    },
                                    {
                                        label: t("rate_per_partner"),
                                        value: <span className="font-mono">{useCase.rate_per_partner || t("unlimited")}</span>,
                                    },
                                ]}
                            />
                        </Panel>
                    </div>
                    {useCase.source_yaml ? (
                        <Panel>
                            <Collapsible title={t("source_yaml", { file: useCase.file || "YAML" })}>
                                <CodeBlock>{useCase.source_yaml}</CodeBlock>
                            </Collapsible>
                        </Panel>
                    ) : null}
                </div>
            )}
        </QueryView>
    );
}
