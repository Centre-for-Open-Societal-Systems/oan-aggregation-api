"use client";

import Image from "next/image";
import { useTranslations } from "next-intl";
import { Link } from "@/i18n/navigation";

/** Shown when the backend answers 404 for the item a detail page asked for. */
export default function NotFound({ message, backHref, backLabel }: { message?: string; backHref: string; backLabel: string }) {
    const t = useTranslations();
    return (
        <div className="flex min-h-[50vh] flex-col items-center justify-center py-12 text-center">
            <Image src="/404.png" width={140} height={140} alt="" className="mb-6" priority />
            <h1 className="mb-3 text-[32px] font-semibold text-gray-900">{t("not_found_title")}</h1>
            {message ? <p className="mb-6 max-w-xl text-[16px] text-gray-600">{message}</p> : null}
            <Link
                href={backHref}
                className="rounded-full bg-gray-900 px-6 py-2.5 text-[16px] font-semibold text-white hover:bg-gray-800"
            >
                {backLabel}
            </Link>
        </div>
    );
}
