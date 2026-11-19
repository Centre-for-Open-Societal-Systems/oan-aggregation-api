# Agri Stack Composite

The **use-case composite** serves approved Agri Stack use cases, such as `loan-profile`. A partner
sends one signed request with a farmer's identifier and consent; the composite queries each
registry (Farmer Registry, Crop Sown Registry, …) over DCI, signed with its own key and carrying the
partner's consent unchanged, and returns one signed response with a status per registry. Each
registry still checks the consent with the Consent Manager; nothing is stored by the composite.

Before installing, create the composite's signing key Secret and onboard the composite (and each
partner) in Partner Management — see the composite README in the agri-stack repository. A partner
calls a use case only when it has a policy for that use case in Partner Management (`name@major`,
e.g. `loan-profile@1`; one policy per use case).

**Use cases** are YAML configuration, not code. They are not in this form: edit `composite.useCases`
in the **Edit YAML** view of the values (one entry per use case), or point
`composite.existingUseCasesConfigMap` at your own ConfigMap. Running pods pick up changes within about
a minute, without a restart.

Full documentation: [Open Agri Stack](https://docs.openg2p.org/products/open-agri-stack) on the OpenG2P documentation site.

**Console** (on by default; `console.enabled: false` turns it off): a read-only staff console showing the use cases, registries
and a call log (outcomes and timings, no partner data). Untick **Enable Console** in the Console group
to leave it out. With it on, the chart deploys the console UI at the console hostname, enables the API's admin API with IAM
staff login, creates its database in the commons Postgres, its Keycloak client and its IAM application.
Grant staff the `AGRI_COMPOSITE_VIEWER` or `AGRI_COMPOSITE_ADMIN` role in IAM. Needs commons
(Postgres, Keycloak, Redis, IAM staff portal).
