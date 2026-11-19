{{- define "agriComposite.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{ default (include "common.names.fullname" .) .Values.serviceAccount.name }}
{{- else -}}
{{ default "default" .Values.serviceAccount.name }}
{{- end -}}
{{- end -}}

{{/*
Service names of the API and the console UI: common.names.fullname over
composite.* / console.ui.* (as their Service templates render it), so routing
and in-cluster URLs match whatever the release is called. Works from the root
context and from the console UI's merged context (which carries composite and
console).
*/}}
{{- define "agriComposite.apiServiceName" -}}
{{- include "common.names.fullname" (dict "Values" .Values.composite "Chart" .Chart "Release" .Release) -}}
{{- end -}}

{{- define "agriComposite.consoleServiceName" -}}
{{- include "common.names.fullname" (dict "Values" .Values.console.ui "Chart" .Chart "Release" .Release) -}}
{{- end -}}

{{/*
Name of the ConfigMap holding the use cases.
*/}}
{{- define "agriComposite.useCasesConfigMap" -}}
{{- if .Values.existingUseCasesConfigMap -}}
{{ tpl .Values.existingUseCasesConfigMap $ }}
{{- else -}}
{{ .Release.Name }}-use-cases
{{- end -}}
{{- end -}}

{{/*
Registries as the service reads them: JSON {controller: {url, partner_id}}.
*/}}
{{- define "agriComposite.registriesJson" -}}
{{- $out := dict -}}
{{- range $id, $r := .Values.registries }}
{{- $_ := set $out $id (dict "url" (tpl (toString $r.url) $) "partner_id" (default "" $r.partnerId) "receiver_id" (default "" $r.receiverId)) -}}
{{- end -}}
{{- $out | toJson -}}
{{- end -}}

{{/*
env: list from .Values.envVars (templated literals) and .Values.envVarsFrom.
*/}}
{{- define "agriComposite.envVars" -}}
{{- range $key, $value := .Values.envVars }}
- name: {{ $key }}
  value: {{ tpl (printf "%v" $value) $ | quote }}
{{- end }}
- name: AGRI_COMPOSITE_REGISTRIES
  value: {{ include "agriComposite.registriesJson" . | quote }}
{{- with .Values.rateLimit.redis }}
- name: AGRI_COMPOSITE_RATE_LIMIT_REDIS_URL
  value: {{ tpl (toString (.url | default "")) $ | quote }}
- name: AGRI_COMPOSITE_RATE_LIMIT_REDIS_KEY_PREFIX
  value: {{ tpl (toString (.keyPrefix | default "agri-composite:ratelimit:")) $ | quote }}
- name: AGRI_COMPOSITE_RATE_LIMIT_REDIS_TIMEOUT_SECONDS
  value: {{ .timeoutSeconds | default 0.25 | quote }}
- name: AGRI_COMPOSITE_RATE_LIMIT_REDIS_RETRY_SECONDS
  value: {{ .retrySeconds | default 10 | quote }}
{{- if and .passwordSecret .passwordSecret.name }}
- name: AGRI_COMPOSITE_RATE_LIMIT_REDIS_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ tpl .passwordSecret.name $ | quote }}
      key: {{ .passwordSecret.key | default "redis-password" | quote }}
{{- end }}
{{- end }}
{{- range $key, $spec := .Values.envVarsFrom }}
- name: {{ $key }}
  valueFrom:
{{ tpl (toYaml $spec) $ | indent 4 }}
{{- end }}
{{- end -}}

{{/*
Console UI env: .Values.envVars (templated literals) and .Values.envVarsFrom.
*/}}
{{- define "agriComposite.consoleUiEnvVars" -}}
{{- range $key, $value := .Values.envVars }}
- name: {{ $key }}
  value: {{ tpl (printf "%v" $value) $ | quote }}
{{- end }}
{{- range $key, $spec := .Values.envVarsFrom }}
- name: {{ $key }}
  valueFrom:
{{ tpl (toYaml $spec) $ | indent 4 }}
{{- end }}
{{- end -}}

{{/*
API env added when console.enabled (root context): the console switch, its
Postgres database, the IAM staff login (iam-core settings) and console options.
*/}}
{{- define "agriComposite.consoleApiEnv" -}}
{{- $c := .Values.console -}}
- name: AGRI_COMPOSITE_CONSOLE_ENABLED
  value: "true"
# Empty: the URL is built from the DB_* parts below.
- name: AGRI_COMPOSITE_DB_DATASOURCE
  value: ""
- name: AGRI_COMPOSITE_DB_DRIVER
  value: {{ $c.db.driver | quote }}
- name: AGRI_COMPOSITE_DB_HOSTNAME
  value: {{ tpl (toString $c.db.hostname) $ | quote }}
- name: AGRI_COMPOSITE_DB_PORT
  value: {{ $c.db.port | quote }}
- name: AGRI_COMPOSITE_DB_DBNAME
  value: {{ tpl $c.db.dbname $ | quote }}
- name: AGRI_COMPOSITE_DB_USERNAME
  value: {{ tpl $c.db.user $ | quote }}
- name: AGRI_COMPOSITE_DB_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ tpl $c.db.secretName $ | quote }}
      key: {{ tpl $c.db.passwordKey $ | quote }}
- name: AGRI_COMPOSITE_AUTH_PROVIDER_API_URL
  value: {{ tpl $c.iam.providerApiUrl $ | quote }}
- name: AGRI_COMPOSITE_AUTH_REDIS_URL
  value: {{ tpl $c.iam.redisUrl $ | quote }}
- name: AGRI_COMPOSITE_AUTH_TRANSACTION_STORE_BACKEND
  value: {{ $c.iam.transactionStoreBackend | quote }}
- name: AGRI_COMPOSITE_AUTH_COOKIE_DOMAIN
  value: {{ tpl $c.iam.cookieDomain $ | quote }}
- name: AGRI_COMPOSITE_KEYCLOAK_CLIENT_ID
  value: {{ tpl $c.iam.keycloakClientId $ | quote }}
- name: AGRI_COMPOSITE_CSRF_ENABLED
  value: {{ $c.iam.csrfEnabled | quote }}
- name: AGRI_COMPOSITE_CONSOLE_PM_PORTAL_URL
  value: {{ tpl $c.pmPortalUrl $ | quote }}
- name: AGRI_COMPOSITE_CONSOLE_CM_PORTAL_URL
  value: {{ tpl $c.cmPortalUrl $ | quote }}
- name: AGRI_COMPOSITE_ACTIVITY_RETENTION_DAYS
  value: {{ $c.activityRetentionDays | quote }}
- name: AGRI_COMPOSITE_CATALOGUE_CACHE_SECONDS
  value: {{ $c.catalogueCacheSeconds | quote }}
{{- range $key, $value := $c.apiEnvVars }}
- name: {{ $key }}
  value: {{ tpl (printf "%v" $value) $ | quote }}
{{- end }}
{{- end -}}

{{/*
API init container (console.enabled and db.waitForDb.enabled): waits until it
can log in to the console database, at most timeoutSeconds, then lets the pod
start either way. Root context; securityContext is the API container's.
*/}}
{{- define "agriComposite.consoleWaitForDb" -}}
{{- $c := .root.Values.console -}}
{{- $w := $c.db.waitForDb -}}
- name: wait-for-console-db
  image: {{ printf "%s:%s" $w.image.repository (toString $w.image.tag) }}
  imagePullPolicy: {{ $w.image.pullPolicy }}
  {{- with .securityContext }}
  securityContext: {{- toYaml . | nindent 4 }}
  {{- end }}
  command: ["sh", "-c"]
  args:
    - |
      i=0
      until PGPASSWORD="${DB_PASSWORD}" psql -h "${DB_HOSTNAME}" -p "${DB_PORT}" -U "${DB_USERNAME}" -d "${DB_DBNAME}" -c "select 1" >/dev/null 2>&1; do
        if [ "$i" -ge "${TIMEOUT_SECONDS}" ]; then
          echo "console database not reachable after ${TIMEOUT_SECONDS}s; starting anyway (console stays off until the pod restarts)"
          exit 0
        fi
        echo "waiting for the console database ${DB_DBNAME} at ${DB_HOSTNAME}..."
        i=$((i+3)); sleep 3
      done
      echo "console database is ready."
  env:
    - name: DB_HOSTNAME
      value: {{ tpl (toString $c.db.hostname) .root | quote }}
    - name: DB_PORT
      value: {{ $c.db.port | quote }}
    - name: DB_DBNAME
      value: {{ tpl $c.db.dbname .root | quote }}
    - name: DB_USERNAME
      value: {{ tpl $c.db.user .root | quote }}
    - name: DB_PASSWORD
      valueFrom:
        secretKeyRef:
          name: {{ tpl $c.db.secretName .root | quote }}
          key: {{ tpl $c.db.passwordKey .root | quote }}
    - name: TIMEOUT_SECONDS
      value: {{ $w.timeoutSeconds | default 180 | quote }}
  resources:
    requests:
      cpu: 10m
      memory: 16Mi
    limits:
      memory: 64Mi
{{- end -}}

{{/*
corsAllowOrigins (comma-separated, templated) as a JSON list: "" → [].
*/}}
{{- define "agriComposite.corsOriginsJson" -}}
{{- $out := list -}}
{{- range splitList "," (tpl (.Values.corsAllowOrigins | default "") .) -}}
{{- if trim . -}}{{- $out = append $out (trim .) -}}{{- end -}}
{{- end -}}
{{- toJson $out -}}
{{- end -}}

{{/*
Edge rate limit: a duration (<n>s, <n>m, <n>h) as whole seconds.
*/}}
{{- define "agriComposite.durationSeconds" -}}
{{- $p := toString . | trim -}}
{{- $n := 0 -}}
{{- if hasSuffix "h" $p }}{{ $n = mul (trimSuffix "h" $p | atoi) 3600 }}
{{- else if hasSuffix "m" $p }}{{ $n = mul (trimSuffix "m" $p | atoi) 60 }}
{{- else }}{{ $n = trimSuffix "s" $p | atoi }}{{ end -}}
{{- if le (int $n) 0 }}{{ fail (printf "edgeRateLimit.per: %q is not a duration like 30s, 1m or 1h" $p) }}{{ end -}}
{{- $n -}}
{{- end -}}

{{/*
Edge rate limit names: unique per namespace and release (several installs share
one gateway). The route name is the Envoy route name the VirtualService sets.
*/}}
{{- define "agriComposite.edgeRateLimitName" -}}
{{- printf "%s-%s-edge-ratelimit" .Release.Namespace .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "agriComposite.edgeRateLimitRoute" -}}
{{- printf "%s.%s.edge-ratelimited" .Release.Name .Release.Namespace -}}
{{- end -}}
{{- define "agriComposite.edgeRateLimitFilter" -}}
{{- printf "agri-composite.local_ratelimit.%s.%s" .Release.Namespace .Release.Name -}}
{{- end -}}
