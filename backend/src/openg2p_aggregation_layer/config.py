from openg2p_fastapi_common.config import Settings as BaseSettings
from pydantic_settings import SettingsConfigDict

from . import __version__


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="aggregation_layer_", env_file=".env", extra="allow"
    )

    openapi_title: str = "OpenG2P Aggregation Layer"
    openapi_description: str = """
        Aggregation Layer for OpenG2P.

        One partner call for a beneficiary's data across several registries
        (an OpenG2P Beneficiary-360 request), gated on the subject's consent
        (held by the Consent Manager). The Beneficiary-360 response comes back
        inside a signed DCI on-search: in the HTTP response (sync, the
        default) or POSTed to the partner's callback (async). No database.
        """
    openapi_version: str = __version__

    # ── Signing key ─────────────────────────────────────────────────────────
    # The aggregator's OWN key, not the CM's. It signs the consent object and
    # the envelope on every registry hop, and the on-search sent to the
    # partner. Its public half is registered in Partner Management under
    # PARTNER_{aggregator_sender_id} - see scripts/register-aggregator.py.
    # .p12 first, then a PEM string, then an ephemeral key (dev only: with
    # more than one worker each gets a different key and registries reject
    # most hops).
    signing_p12_path: str = ""
    signing_p12_password: str = ""
    signing_private_key_pem: str = ""
    signing_kid: str = "agg-2026-01"
    signing_algorithm: str = "EdDSA"  # fallback hint only; key type wins
    signing_is_demo: bool = False

    # ── Consent Manager (reached over HTTP only) ────────────────────────────
    # Every consent decision is the CM's: validate a consent object, read a
    # consent's status. Only the CM's generic APIs are used - see
    # docs/CM-API-CONTRACT.md.
    cm_base_url: str = "http://localhost:8000"
    cm_timeout: float = 15.0
    # Service-to-service auth: Keycloak client-credentials for the
    # `aggregation-layer` client. Optional: /validate and the consent status
    # are open partner APIs today. A static token, when set, is sent verbatim
    # (dev).
    cm_token_url: str = ""
    cm_client_id: str = "aggregation-layer"
    cm_client_secret: str = ""
    cm_static_token: str = ""
    # This service's controller id at the CM: a partner's stored consent
    # (``authorize.consent_id``) must carry a grant for it.
    cm_data_controller: str = "aggregation_layer"

    # ── Partner authentication for ``authorize.consent_id`` ─────────────────
    # A consent id is only a reference, so the caller must also prove it is the
    # partner the consent was given to: a bearer token from the CM's partner
    # realm whose ``partner_id`` claim names that partner. Empty issuer = the
    # consent_id route is refused (consent_jws, which the partner signs, needs
    # no token).
    partner_auth_issuer: str = ""
    partner_auth_jwks_url: str = ""
    partner_auth_audience: str = ""
    partner_auth_partner_claim: str = "partner_id"

    # ── Caller authentication (Keycloak / OIDC bearer) ──────────────────────
    # Used by the staff routes (realm ``staff``).
    auth_enabled: bool = True
    auth_issuer: str = ""
    auth_jwks_url: str = ""
    auth_audience: str = ""
    auth_algorithms: list[str] = ["RS256", "ES256", "EdDSA"]
    subject_default_id_type: str = "national_id"

    # ── Staff search (POST /aggregation/v1/staff/registry/search) ───────────
    # Registry staff look a beneficiary up across every registry that has a
    # staff_api in the catalog, with no consent, no consent object and no OTP.
    # The caller is a staff user (token from the auth_* realm above) holding
    # one of these roles (realm or client). Empty refuses every caller: the
    # route exists but nobody may use it until a role is named.
    staff_search_roles: list[str] = ["AGGREGATION_STAFF_VIEWER"]
    # This service's own Keycloak client for the registries' staff-apis
    # (client_credentials). Its service account needs a view-only role
    # (e.g. Data Supervisor) on each registry's staff-portal client. A catalog
    # entry's staff_api.auth overrides these for a registry on another Keycloak.
    staff_api_token_url: str = ""
    staff_api_client_id: str = "aggregation-layer-staff"
    staff_api_client_secret: str = ""
    # Per staff-api call; a catalog entry's staff_api.timeout_sec overrides it.
    staff_api_timeout: float = 20.0
    # Register ids, tab lists and tab sections are read from the staff-api and
    # kept this long, so a tab or section added in a registry shows up without
    # a restart. A tab id not in the cached list forces one early refresh.
    staff_api_metadata_ttl_sec: int = 600
    # At most this many staff-api calls in flight per registry (per worker);
    # a call that cannot start within staff_api_queue_timeout_sec fails that
    # registry with "registry_busy" instead of piling up.
    staff_api_max_concurrency: int = 20
    staff_api_queue_timeout_sec: float = 5.0
    # Staff-api lists (register search, metadata) come in pages: at most this
    # many are read per call. A search cut short by the cap is reported as
    # incomplete, never as "not found".
    staff_api_max_pages: int = 20
    # How long a search's searchId can open tabs. A tab request is accepted
    # only for a record that search returned, to the staff user who ran it.
    staff_search_ttl_sec: int = 1800
    # The searchId is a ticket with a MAC (nothing is stored). Its key is this
    # secret when set, else derived from the signing key: every pod must use
    # the same key, or a tab may land on a pod that cannot read the ticket.
    staff_search_ticket_secret: str = ""
    # Rows per page of a list section in a tab answer (pageSize default / cap).
    staff_tab_page_size: int = 50
    staff_tab_max_page_size: int = 200
    # Staff requests (search + tab) per user per minute, per worker; 0 = off.
    # A gateway limit (e.g. Kong) is the cluster-wide one; this is the backstop.
    staff_rate_limit_per_minute: int = 120

    # ── Registry catalog ────────────────────────────────────────────────────
    # The YAML file that lists every registry this service may query: where
    # it is, which CM binding each hop spends, how its record maps onto the
    # Beneficiary-360 response and which fields may leave this service. Loaded
    # and validated at startup; an invalid file stops the service. Adding a
    # registry is an entry there, never a code change. See registry_catalog.py
    # and deploy/registries.yaml.
    registry_catalog_path: str = ""

    # ── Aggregator: cross-registry fetch, sync or async ─────────────────────
    # One partner call for a beneficiary across several registries, answered
    # in the response (sync) or on a callback (async). The registries' own
    # /dci/registry/sync/search is untouched; the aggregator calls each of them
    # as an ordinary partner, so consent enforcement still applies per hop.
    # Identity the aggregator signs its internal consent objects with. Its
    # public key must be registered in Partner Management and it needs a CM
    # binding per registry on lawful_basis legitimate_interest — see
    # scripts/register-aggregator.py.
    aggregator_issuer: str = "aggregation-layer"
    # MUST map to the Partner Management partner holding the aggregator's public
    # key. The registry derives that reference from the DCI header as
    #   PARTNER_{sender_id.replace("-","_").upper()}
    # (keymanager_helper.partner_reference_id), so "aggregation-layer" resolves to
    # PARTNER_AGGREGATION_LAYER. Change one without the other and every internal hop
    # fails with signature_invalid / REQUEST_VALIDATION_ERROR.
    aggregator_sender_id: str = "aggregation-layer"
    aggregator_purpose_code: str = "loan_origination"
    # Validity of each hop's consent object.
    aggregator_consent_validity_sec: int = 300
    # Defaults for a registry hop; a catalog entry may override both.
    aggregator_page_size: int = 10
    aggregator_registry_timeout: float = 30.0
    # Async route: the one callback attempt (no retry, nothing is kept).
    aggregator_callback_timeout: float = 30.0
