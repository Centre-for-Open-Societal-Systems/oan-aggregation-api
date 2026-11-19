import { NextRequest, NextResponse } from 'next/server';
import { cookies } from 'next/headers';

import { buildBackendAuthHeaders } from './auth-cookies';

export interface AuthContext {
    accessToken: string;
    backendHeaders: Record<string, string>;
}

export function requireAuth(req: NextRequest): AuthContext | NextResponse {
    const accessToken = req.cookies.get('X-Access-Token')?.value;
    const idToken = req.cookies.get('X-ID-Token')?.value;

    if (!accessToken && !idToken) {
        return NextResponse.json(
            {
                errors: [
                    {
                        code: 'G2P-AUT-LOGIN-REQUIRED',
                        message: 'Authentication required. No valid tokens found.',
                    },
                ],
            },
            { status: 401 }
        );
    }

    if (!accessToken && idToken) {
        return NextResponse.json(
            {
                errors: [
                    {
                        code: 'G2P-AUT-413',
                        message: 'Your access token exceeds the allowed size limit due to too many assigned roles. Please contact your administrator',
                    },
                ],
            },
            { status: 413 }
        );
    }

    if (!accessToken) {
        return NextResponse.json(
            { errors: [{ code: 'G2P-AUT-401', message: 'Unauthorized' }] },
            { status: 401 }
        );
    }

    return {
        accessToken,
        backendHeaders: buildBackendAuthHeaders(req.cookies, accessToken),
    };
}

export async function requireAuthFromCookies(): Promise<AuthContext | null> {
    const cookieStore = await cookies();
    const accessToken = cookieStore.get('X-Access-Token')?.value;
    const idToken = cookieStore.get('X-ID-Token')?.value;

    if (!accessToken && !idToken) {
        return null;
    }

    if (!accessToken && idToken) {
        return null;
    }

    if (!accessToken) {
        return null;
    }

    return {
        accessToken,
        backendHeaders: buildBackendAuthHeaders(cookieStore, accessToken),
    };
}
