export const registriesPath = () => "registries";
export const dataScopesPath = (id: string) => `registries/${encodeURIComponent(id)}/data-scopes`;

/** Console route of a registry's detail page. */
export const registryHref = (id: string) => `/registries/${encodeURIComponent(id)}`;
