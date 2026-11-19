export const useCasesPath = () => "use-cases";
export const useCasePath = (ref: string) => `use-cases/${encodeURIComponent(ref)}`;

/** Console route of a use case's detail page. */
export const toUseCaseHref = (ref: string) => `/use-cases/${encodeURIComponent(ref)}`;
