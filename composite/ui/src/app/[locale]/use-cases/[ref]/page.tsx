"use client";

import { useParams } from "next/navigation";
import { routeParam } from "@/shared/utils/routeParam";
import { UseCaseDetailView } from "@/features/use-cases";

export default function UseCaseDetailPage() {
    const ref = routeParam(useParams<{ ref: string }>().ref);
    return <UseCaseDetailView key={ref} useCaseRef={ref} />;
}
