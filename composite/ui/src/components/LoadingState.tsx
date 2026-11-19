"use client";

import Image from "next/image";
import { useTranslations } from "next-intl";

type LoadingStateProps = {
  label?: string;
  fullScreen?: boolean;
  compact?: boolean;
  loaderOnly?: boolean;
};

export default function LoadingState({
  label,
  fullScreen = false,
  compact = false,
  loaderOnly = false,
}: LoadingStateProps) {
  const t = useTranslations();
  const text = label ?? t("loading");

  if (loaderOnly) {
    return (
      <div className="flex items-center justify-center py-12 px-4 min-h-50 bg-white">
        <Image
          src="/loading.gif"
          alt={text}
          width={48}
          height={48}
          priority
          unoptimized
          className="rounded-[10px]"
        />
      </div>
    );
  }

  return (
    <div
      className={
        fullScreen
          ? "fixed inset-0 z-50 flex items-center justify-center bg-white"
          : compact
            ? "flex items-center justify-center p-6 min-h-30 bg-white"
            : "flex items-center justify-center py-12 px-4 min-h-50 bg-white"
      }
    >
      <div className="flex flex-col items-center gap-4">
        <Image
          src="/loading.gif"
          alt={text}
          width={48}
          height={48}
          priority
          unoptimized
          className={compact ? "rounded-[10px]" : ""}
        />
        <p className="text-gray-600 text-[16px] m-0">
          {text}
        </p>
      </div>
    </div>
  );
}
