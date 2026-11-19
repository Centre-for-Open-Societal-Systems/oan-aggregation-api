import "server-only";
import { NextRequest, NextResponse } from "next/server";
import { getBackendConfig } from "./backend-config";
import { requireAuth } from "./requireAuth";
import { applyBackendSetCookies } from "./auth-cookies";

/**
 * Forwards a GET to the composite backend with the user's IAM session (Bearer token, auth
 * cookies and CSRF token — see auth-cookies), and passes the backend's status and JSON body
 * back unchanged. Refreshed / cleared auth cookies from the backend are relayed to the browser.
 *
 * Unlike the Master Data proxy there is no request/response envelope: the composite admin API
 * is plain REST (GET, JSON in the body, errors as `{"error": {"code", "message"}}`).
 */
export async function proxyGetToBackend(req: NextRequest, targetEndpoint: string) {
	const auth = requireAuth(req);
	if (auth instanceof NextResponse) return auth;

	const { backendUrl } = getBackendConfig();
	if (!backendUrl) {
		return NextResponse.json(
			{ statusText: "BACKEND_URL is not configured", code: 500 },
			{ status: 500 },
		);
	}

	try {
		const response = await fetch(`${backendUrl}${targetEndpoint}`, {
			method: "GET",
			headers: auth.backendHeaders,
			cache: "no-store",
		});

		let body: unknown;
		try {
			body = await response.json();
		} catch {
			body = response.ok
				? { statusText: "Empty response from backend", code: response.status }
				: { statusText: response.statusText || `Error ${response.status}`, code: response.status };
		}

		const result = NextResponse.json(body, { status: response.status });
		applyBackendSetCookies(response, result);
		return result;
	} catch (e) {
		return NextResponse.json(
			{ statusText: e instanceof Error ? e.message : "Backend unreachable", code: 502 },
			{ status: 502 },
		);
	}
}
