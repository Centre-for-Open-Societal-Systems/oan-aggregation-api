import { getServerEnv } from "./env-config";

// Backend configuration (use in API routes).
export function getBackendConfig() {
    const env = getServerEnv();

    return {
        backendUrl: env.backendUrl,
        iamUrl: env.iamUrl,
        loginProviderId: env.loginProviderId,
        applicationMnemonic: env.applicationMnemonic,
        cookieDomain: env.cookieDomain,
    };
}
