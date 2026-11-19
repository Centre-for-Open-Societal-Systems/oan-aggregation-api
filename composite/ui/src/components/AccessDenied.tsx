"use client";

import { useTranslations } from "next-intl";
import Image from "next/image";
import Link from "next/link";

export default function AccessDenied() {
  const t = useTranslations();

  return (
    <div className="flex flex-col items-center justify-center min-h-[60vh] py-12">
      <Image
        src="/forbidden.png"
        width={140}
        height={140}
        alt="Access denied illustration"
        className="mb-6"
        priority
      />

      <h1 className="mb-4 text-[40px] font-semibold leading-11.75 text-gray-900">
        {t("access_denied")}
      </h1>

      <p className="mb-6 text-[20px] font-light leading-6 text-gray-600 max-w-xl text-center">
        {t("forbidden_subtitle")}
      </p>

      <Link href="/">
        <button className="flex items-center gap-3 rounded-full bg-gray-900 px-6 py-2.5 text-[16px] font-semibold text-white hover:bg-gray-800 transition-colors">
          <Image
            src="/left_white_arrow.png"
            width={16}
            height={14}
            alt="left arrow"
            priority
          />
          {t("goBack")}
        </button>
      </Link>
    </div>
  );
}
