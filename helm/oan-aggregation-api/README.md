# oan-aggregation-api chart

The OAN Aggregation API: one Deployment and ClusterIP Service named `<release>-aggregation-api`, an optional
Istio VirtualService, and the registry catalog as a ConfigMap. Stateless, no database. Settings are described in
the repository README, "Deployment (DevOps checklist)".

It runs as a subchart of `openg2p-commons-services` in oan-helm (alias `aggregation-api`, release
`commons-services` in namespace `commons`), bundled like `openg2p-master-data`:
`charts/openg2p-commons-services/charts/oan-aggregation-api-<version>.tgz`. Its values are the
`aggregation-api:` block of that chart's values.yaml; env values and `virtualService.host` may use
`{{ .Values.global.baseDomain }}`. Inside commons-services the names are `commons-services-aggregation-api`
and the selector is `app.kubernetes.io/instance: commons-services`, `app.kubernetes.io/component: aggregation-api`,
the same as the template commons-services had before, so upgrading to the subchart changes the Deployment in place.

## CI/CD

[`Jenkinsfile`](../../Jenkinsfile) (multibranch). Every branch and pull request runs the unit tests in the
built image and lints the chart; `develop` also:

1. pushes `eoan/oan-aggregation-api/aggregation-api:<appVersion>-develop.<build>` (e.g. `0.0.0-develop.42`) to
   ECR, creating the repository on the first push;
2. packages this chart with that image (oan-helm `scripts/ci/package_chart.py`, version
   `0.0.0-develop.<commit count>`);
3. publishes the archive to oan-helm `repository/develop/` (`publish_chart.py`);
4. opens a pull request on oan-helm that bundles it in `openg2p-commons-services`: the archive under
   `charts/openg2p-commons-services/charts/`, its version in `Chart.yaml` and `Chart.lock`
   (`vendor_subchart.py`). An older open pull request for this chart is closed as superseded.

Nothing is deployed by this pipeline. Dev runs a new build once that pull request is merged, oan-helm releases the
next `openg2p-commons-services` version and `commons-services` is upgraded (Rancher).

Jenkins credentials: `aws-ecr-creds`, `oan-helm-fork-token`.

## Prerequisites in the namespace

Created outside the chart (on dev they already exist in `commons`):

| Object | Keys | Value |
|---|---|---|
| Secret `signing.existingSecret` | `aggregation.p12`, `password` | This service's signing key; its certificate is registered in Partner Management with kid `signing.kid` |
| Secret `runtimeSecret` | `AGGREGATION_LAYER_STAFF_API_CLIENT_SECRET`, `AGGREGATION_LAYER_STAFF_SEARCH_TICKET_SECRET` | Loaded whole as environment |
| Secret `ecr-pull-secret` | | ECR pull secret, refreshed by the namespace's `ecr-pull-secret-refresh` CronJob |

## Values

| Key | Meaning |
|---|---|
| `image.repository`, `image.tag`, `image.digest` | CI writes the tag as `<tag>@sha256:<digest>`; a `digest` alone pins by digest |
| `fullnameOverride` | Resource name; default `<release>-aggregation-api` |
| `virtualService.enabled`, `.host`, `.gateway` | Istio route, e.g. `aggregation.{{ .Values.global.baseDomain }}` on `istio-system/development` |
| `signing.existingSecret`, `.p12Key`, `.passwordKey`, `.kid` | Signing key Secret, mounted at `/app/keys` |
| `runtimeSecret` | Secret loaded as environment |
| `catalog.registries` | Registry catalog ([`deploy/registries.yaml`](../../deploy/registries.yaml) format) rendered into a ConfigMap; a change rolls the pods |
| `catalog.existingConfigMap` | Or an existing ConfigMap with the key `registries.yaml` |
| `env` | Non-secret `AGGREGATION_LAYER_*` settings (rendered with `tpl`) |
