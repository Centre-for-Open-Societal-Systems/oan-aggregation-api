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

        One partner call naming fields from several registries, gated on the
        subject's consent (held by the Consent Manager) and, where the
        partner's policy asks for it, an OTP. The aggregated record is POSTed
        to the partner's callback as a signed DCI on-search.
        """
    openapi_version: str = __version__

    # ── Database (the aggregator's own; it never reads the CM's) ────────────
    db_driver: str = "postgresql+asyncpg"
    db_username: str = "postgres"
    db_password: str = "postgres"
    db_hostname: str = "localhost"
    db_port: int = 5432
    db_dbname: str = "aggregation_layer_db"

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
    # Every consent decision is the CM's: validate, raise a consent request,
    # read its status, read a consent's status. Only the CM's generic APIs are
    # used - see docs/CM-API-CONTRACT.md. The CM holds nothing for this
    # service; what the subject authorised per registry hop is kept here
    # (models/grant.py).
    cm_base_url: str = "http://localhost:8000"
    cm_timeout: float = 15.0
    # Service-to-service auth: Keycloak client-credentials for the
    # `aggregation-layer` client. Its service account needs the CM admin role
    # (CONSENT_MANAGER_ADMIN): that is what the CM's policy read and partner
    # list require today. A static token, when set, is sent verbatim (dev).
    cm_token_url: str = ""
    cm_client_id: str = "aggregation-layer"
    cm_client_secret: str = ""
    cm_static_token: str = ""
    # CM partner id per partner audience, as JSON: {"komal-aggregator": "<uuid>"}.
    # The CM's /validate answers with the consent, not the partner, so the
    # partner is read from the (CM-verified) object's ``aud`` and mapped to its
    # CM id here. An audience missing from the map is looked up once in the
    # CM's partner list (GET /consent/v1/partners) and cached.
    cm_partner_ids: str = ""
    # How often consent state is read back from the CM: raised consent
    # requests (approved / denied / expired) and the consent each in-flight
    # aggregation stands on (withdrawn / expired). Nothing is pushed by the
    # CM, so this is the delay between a decision there and its effect here.
    # 0 disables the loop; the reaper still runs one pass per invocation.
    cm_poll_interval_sec: int = 15
    # Run the poll loop inside the API process (dev). In production set it
    # false and let `python -m openg2p_aggregation_layer.worker` poll.
    cm_poll_in_app: bool = True
    # A poller that dies holding a row releases it after this long.
    cm_poll_claim_timeout_sec: int = 120

    # ── Caller authentication (Keycloak / OIDC bearer) ──────────────────────
    # Used by the subject-facing routes (verify-otp, status). Same realm the
    # CM's beneficiary API uses, so a farmer's token works on both.
    auth_enabled: bool = True
    auth_issuer: str = ""
    auth_jwks_url: str = ""
    auth_audience: str = ""
    auth_algorithms: list[str] = ["RS256", "ES256", "EdDSA"]
    auth_admin_role: str = "AGGREGATION_LAYER_ADMIN"
    subject_default_id_type: str = "national_id"

    # ── Aggregator: async, OTP-gated, cross-registry fetch ──────────────────
    # One partner call naming fields from several registries, answered on a
    # callback once the subject has entered an OTP. The registries' own
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
    # Validity of each hop's consent object AND of the per-registry grant
    # this service records (models/grant.py).
    aggregator_consent_validity_sec: int = 300
    # A per-registry grant is reused, rather than minted again, only while at
    # least this much of its validity is left. A fan-out that retries must not
    # start on a grant that lapses under it, so this is well above one hop.
    aggregator_grant_reuse_min_remaining_sec: int = 150
    # When the subject has no consent for this partner, raise one for them and
    # park the aggregation until it is approved, instead of refusing the seek
    # with no_subject_consent. False restores the two-step behaviour where the
    # partner must obtain consent out of band before it may call.
    #
    # Only reachable when the CM runs with subject_consent_required=true -
    # otherwise /validate permits on the policy ceiling and never answers
    # no_subject_consent. On approval the partner's object is validated again
    # to learn what was granted, so the approval must land within the CM's
    # replay window (replay_freshness_window_sec, 300s by default) of the
    # object's issued_at; later, the row is rejected and the partner re-seeks.
    aggregator_raise_consent: bool = True
    # Where the subject's consent screen lives, used to build the consent_url
    # the partner redirects them to. Empty omits the field rather than guessing.
    consent_ui_base_url: str = "http://localhost:3002"
    aggregator_page_size: int = 10
    aggregator_registry_timeout: float = 30.0
    aggregator_callback_timeout: float = 30.0
    # reg_type/reg_record_type on the aggregated on-search. The record spans
    # registries, so neither can honestly be one registry's value.
    aggregator_reg_type: str = "spdci-extensions-dci:AggregatedRecord"
    aggregator_reg_record_type: str = "spdci-extensions-dci:AggregatedRecord"
    # Where each registry lives and which CM binding to spend there, as JSON:
    #   {"farmer": {"url": "...", "audience": "...", "controller_id": "...",
    #               "reg_type": "...", "reg_record_type": "...",
    #               "receiver_id": "...", "id_type": "functional_id"}}
    # Keys must match the registry prefixes used in services/field_catalog.py.
    aggregator_registries: str = ""

    # ── Kafka (the fan-out and delivery queues) ────────────────────────────
    #
    # Without a broker, verify_otp spawns asyncio.create_task() and the
    # registry fan-out runs inside the API worker that happened to serve the
    # request. Fifty subjects entering an OTP in the same minute means fifty
    # concurrent, unbounded fan-outs competing with the portal's own request
    # handling, and a partner whose callback is down loses the data outright:
    # one POST is attempted, and there is nothing to retry it.
    #
    # With a broker the HTTP handler only publishes, which is bounded and
    # fast. Workers consume at a rate the registries can survive, and delivery
    # is a separate topic so a slow partner cannot hold a registry worker.
    #
    # False keeps the in-process behaviour, so the demo stack runs unchanged
    # with no broker to start. See KAFKA.md.
    kafka_enabled: bool = False
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_client_id: str = "openg2p-aggregation-layer"
    # Topics are derived from this prefix unless named explicitly, following
    # the platform's Audit Manager convention (openg2p.audit.events/.dlq).
    kafka_topic_prefix: str = "openg2p"
    kafka_topic_fanout: str = ""    # <prefix>.aggregation.fanout
    kafka_topic_delivery: str = ""  # <prefix>.aggregation.delivery
    # Base name only. The retry topics are one PER BACKOFF STEP, derived from
    # this and kafka_retry_backoff_seconds: <base>.10s, <base>.60s, and so on.
    # See the Settings.retry_tiers docstring for why a single one cannot work.
    kafka_topic_retry: str = ""     # <prefix>.aggregation.delivery.retry
    kafka_topic_dlq: str = ""       # <prefix>.aggregation.dlq
    kafka_group_fanout: str = "openg2p-aggregation-fanout"
    kafka_group_delivery: str = "openg2p-aggregation-delivery"
    # Base name; each tier gets "<base>-<delay>s" so their offsets stay
    # independent and a busy tier cannot hold up an idle one.
    kafka_group_retry: str = "openg2p-aggregation-retry"
    # Partitions cap how many fan-outs can run at once across the whole
    # deployment, so this is the load limit the registries actually feel.
    # 12 mirrors the Audit Manager default.
    kafka_topic_partitions: int = 12
    kafka_topic_replication: int = 1
    # Idempotent CREATE TOPICS on startup, like the Audit Manager's topicInit
    # Job. Harmless when the topics exist; set false where the broker forbids
    # client-side topic creation.
    kafka_topic_init: bool = True
    # The delivery topic carries the aggregated record itself, because
    # retrying a callback must not mean querying the registries a second time.
    # That is personal data at rest in Kafka for as long as the topic keeps
    # it, so the retention is deliberately short and set on creation.
    kafka_delivery_retention_ms: int = 3_600_000      # 1 hour
    kafka_dlq_retention_ms: int = 604_800_000         # 7 days
    # Run the consumers inside the API process. True is the single-process
    # dev default; in production run `python -m openg2p_aggregation_layer.worker`
    # and set this false, so registry fan-out cannot compete with the portal's
    # own event loop at all.
    kafka_consumers_in_app: bool = True
    # In-flight work per consumer. The ceiling on registry load is this times
    # the number of consumer instances, bounded by the partition count.
    kafka_fanout_concurrency: int = 4
    kafka_delivery_concurrency: int = 8
    # How long a publish may block the HTTP handler before it gives up and
    # falls back to an in-process task. The subject has already burnt their
    # OTP by this point; refusing the request would lose the authorisation.
    kafka_producer_timeout: float = 5.0
    # Callback retries. Attempt 1 is immediate; the rest are spaced by this
    # list, then the message goes to the DLQ.
    kafka_delivery_max_attempts: int = 5
    kafka_retry_backoff_seconds: str = "10,60,300,900"
    # A row left "fetching"/"delivering" by a worker that died is re-queued
    # after this long by `python -m openg2p_aggregation_layer.reap`.
    kafka_claim_timeout_sec: int = 600

    # ── OTP (subject's real-time authorisation for an aggregated fetch) ─────
    otp_length: int = 6
    otp_ttl_sec: int = 300
    otp_max_attempts: int = 3
    # Mixed into the OTP hash so a stolen database row cannot be brute-forced
    # against a 6-digit space offline. Set this per environment.
    otp_salt: str = "change-me-per-environment"
    # DEV ONLY. Exposes GET /consent/v1/aggregation/{id}/otp. There is no SMS or
    # email gateway in this stack, so the default sender logs the code; this
    # endpoint reports the OTP state alongside it.
    otp_debug_enabled: bool = False

    # Which OTP backend to use. "fayda" applies Fayda's rules in-process via
    # utils/fayda_otp; "internal" is the same without the Fayda framing.
    # Switching is config only - nothing above services/otp_provider.py changes,
    # which is also how a real Fayda deployment would plug in.
    otp_provider: str = "fayda"
    # ── OTP publishing (S3 / MinIO drop box) ──────────────────────
    # Mirrors the OAN Gen 1 / A2C arrangement: each issued OTP is written to a
    # bucket under otp/ so a tester can read it without an SMS gateway. DEV
    # ONLY - anything that can read the bucket reads a live OTP beside the
    # individual it belongs to. Off by default; see services/otp_publisher.py.
    otp_publish_enabled: bool = False
    otp_publish_bucket: str = "a2c-webhook"
    otp_publish_prefix: str = "otp/"
    otp_publish_region: str = "ap-south-1"
    # Set to point at MinIO or any S3-compatible store; empty resolves AWS.
    otp_publish_endpoint_url: str = ""
    otp_publish_access_key: str = ""
    otp_publish_secret_key: str = ""

    # ── Fayda (OAN mock, g2p_ati_consent_mgt/utils/mock_fayda_otp_api.py) ───
    # Base URL with no path: the endpoints are /requestData and /getDataAuth.
    fayda_base_url: str = ""
    fayda_client_id: str = "demo-client"
    fayda_client_secret: str = "demo-secret"
    fayda_version: str = "1.0"
    # env and domain_uri must match the server's own MOCK_FAYDA_ENV and
    # MOCK_FAYDA_DOMAIN_URI exactly, or it answers 400 before anything else.
    fayda_env: str = "prod"
    fayda_domain_uri: str = "fayda.et"
    fayda_identifier_type: str = "FIN"
    fayda_otp_channel: str = "PHONE"
    # Only used to render the masked-mobile line in the log, so an
    # operator can see WHERE a real Fayda would have sent the code.
    fayda_demo_phone: str = "0911000055"
    fayda_timeout: float = 20.0
    # Fayda keys on the individual's Fayda/FIN number, not a Keycloak username,
    # so a demo subject has to be mapped onto one.
    # JSON: {"staff": "6140798523698702"}
    fayda_individual_id_map: str = ""

    @property
    def fayda_individual_ids(self) -> dict:
        import json
        import logging

        raw = (self.fayda_individual_id_map or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(self.logging_default_logger_name).error(
                "fayda_individual_id_map is not valid JSON (%s); subjects will "
                "be passed through unmapped", exc)
            return {}
        return parsed if isinstance(parsed, dict) else {}


    @property
    def cm_partner_id_map(self) -> dict:
        """``cm_partner_ids`` parsed; a bad value logs and yields {}."""
        import json
        import logging

        raw = (self.cm_partner_ids or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(self.logging_default_logger_name).error(
                "aggregation_layer_cm_partner_ids is not valid JSON (%s); partners "
                "will be looked up in the CM", exc)
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @property
    def aggregator_registry_map(self) -> dict:
        """``aggregator_registries`` parsed, with a safe empty default.

        A bad value must not take the whole service down at import time, so a
        parse failure logs and yields {} — the aggregator then reports
        'not_configured' per registry instead of 500ing.
        """
        import json
        import logging

        raw = (self.aggregator_registries or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(self.logging_default_logger_name).error(
                "aggregation_layer_aggregator_registries is not valid JSON (%s); "
                "the aggregator will report every registry as not_configured", exc)
            return {}
        return parsed if isinstance(parsed, dict) else {}

    # ── Kafka derived values ────────────────────────────────────────────────
    #
    # Each topic name is overridable, but defaults to a suffix on the prefix so
    # a deployment only has to set one variable to namespace the whole service.

    def _topic(self, explicit: str, suffix: str) -> str:
        return (explicit or "").strip() or "%s.%s" % (
            self.kafka_topic_prefix.rstrip("."), suffix)

    @property
    def topic_fanout(self) -> str:
        """Work queue: one message per aggregation cleared to fetch."""
        return self._topic(self.kafka_topic_fanout, "aggregation.fanout")

    @property
    def topic_delivery(self) -> str:
        """Outbound queue: the signed on-search envelope, awaiting its callback."""
        return self._topic(self.kafka_topic_delivery, "aggregation.delivery")

    @property
    def topic_retry(self) -> str:
        """Base name for the retry tiers. Not itself consumed."""
        return self._topic(self.kafka_topic_retry, "aggregation.delivery.retry")

    @property
    def retry_tiers(self) -> list:
        """One topic per backoff step: ``...delivery.retry.10s``, ``.60s``, …

        A single retry topic cannot work, and the reason is worth writing down
        because it is not obvious until it bites. The wait has to live
        somewhere, and the simple answer is to sleep in the consumer — but a
        Kafka consumer is a **loop**: it does not fetch the next batch until
        the current one is done. Put a 900-second hold and a 10-second hold on
        the same topic and the 10-second one waits 900 seconds, because the
        consumer is still asleep on the message in front of it.

        Splitting by duration removes the problem rather than managing it:
        every message on a tier waits exactly the same length of time, so a
        message that arrived earlier is always due earlier, and first-in
        first-out is precisely the right order to process them in. The sleep
        blocks only messages that were going to wait that long anyway.

        Each tier gets its own consumer group, so their offsets are
        independent — a tier that is busy cannot hold up a tier that is idle.
        """
        return [{"delay": delay,
                 "topic": "%s.%ds" % (self.topic_retry, delay),
                 "group": "%s-%ds" % (self.kafka_group_retry, delay)}
                for delay in self.retry_backoff]

    def retry_tier_for(self, delay: int) -> str:
        """The tier topic a given backoff belongs on.

        An exact match normally; otherwise the nearest tier at or above it, so
        a hand-set delay is never rounded *down* into a shorter wait.
        """
        tiers = self.retry_tiers
        for tier in tiers:
            if tier["delay"] >= delay:
                return tier["topic"]
        return tiers[-1]["topic"]

    @property
    def topic_dlq(self) -> str:
        """Anything that exhausted its attempts, kept for an operator to look at."""
        return self._topic(self.kafka_topic_dlq, "aggregation.dlq")

    @property
    def retry_backoff(self) -> list:
        """``kafka_retry_backoff_seconds`` as a list of ints.

        A malformed value must not strand deliveries, so it falls back to the
        documented default rather than raising at import time.
        """
        import logging

        raw = (self.kafka_retry_backoff_seconds or "").strip()
        try:
            values = [int(part) for part in raw.split(",") if part.strip()]
        except ValueError:
            values = []
        if not values:
            logging.getLogger(self.logging_default_logger_name).error(
                "aggregation_layer_kafka_retry_backoff_seconds is not a list of "
                "integers (%r); using 10,60,300,900", raw)
            values = [10, 60, 300, 900]
        return values
