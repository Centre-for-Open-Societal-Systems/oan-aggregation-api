// Single source of server/runtime env — read at request time (K8s pod env, not Docker build).
export function getServerEnv() {
    return {
        backendUrl: (process.env.BACKEND_URL ?? "").replace(/\/+$/, ""),
        iamUrl: process.env.IAM_URL ?? "",
        loginProviderId: process.env.LOGIN_PROVIDER_ID ?? "",
        applicationMnemonic: process.env.APPLICATION_MNEMONIC || "agri-composite",
        cookieDomain: process.env.COOKIE_DOMAIN?.trim() ?? "",
        defaultLocale: process.env.DEFAULT_LOCALE ?? "",
    };
}

export type ServerEnv = ReturnType<typeof getServerEnv>;
