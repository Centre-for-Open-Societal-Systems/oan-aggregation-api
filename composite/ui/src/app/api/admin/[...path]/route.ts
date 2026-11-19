import { NextRequest, NextResponse } from "next/server";
import { proxyGetToBackend } from "@/app/api/_lib/backend-proxy";

/**
 * Read-only proxy for the composite admin API (`GET /composite/v1/admin/*` on BACKEND_URL).
 *
 * Only these paths are forwarded:
 *   overview · use-cases · use-cases/{ref} · registries · registries/{id}/data-scopes ·
 *   partners · activity (with limit, offset, partner, use_case, outcome).
 * Authorisation (`composite:view`) is enforced by the backend; its 401 / 403 / 404 are passed
 * through as-is.
 */
const ADMIN_BASE = "/composite/v1/admin";
const ACTIVITY_PARAMS = ["limit", "offset", "partner", "use_case", "outcome"];

function isAllowed(path: string[]): boolean {
	const [head, id, tail] = path;
	switch (path.length) {
		case 1:
			return ["overview", "use-cases", "registries", "partners", "activity"].includes(head);
		case 2:
			return head === "use-cases" && !!id;
		case 3:
			return head === "registries" && !!id && tail === "data-scopes";
		default:
			return false;
	}
}

export async function GET(
	request: NextRequest,
	{ params }: { params: Promise<{ path: string[] }> },
) {
	const { path } = await params;
	if (!isAllowed(path)) {
		return NextResponse.json(
			{ error: { code: "unknown_admin_path", message: `Unknown admin path: ${path.join("/")}` } },
			{ status: 404 },
		);
	}

	let endpoint = `${ADMIN_BASE}/${path.map(encodeURIComponent).join("/")}`;
	if (path[0] === "activity") {
		const query = new URLSearchParams();
		for (const key of ACTIVITY_PARAMS) {
			const value = request.nextUrl.searchParams.get(key);
			if (value) query.set(key, value);
		}
		const qs = query.toString();
		if (qs) endpoint += `?${qs}`;
	}

	return proxyGetToBackend(request, endpoint);
}
