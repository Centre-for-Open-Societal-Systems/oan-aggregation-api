import { hasLocale } from 'next-intl';
import createMiddleware from 'next-intl/middleware';
import { NextRequest } from 'next/server';

import { getServerEnv } from '@/app/api/_lib/env-config';
import { routing } from './i18n/routing';

export default function middleware(request: NextRequest) {
    // DEFAULT_LOCALE is read per request (pod env), falling back to the routing default.
    const configured = getServerEnv().defaultLocale;
    const defaultLocale = hasLocale(routing.locales, configured) ? configured : routing.defaultLocale;
    const handleRequest = createMiddleware({ ...routing, defaultLocale });

    return handleRequest(request);
}

export const config = {
    matcher: ['/((?!api|_next|.*\\..*).*)'],
};
