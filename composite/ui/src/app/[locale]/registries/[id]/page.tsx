"use client";

import { useParams } from "next/navigation";
import { routeParam } from "@/shared/utils/routeParam";
import { RegistryDetailView } from "@/features/registries";

export default function RegistryDetailPage() {
    const id = routeParam(useParams<{ id: string }>().id);
    return <RegistryDetailView key={id} registryId={id} />;
}
