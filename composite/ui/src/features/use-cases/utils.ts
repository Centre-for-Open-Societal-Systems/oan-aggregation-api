import { unique } from "@/shared/utils/format";
import type { UseCase } from "./types";

/** Registries a use case reads from, in source order. */
export function registriesOf(useCase: Pick<UseCase, "sources">): string[] {
    return unique(useCase.sources.map((s) => s.controller));
}
