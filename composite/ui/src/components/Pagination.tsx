"use client";

import Image from "next/image";

interface PaginationProps {
  page: number;
  pageSize: number;
  total: number;
  /** total is a lower bound (the backend stopped counting): shown as "total+". */
  totalCapped?: boolean;
  onPageChange: (page: number) => void;
}

export default function Pagination({
  page,
  pageSize,
  total,
  totalCapped = false,
  onPageChange,
}: PaginationProps) {
  const start = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const end = Math.min(page * pageSize, total);
  const totalPages = Math.max(1, Math.ceil(total / pageSize) || 1);

  return (
    <div className="flex items-center justify-end gap-3">
      <span className="text-[16px] text-(--color-text-muted)">
        {start} - {end} of {total}{totalCapped ? "+" : ""}
      </span>

      <button
        type="button"
        className="flex items-center justify-center min-w-10 h-8.5 px-3 rounded-[10px] bg-(--color-accent) text-black text-[16px] font-medium cursor-pointer transition-colors duration-150 disabled:bg-(--color-grey) disabled:text-white disabled:opacity-50 disabled:cursor-not-allowed"
        disabled={page <= 1}
        onClick={() => onPageChange(page - 1)}
        aria-label="Previous page"
      >
        <Image
          src="/arrow_back_01.png"
          width={16}
          height={16}
          alt="Previous"
          className="block"
        />
      </button>

      <button
        type="button"
        className="flex items-center justify-center min-w-10 h-8.5 px-3 rounded-[10px] bg-(--color-accent) text-black text-[16px] font-medium cursor-pointer transition-colors duration-150 disabled:bg-(--color-grey) disabled:text-white disabled:opacity-50 disabled:cursor-not-allowed"
        disabled={page >= totalPages}
        onClick={() => onPageChange(page + 1)}
        aria-label="Next page"
      >
        <Image
          src="/arrow_next_01.png"
          width={16}
          height={16}
          alt="Next"
          className="block"
        />
      </button>
    </div>
  );
}
