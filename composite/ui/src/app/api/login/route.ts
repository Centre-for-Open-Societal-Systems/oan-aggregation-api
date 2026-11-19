import { NextRequest, NextResponse } from 'next/server';
import { getBackendConfig } from '../_lib/backend-config';

/**
 * This console's public origin as the browser sees it. Behind the ingress, req.nextUrl.origin is
 * the pod's own address (http://0.0.0.0:3000), so use the forwarded host and protocol.
 */
function publicOrigin(req: NextRequest): string {
    const host = (req.headers.get('x-forwarded-host') ?? req.headers.get('host') ?? '').split(',')[0].trim();
    const proto = (req.headers.get('x-forwarded-proto') ?? req.nextUrl.protocol.replace(':', '')).split(',')[0].trim();
    return host ? `${proto || 'https'}://${host}` : req.nextUrl.origin;
}

/**
 * Where to send the user after login, as an absolute URL on this console (IAM redirects there
 * from its own host, so a relative path would land on IAM). Accepts a relative path ("/x", not
 * "//x" or "/\x") or an absolute URL on this console's origin; anything else becomes the home page.
 */
function safeRedirectUri(req: NextRequest, value: string | null): string {
    const origin = publicOrigin(req);
    if (!value) return `${origin}/`;
    if (value.startsWith('/')) {
        return value.startsWith('//') || value.startsWith('/\\') ? `${origin}/` : `${origin}${value}`;
    }
    try {
        const url = new URL(value);
        return url.origin === origin ? url.toString() : `${origin}/`;
    } catch {
        return `${origin}/`;
    }
}

export async function GET(req: NextRequest) {
    const redirectUri = safeRedirectUri(req, req.nextUrl.searchParams.get('redirect_uri'));

    const backendConfig = getBackendConfig();
    if (!backendConfig.iamUrl || !backendConfig.loginProviderId) {
        return NextResponse.json(
            { error: 'Login is not configured: IAM_URL and LOGIN_PROVIDER_ID must be set' },
            { status: 500 },
        );
    }
    const iamUrl = `${backendConfig.iamUrl}${"/auth/start_authentication_transaction"}`;

    const url = `${iamUrl}?id=${encodeURIComponent(backendConfig.loginProviderId)}&redirect_uri=${encodeURIComponent(redirectUri)}`;

    let data: { redirectUrl?: string };
    try {
        const res = await fetch(url, {
            method: 'POST',
            headers: { accept: 'application/json' },
            body: ''
        });
        data = await res.json();
    } catch {
        return NextResponse.json({ error: 'IAM did not answer the login request' }, { status: 502 });
    }

    if (!data?.redirectUrl) {
        return NextResponse.json({ error: 'Failed to initiate auth' }, { status: 500 });
    }

    return NextResponse.redirect(data.redirectUrl);
}
